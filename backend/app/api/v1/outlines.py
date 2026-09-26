import uuid
from datetime import UTC, datetime
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_queue
from app.api.sse import event_stream_response
from app.api.v1.projects import OwnedProject
from app.core.db import get_session
from app.domain.layout import load_layouts
from app.models.project import Project, ProjectOutline
from app.observability.recorder import finish_trace, start_trace
from app.schemas.outline import (
    OutlineEvent,
    OutlineGenerateAccepted,
    OutlinePublic,
    OutlineRevisionRequest,
    OutlineUpdate,
)
from app.services.deck import reconcile_stuck_outline
from app.services.outline_inputs import migrate_outline_signature, outline_input_matches
from app.services.outline_progress import outline_events, publish_outline_event

router = APIRouter(prefix="/projects/{project_id}/outline", tags=["outline"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[ArqRedis, Depends(get_queue)]


def _outline_or_404(project: Project) -> ProjectOutline:
    if project.outline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="尚未生成大纲")
    return project.outline


def _ensure_revision(outline: ProjectOutline, revision: int) -> None:
    if outline.revision != revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="大纲已被其他操作更新，请刷新后重试",
        )


def _ensure_draft(outline: ProjectOutline) -> None:
    if outline.status != "draft":
        detail = "请先取消确认" if outline.status == "confirmed" else "当前大纲不可编辑"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _validate_pages(project: Project, pages: list) -> None:
    valid_layouts = load_layouts()
    invalid = sorted({page.layout_id for page in pages if page.layout_id not in valid_layouts})
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"大纲包含未知布局：{'、'.join(invalid)}",
        )
    if len(pages) != project.page_count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"大纲必须包含 {project.page_count} 页",
        )


@router.get("", response_model=OutlinePublic)
async def get_outline(
    project: OwnedProject,
    session: SessionDep,
) -> ProjectOutline:
    # 读路径对账（ADR-0001）：卡死的 generating 大纲在用户看到状态前复位成 failed
    outline = _outline_or_404(project)
    if reconcile_stuck_outline(outline):
        await session.commit()
        # commit 会过期属性；响应直接序列化 ORM 对象，须先刷新避免-greenlet 惰性加载
        await session.refresh(outline)
    return outline


@router.post(
    "/generate",
    response_model=OutlineGenerateAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_outline(
    project: OwnedProject,
    session: SessionDep,
    queue: QueueDep,
) -> OutlineGenerateAccepted:
    if not project.sources or not any(source.char_count > 0 for source in project.sources):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="请先添加可用于生成大纲的输入材料",
        )
    # 先对账卡死的大纲（ADR-0001）：worker 死亡留下的超时 generating 在此复位，
    # 下面的守卫只拦真正的活任务
    if project.outline is not None and reconcile_stuck_outline(project.outline):
        await session.commit()
    if project.outline is not None and project.outline.status == "confirmed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="请先取消确认")
    if project.outline is not None and project.outline.status == "generating":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="大纲正在生成")

    job_id = f"outline-{project.id}-{uuid.uuid4().hex}"
    outline = project.outline
    if outline is None:
        outline = ProjectOutline(project_id=project.id)
        session.add(outline)
    outline.status = "generating"
    outline.error = None
    outline.job_id = job_id
    # 进入 generating 的时刻；惰性对账判死 worker 用（ADR-0001）
    outline.started_at = datetime.now(UTC)
    await session.commit()

    # trace 先建、入队一次带 trace_id（#37）：arq 对已存在 _job_id 的二次入队
    # 是空操作，旧实现「先入队再补 trace_id」在生产队列上永远送不到 worker；
    # 队列挂掉的收口在下方 except 里显式 finish_trace，不留孤儿 running trace
    trace_id = await start_trace(kind="outline", project_id=project.id, job_id=job_id)
    try:
        job = await queue.enqueue_job(
            "generate_outline",
            str(project.id),
            job_id,
            str(trace_id) if trace_id is not None else None,
            _job_id=job_id,
        )
    except RedisError as error:
        outline.status = "failed"
        outline.error = "任务队列暂时不可用"
        await session.commit()
        if trace_id is not None:
            await finish_trace(
                trace_id,
                "failed",
                error_code="enqueue_failed",
                error_message="任务队列暂时不可用",
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务队列暂时不可用",
        ) from error

    if job is None:
        if trace_id is not None:
            # 同 _job_id 的任务已在队列：新建的 trace 无任务会跑，直接收口
            await finish_trace(trace_id, "cancelled", error_code="duplicate_job")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="任务已存在")

    await publish_outline_event(
        project.id,
        OutlineEvent(
            type="progress",
            status="generating",
            progress=0,
            message="任务已进入队列",
            revision=outline.revision,
            trace_id=trace_id,
        ),
    )
    return OutlineGenerateAccepted(job_id=job_id, trace_id=trace_id)


@router.patch("", response_model=OutlinePublic)
async def update_outline(
    body: OutlineUpdate,
    project: OwnedProject,
    session: SessionDep,
) -> ProjectOutline:
    outline = _outline_or_404(project)
    _ensure_draft(outline)
    _ensure_revision(outline, body.revision)
    _validate_pages(project, body.pages)

    outline.pages = [page.model_dump(mode="json") for page in body.pages]
    outline.revision += 1
    await session.commit()
    await session.refresh(outline)
    return outline


@router.post("/confirm", response_model=OutlinePublic)
async def confirm_outline(
    body: OutlineRevisionRequest,
    project: OwnedProject,
    session: SessionDep,
) -> ProjectOutline:
    outline = _outline_or_404(project)
    _ensure_draft(outline)
    _ensure_revision(outline, body.revision)
    if not outline_input_matches(project, outline.input_signature):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="生成大纲后输入材料或设置已变化，请重新生成",
        )
    # 匹配到 legacy 签名时升级为当前指纹
    migrated = migrate_outline_signature(project, outline.input_signature)
    if migrated is not None:
        outline.input_signature = migrated
    _validate_pages(project, [*map(_page_from_dict, outline.pages)])

    outline.status = "confirmed"
    outline.revision += 1
    project.status = "outline_ready"
    await session.commit()
    await session.refresh(outline)
    return outline


@router.post("/unconfirm", response_model=OutlinePublic)
async def unconfirm_outline(
    body: OutlineRevisionRequest,
    project: OwnedProject,
    session: SessionDep,
) -> ProjectOutline:
    outline = _outline_or_404(project)
    if outline.status != "confirmed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="大纲尚未确认")
    _ensure_revision(outline, body.revision)

    outline.status = "draft"
    outline.revision += 1
    project.status = "draft"
    await session.commit()
    await session.refresh(outline)
    return outline


def _page_from_dict(data: dict):
    from app.domain.outline import OutlinePage

    return OutlinePage.model_validate(data)


@router.get("/events")
async def stream_outline_events(
    request: Request,
    project: OwnedProject,
) -> StreamingResponse:
    outline = _outline_or_404(project)
    settled = outline.status in {"draft", "confirmed"}
    return event_stream_response(
        request,
        stream=outline_events,
        key=project.id,
        fallback=OutlineEvent(
            type="snapshot",
            status=outline.status,
            progress=100 if settled else 0,
            message="大纲已就绪" if settled else "等待任务进度",
            revision=outline.revision,
        ),
        terminal_types={"completed", "failed"},
    )
