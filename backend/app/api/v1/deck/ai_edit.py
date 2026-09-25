import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.v1.deck._shared import (
    SessionDep,
    _commit_slide_edit,
    _dump_layout_tree,
    _ensure_editable,
    _find_slide,
)
from app.api.v1.projects import OwnedProject
from app.domain.edit_ops import (
    EditStructureError,
    add_block,
    change_block_type,
    delete_block,
    parse_block,
    restore_block,
)
from app.domain.flex_layout import FlexContainer
from app.domain.slide_patch import apply_patches, filter_patches, unlocked_editable_blocks
from app.llm.base import AiEditHistoryTurn, SlideEditInput
from app.llm.client import _is_timeout
from app.llm.errors import InvalidSlideEditOutputError, LLMNotConfiguredError
from app.llm.slide_edit import block_to_edit_input
from app.observability import codes, context
from app.observability.recorder import finish_trace, start_trace
from app.schemas.deck import (
    AiEditApplyRequest,
    AiEditOperationPublic,
    AiEditProposalPublic,
    AiEditRequest,
    DiscardedOperationPublic,
    SlidePublic,
)
from app.services.deck import load_slides
from app.worker.context import create_slide_edit_generator
from app.workflows.slide_edit import (
    build_slide_edit_workflow,
    parse_slide_blocks,
    run_slide_edit_workflow,
)

router = APIRouter(prefix="/projects/{project_id}/deck", tags=["deck"])


@router.post(
    "/slides/{slide_id}/ai-edit",
    response_model=AiEditProposalPublic,
)
async def propose_slide_ai_edit(
    slide_id: uuid.UUID,
    body: AiEditRequest,
    project: OwnedProject,
    session: SessionDep,
) -> AiEditProposalPublic:
    slides = await load_slides(session, project.id)
    slide = _find_slide(slides, slide_id)
    _ensure_editable(slide, body.revision)

    blocks = parse_slide_blocks(slide.blocks)
    editable = unlocked_editable_blocks(blocks)
    flex = slide.layout_mode == "flex" and slide.layout_tree is not None
    if not editable and not flex:
        return AiEditProposalPublic(
            revision=slide.revision,
            operations=[],
            discarded=[],
            warnings=[],
        )

    layout_tree = FlexContainer.model_validate(slide.layout_tree) if flex else None
    payload = SlideEditInput(
        deck_title=project.title,
        audience=project.audience,
        tone=project.tone,
        page_title=slide.title,
        layout_id=slide.layout_id,
        layout_mode=slide.layout_mode,
        instruction=body.instruction,
        history=[
            AiEditHistoryTurn(instruction=item.instruction, note=item.note) for item in body.history
        ],
        blocks=[block_to_edit_input(block) for block in editable],
        original_blocks=blocks,
        layout_tree=layout_tree,
    )

    # 一次 AI 编辑一条 trace（obs#6）：同步路径在请求出口即收口，
    # 不经队列、worker 无关。trace 建失败（返回 None）时全程降级为不记录。
    trace_id = await start_trace(kind="ai_edit", project_id=project.id)
    token = context.set_trace_id(trace_id) if trace_id is not None else None
    workflow = build_slide_edit_workflow(create_slide_edit_generator())
    try:
        operations, discarded, issues, _patched = await run_slide_edit_workflow(
            workflow,
            payload=payload,
            slide_id=str(slide.id),
            layout_id=slide.layout_id,
            layout_mode=slide.layout_mode,
            layout_tree=layout_tree,
            blocks=blocks,
            theme_id=project.theme_id,
            theme_overrides=dict(project.theme_overrides or {}),
        )
    except LLMNotConfiguredError as error:
        await _finish_ai_edit_trace(trace_id, codes.LLM_NOT_CONFIGURED, str(error))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    except InvalidSlideEditOutputError as error:
        await _finish_ai_edit_trace(trace_id, codes.LLM_ERROR, str(error))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except Exception as error:
        # 工具循环超时（openai.APITimeoutError 等）记 llm_timeout，其余按内部错误；
        # 意外异常不落原文，只留类名，避免供应商响应成为敏感数据旁路。
        if _is_timeout(error):
            await _finish_ai_edit_trace(trace_id, codes.LLM_TIMEOUT, str(error))
        else:
            await _finish_ai_edit_trace(trace_id, codes.INTERNAL_ERROR, error.__class__.__name__)
        raise
    else:
        await _finish_ai_edit_trace(trace_id, None, None)
    finally:
        if token is not None:
            context.reset_trace_id(token)

    return AiEditProposalPublic(
        revision=slide.revision,
        operations=[
            AiEditOperationPublic(
                op=item.op,
                block_id=item.block_id,
                slot_id=item.slot_id,
                type=item.type,
                after_block_id=item.after_block_id,
                before=item.before,
                after=item.after,
            )
            for item in operations
        ],
        discarded=[
            DiscardedOperationPublic(block_id=item.block_id, reason=item.reason)
            for item in discarded
        ],
        warnings=[issue for issue in issues if issue.severity == "warning"],
        trace_id=trace_id,
    )


