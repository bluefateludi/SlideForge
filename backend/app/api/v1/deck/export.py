import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.v1.deck._shared import SessionDep
from app.api.v1.projects import OwnedProject
from app.domain.export_check import ExportCheckReport
from app.domain.theme import resolve_project_theme
from app.observability import context
from app.observability.recorder import span, trace_exists
from app.render.pptx import PPTX_MEDIA_TYPE, render_deck_to_pptx
from app.render.verify import verify_pptx
from app.schemas.deck import DeckPublic
from app.services.deck import load_slides, to_deck_public
from app.services.quality import build_quality_report, project_to_content_deck

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/deck", tags=["deck"])


@router.get("", response_model=DeckPublic)
async def get_deck(project: OwnedProject, session: SessionDep) -> DeckPublic:
    slides = await load_slides(session, project.id)
    return to_deck_public(project, slides)


@router.get("/quality", response_model=ExportCheckReport)
async def get_deck_quality(project: OwnedProject, session: SessionDep) -> ExportCheckReport:
    """导出前质量报告：分级 issues 与是否允许导出。检查逻辑见 build_quality_report。"""
    slides = await load_slides(session, project.id)
    return build_quality_report(project, slides)


@router.get("/export")
async def export_deck(project: OwnedProject, session: SessionDep) -> StreamingResponse:
    """同步导出项目 PPTX：检查 → 渲染 → 回读验证 → 返回文件流。"""
    slides = await load_slides(session, project.id)
    if not slides or any(slide.status != "ready" for slide in slides):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="页面尚未全部生成完成，无法导出",
        )

    async with _export_trace(project) as trace_id:
        quality = await _run_quality_check(project, slides, trace_id)
        if not quality.export_allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "导出前检查未通过，存在必须修复的问题",
                    "report": quality.model_dump(mode="json"),
                },
            )

        deck = project_to_content_deck(project, slides)
        try:
            buffer = await _run_render(project, deck, trace_id)
            payload = buffer.getvalue()
        except _RenderFailed as error:
            logger.exception("项目 %s 导出渲染失败", project.id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"PPTX 渲染失败：{error}",
            ) from error

        try:
            verified = await _run_verify(payload, deck, trace_id)
        except _VerifyFailed as error:
            logger.exception("项目 %s 导出回读验证异常", project.id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"导出回读验证失败：{error}",
            ) from error

    if not verified.passed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "导出回读验证未通过，未返回文件",
                "issues": [issue.model_dump(mode="json") for issue in verified.issues],
            },
        )

    filename = quote(f"{project.title}.pptx")
    return StreamingResponse(
        BytesIO(payload),
        media_type=PPTX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


class _RenderFailed(Exception):
    """渲染异常的内部标记：原异常作为 __cause__ 挂在 HTTPException 上。"""


class _VerifyFailed(Exception):
    """回读验证异常的内部标记：原异常作为 __cause__ 挂在 HTTPException 上。"""


async def _run_sync(func: Callable, *args, **kwargs):
    """同步渲染/验证放线程池，避免阻塞事件循环（行为对齐原直接调用）。"""
    return await asyncio.to_thread(func, *args, **kwargs)


@asynccontextmanager
async def _export_trace(project: OwnedProject) -> AsyncIterator[uuid.UUID | None]:
    """导出 span 归属到最近一次 deck trace（obs#2）。

    无 last_deck_trace_id 或 trace 已不存在时整个跳过埋点（yield None），
    导出照常进行；临时 set trace contextvar，退出时复位。
    """
    trace_id = project.last_deck_trace_id
    if trace_id is None or not await trace_exists(trace_id):
        yield None
        return
    token = context.set_trace_id(trace_id)
    try:
        yield trace_id
    finally:
        context.reset_trace_id(token)


async def _run_quality_check(project: OwnedProject, slides, trace_id: uuid.UUID | None):
    """质量检查步骤；span 只在挂到有效 trace 时开启（None 直通）。"""
    if trace_id is None:
        return build_quality_report(project, slides)
    async with span("export.quality_check", "export"):
        return build_quality_report(project, slides)


async def _run_render(project: OwnedProject, deck, trace_id: uuid.UUID | None):
    """PPTX 渲染步骤；渲染是 CPU 同步代码，放线程池执行。"""
    if trace_id is None:
        try:
            return await _run_sync(
                render_deck_to_pptx, deck, theme=resolve_project_theme(project)
            )
        except Exception as error:
            raise _RenderFailed(str(error) or error.__class__.__name__) from error
    try:
        async with span("export.render", "export"):
            return await _run_sync(
                render_deck_to_pptx, deck, theme=resolve_project_theme(project)
            )
    except Exception as error:
        raise _RenderFailed(str(error) or error.__class__.__name__) from error


async def _run_verify(payload: bytes, deck, trace_id: uuid.UUID | None):
    """回读验证步骤；异常在 span 内被记为 failed 后再转内部标记。"""
    if trace_id is None:
        try:
            return await _run_sync(verify_pptx, payload, deck)
        except Exception as error:
            raise _VerifyFailed(str(error) or error.__class__.__name__) from error
    try:
        async with span("export.verify", "export"):
            return await _run_sync(verify_pptx, payload, deck)
    except Exception as error:
        raise _VerifyFailed(str(error) or error.__class__.__name__) from error
