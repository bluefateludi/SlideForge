import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import async_session_factory
from app.domain.outline import OutlinePage
from app.llm.base import OutlineGenerationInput, OutlineGenerator, OutlineSourceSection
from app.llm.errors import InvalidModelOutputError, LLMNotConfiguredError
from app.models.project import Project
from app.observability import context
from app.observability.recorder import finish_trace
from app.schemas.outline import OutlineEvent
from app.services.outline_inputs import project_input_signature
from app.services.outline_progress import publish_outline_event
from app.worker.context import create_outline_generator
from app.worker.retry import retry_after_failure
from app.workflows.outline import build_outline_workflow, run_outline_workflow

__all__ = ["create_outline_generator", "generate_outline"]

logger = logging.getLogger(__name__)


async def generate_outline(
    ctx: dict[str, Any],
    project_id: str,
    job_id: str,
    trace_id: str | None = None,
) -> None:
    parsed_trace = uuid.UUID(trace_id) if trace_id else None
    trace_token = context.set_trace_id(parsed_trace)
    try:
        await _generate_outline(ctx, project_id, job_id, parsed_trace)
    finally:
        context.reset_trace_id(trace_token)


async def _generate_outline(
    ctx: dict[str, Any],
    project_id: str,
    job_id: str,
    trace_id: uuid.UUID | None,
) -> None:
    project_uuid = uuid.UUID(project_id)
    logger.info("outline 任务开始 job_id=%s", job_id)
    await _progress(project_uuid, 10, "正在整理输入材料")

    loaded = await _load_generation_input(project_uuid, job_id)
    if loaded is None:
        # stale job 被新任务取代：按取消收口而不是成功
        if trace_id is not None:
            await finish_trace(trace_id, "cancelled", error_code="stale_job")
        return
    payload, input_signature, expected_revision = loaded

    await _progress(project_uuid, 25, "正在规划大纲结构")
    generator: OutlineGenerator = ctx["outline_generator"]

    try:
        workflow = build_outline_workflow(generator)
        draft = await run_outline_workflow(workflow, payload)
        pages = [OutlinePage(**page.model_dump()) for page in draft.pages]
        await _progress(project_uuid, 90, "正在保存大纲")
        revision = await _save_completed(
            project_uuid,
            job_id,
            pages,
            input_signature,
            expected_revision,
        )
    except Exception as error:
        retry = retry_after_failure(ctx, error)
        if retry is not None:
            await _progress(project_uuid, 30, "模型调用失败，正在重试")
            # arq 会重跑同一 job，trace 继续跑，此处不收口
            raise retry from error
        message = _public_error(error)
        if trace_id is not None:
            await finish_trace(
                trace_id,
                "failed",
                error_code=_error_code(error),
                error_message=message,
            )
        await _save_failed(project_uuid, job_id, message)
        return

    if revision is None:
        if trace_id is not None:
            await finish_trace(trace_id, "cancelled", error_code="stale_job")
        return

    if trace_id is not None:
        await finish_trace(trace_id, "succeeded")
    logger.info("outline 任务完成 job_id=%s", job_id)

    await publish_outline_event(
        project_uuid,
        OutlineEvent(
            type="completed",
            status="draft",
            progress=100,
            message="大纲已生成",
            revision=revision,
            trace_id=context.current_trace_id(),
        ),
    )


async def _load_generation_input(
    project_id: uuid.UUID,
    job_id: str,
) -> tuple[OutlineGenerationInput, str, int] | None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Project)
            .options(selectinload(Project.sources), selectinload(Project.outline))
            .where(Project.id == project_id)
        )
        project = result.scalar_one_or_none()
        if (
            project is None
            or project.outline is None
            or project.outline.job_id != job_id
            or project.outline.status != "generating"
        ):
            return None

        sections = [
            OutlineSourceSection(
                ref=f"S{source_index}:{section_index}",
                heading=section.get("heading"),
                level=section.get("level", 0),
                text=section.get("text", ""),
                locator=section.get("locator", ""),
            )
            for source_index, source in enumerate(project.sources, start=1)
            for section_index, section in enumerate(source.sections, start=1)
        ]
        from app.domain.content_density import normalize_density

        payload = OutlineGenerationInput(
            title=project.title,
            audience=project.audience,
            tone=project.tone,
            page_count=project.page_count,
            content_density=normalize_density(getattr(project, "content_density", None)),
            sections=sections,
        )
        return payload, project_input_signature(project), project.outline.revision


async def _save_completed(
    project_id: uuid.UUID,
    job_id: str,
    pages: list[OutlinePage],
    input_signature: str,
    expected_revision: int,
) -> int | None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Project)
            .options(selectinload(Project.outline))
            .where(Project.id == project_id)
            .with_for_update()
        )
        project = result.scalar_one()
        outline = project.outline
        if (
            outline is None
            or outline.job_id != job_id
            or outline.status != "generating"
            or outline.revision != expected_revision
        ):
            # 用户已经启动了更新任务时，迟到结果必须丢弃，不能覆盖新状态。
            return None

        outline.pages = [page.model_dump(mode="json") for page in pages]
        outline.input_signature = input_signature
        outline.status = "draft"
        outline.error = None
        outline.revision += 1
        project.status = "draft"
        await session.commit()
        return outline.revision


async def _save_failed(project_id: uuid.UUID, job_id: str, message: str) -> None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Project)
            .options(selectinload(Project.outline))
            .where(Project.id == project_id)
            .with_for_update()
        )
        project = result.scalar_one_or_none()
        if project is None or project.outline is None or project.outline.job_id != job_id:
            return
        project.outline.status = "failed"
        project.outline.error = message
        await session.commit()

    await publish_outline_event(
        project_id,
        OutlineEvent(
            type="failed",
            status="failed",
            progress=100,
            message=message,
            trace_id=context.current_trace_id(),
        ),
    )


async def _progress(project_id: uuid.UUID, progress: int, message: str) -> None:
    await publish_outline_event(
        project_id,
        OutlineEvent(
            type="progress",
            status="generating",
            progress=progress,
            message=message,
            trace_id=context.current_trace_id(),
        ),
    )


def _public_error(error: Exception) -> str:
    if isinstance(error, LLMNotConfiguredError):
        return str(error)
    # 不把供应商响应或完整输入材料落库，避免错误信息成为敏感数据旁路。
    return "模型生成大纲失败，请稍后重试"


def _error_code(error: Exception) -> str:
    if isinstance(error, LLMNotConfiguredError):
        return "llm_not_configured"
    if isinstance(error, InvalidModelOutputError):
        return "llm_error"
    return "internal_error"
