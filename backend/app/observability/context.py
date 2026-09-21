"""当前 trace / span 的进程内上下文（contextvar）。

API 与 worker 任务入口各自独立事件循环，contextvar 天然隔离；
deck 任务逐页 asyncio.Task 会复制当前上下文，各页 span 互不串线。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_trace_id: ContextVar[uuid.UUID | None] = ContextVar("slideforge_trace_id", default=None)
_span_id: ContextVar[int | None] = ContextVar("slideforge_span_id", default=None)


def current_trace_id() -> uuid.UUID | None:
    return _trace_id.get()


def current_span_id() -> int | None:
    return _span_id.get()


def set_trace_id(trace_id: uuid.UUID | None) -> object:
    """设置当前 trace，返回可传给 reset 的 token。"""
    return _trace_id.set(trace_id)


def set_span_id(span_id: int | None) -> object:
    """设置当前 span，返回可传给 reset 的 token。"""
    return _span_id.set(span_id)


def reset_trace_id(token: object) -> None:
    _trace_id.reset(token)  # type: ignore[arg-type]


def reset_span_id(token: object) -> None:
    _span_id.reset(token)  # type: ignore[arg-type]
