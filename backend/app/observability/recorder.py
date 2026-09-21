"""trace / span 落库记录器（obs#1）。

全部公开函数自带异常吞咽：埋点是旁路，任何观测写失败只记 warning 日志，
绝不影响业务路径。调用方拿到的 trace_id / SpanHandle 失败时为 None，
下游自然降级为「本次不记录」。
"""

from __future__ import annotations

import functools
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from app.core.db import async_session_factory
from app.observability import context
from app.observability.models import Span, Trace

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpanHandle:
    """已落库 span 的轻量句柄：主键 + 进程内起点（算 duration 用）。"""

    span_id: int
    started_at: float


def _now() -> datetime:
    return datetime.now(UTC)


async def start_trace(
    *,
    kind: str,
    project_id: uuid.UUID,
    job_id: str | None = None,
    linked_trace_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """插入一条 running trace，返回其 id；失败返回 None（业务不中断）。"""
    trace_id = uuid.uuid4()
    try:
        async with async_session_factory() as session:
            session.add(
                Trace(
                    id=trace_id,
                    kind=kind,
                    status="running",
                    project_id=project_id,
                    job_id=job_id,
                    linked_trace_id=linked_trace_id,
                )
            )
            await session.commit()
        return trace_id
    except Exception:
        logger.warning("start_trace 落库失败（kind=%s project=%s）", kind, project_id)
        return None


async def finish_trace(
    trace_id: uuid.UUID,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(Trace)
                .where(Trace.id == trace_id)
                .values(
                    status=status,
                    error_code=error_code,
                    error_message=error_message,
                    finished_at=_now(),
                )
            )
            await session.commit()
    except Exception:
        logger.warning("finish_trace 落库失败（trace=%s status=%s）", trace_id, status)


async def start_span(
    name: str,
    span_kind: str,
    *,
    attributes: dict | None = None,
) -> SpanHandle | None:
    """插入一条 running span；无 trace 上下文或落库失败时返回 None。"""
    trace_id = context.current_trace_id()
    if trace_id is None:
        return None
    started = time.perf_counter()
    try:
        async with async_session_factory() as session:
            span = Span(
                trace_id=trace_id,
                parent_span_id=context.current_span_id(),
                name=name,
                span_kind=span_kind,
                attributes=attributes,
            )
            session.add(span)
            await session.commit()
            return SpanHandle(span_id=span.id, started_at=started)
    except Exception:
        logger.warning("start_span 落库失败（name=%s）", name)
        return None


async def finish_span(
    handle: SpanHandle | None,
    status: str = "succeeded",
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    model: str | None = None,
    purpose: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    if handle is None:
        return
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(Span)
                .where(Span.id == handle.span_id)
                .values(
                    status=status,
                    finished_at=_now(),
                    duration_ms=int((time.perf_counter() - handle.started_at) * 1000),
                    error_code=error_code,
                    error_message=error_message,
                    model=model,
                    purpose=purpose,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            )
            await session.commit()
    except Exception:
        logger.warning("finish_span 落库失败（span=%s）", handle.span_id)


@asynccontextmanager
async def span(name: str, span_kind: str, **attributes: Any):
    """同步便捷入口：进入即 start，异常时 finish(failed) 并透传异常。"""
    handle = await start_span(name, span_kind, attributes=attributes or None)
    token = context.set_span_id(handle.span_id) if handle is not None else None
    try:
        yield handle
    except Exception as error:
        await finish_span(
            handle,
            "failed",
            error_code=error.__class__.__name__,
            error_message=str(error) or error.__class__.__name__,
        )
        raise
    else:
        await finish_span(handle, "succeeded")
    finally:
        if token is not None:
            context.reset_span_id(token)


async def latest_outline_trace_id(project_id: uuid.UUID) -> uuid.UUID | None:
    """项目最近一次大纲 trace，供 deck trace 建链；查询失败按无链处理。"""
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Trace.id)
                .where(Trace.project_id == project_id, Trace.kind == "outline")
                .order_by(Trace.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()
    except Exception:
        logger.warning("latest_outline_trace_id 查询失败（project=%s）", project_id)
        return None


async def trace_exists(trace_id: uuid.UUID) -> bool:
    """确认 trace 仍存在（export 挂靠前检查）；查询失败按不存在处理。"""
    try:
        async with async_session_factory() as session:
            return await session.get(Trace, trace_id) is not None
    except Exception:
        logger.warning("trace_exists 查询失败（trace=%s）", trace_id)
        return False


def traced_node(
    name: str,
    span_kind: str = "node",
    *,
    attributes: Callable[[dict], dict] | None = None,
):
    """LangGraph 节点协程的 span 包装（obs#2）。

    不改图结构：``prepare = traced_node("outline.prepare")(prepare)``。
    进入时开 span 并把 span_id 压入 contextvar（节点内 LLM 调用自动挂成
    子 span）；无 trace 上下文时零开销直通。attributes 回调拿到节点返回的
    state diff（只含新增键），拿不到的值（如 repair 轮次）就地省略。
    """

    def decorator(node: Callable[..., Awaitable[dict]]):
        @functools.wraps(node)
        async def wrapper(state):
            if attributes is not None:
                try:
                    extra = attributes(state)
                except Exception:
                    extra = None
            else:
                extra = None
            async with span(name, span_kind, **(extra or {})):
                return await node(state)

        return wrapper

    return decorator