async def _finish_ai_edit_trace(
    trace_id: uuid.UUID | None, error_code: str | None, error_message: str | None
) -> None:
    """收口 ai_edit trace；error_code 为 None 即成功。trace_id 为 None 时是空操作。"""
    if trace_id is None:
        return
    await finish_trace(
        trace_id,
        "succeeded" if error_code is None else "failed",
        error_code=error_code,
        error_message=error_message,
    )


@router.post(
    "/slides/{slide_id}/ai-edit/apply",
    response_model=SlidePublic,
)
async def apply_slide_ai_edit(
    slide_id: uuid.UUID,
    body: AiEditApplyRequest,
    project: OwnedProject,
    session: SessionDep,
) -> SlidePublic:
    slides = await load_slides(session, project.id)
    slide = _find_slide(slides, slide_id)
    _ensure_editable(slide, body.revision)

    blocks = parse_slide_blocks(slide.blocks)
    tree = (
        FlexContainer.model_validate(slide.layout_tree)
        if slide.layout_mode == "flex" and slide.layout_tree is not None
        else None
    )
    try:
        blocks, tree = _apply_one(body, blocks, tree)
    except EditStructureError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    slide.blocks = [block.model_dump(mode="json") for block in blocks]
    if tree is not None:
        slide.layout_tree = _dump_layout_tree(tree)
    return await _commit_slide_edit(session, project, slide)


def _has_block(blocks, block_id: str) -> bool:
    return any(block.id == block_id for block in blocks)


def _apply_one(body: AiEditApplyRequest, blocks, tree):
    if body.op == "replace":
        if body.replace is None:
            raise EditStructureError("缺少替换内容")
        filtered = filter_patches(blocks, [body.replace])
        return apply_patches(blocks, filtered.accepted), tree

    if tree is None:
        raise EditStructureError("当前页面不是灵活布局，无法增删或改类型")

    if body.op == "add":
        if body.side == "after":
            if _has_block(blocks, body.block_id):
                return blocks, tree
            if not body.block or not body.after_block_id:
                raise EditStructureError("新增块缺少内容或锚点")
            raw = dict(body.block)
            created_id = str(raw.get("id") or body.block_id)
            block_type = str(raw.get("type") or "text")
            patched, tree, _ = add_block(
                blocks,
                tree,
                block_type=block_type,
                after_block_id=body.after_block_id,
                content=raw,
                block_id=created_id,
            )
            return patched, tree
        if not _has_block(blocks, body.block_id):
            return blocks, tree
        patched, tree, _ = delete_block(blocks, tree, body.block_id)
        return patched, tree

    if body.op == "delete":
        if body.side == "after":
            if not _has_block(blocks, body.block_id):
                return blocks, tree
            patched, tree, _ = delete_block(blocks, tree, body.block_id)
            return patched, tree
        if _has_block(blocks, body.block_id):
            return blocks, tree
        if not body.block:
            raise EditStructureError("恢复删除缺少原块内容")
        restored = parse_block(body.block)
        return restore_block(
            blocks, tree, block=restored, after_block_id=body.after_block_id
        )

    if body.side == "after":
        if not body.block:
            raise EditStructureError("改类型缺少新块内容")
        raw = dict(body.block)
        patched, tree, _old, _new = change_block_type(
            blocks,
            tree,
            block_id=body.block_id,
            new_type=str(raw.get("type") or body.block.get("type")),
            content=raw,
        )
        return patched, tree
    if not body.block:
        raise EditStructureError("恢复类型缺少原块内容")
    raw = dict(body.block)
    patched, tree, _old, _new = change_block_type(
        blocks,
        tree,
        block_id=body.block_id,
        new_type=str(raw.get("type")),
        content=raw,
    )
    return patched, tree
