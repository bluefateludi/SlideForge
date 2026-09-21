"""obs#1 trace 地基测试：recorder CRUD / 吞异常、logging filter。"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import delete

from app.core.db import async_session_factory
from app.observability import context
from app.observability.logging import TraceIdFilter, setup_logging
from app.observability.models import Span, Trace
from app.observability.recorder import (
    finish_span,
    finish_trace,
    latest_outline_trace_id,
    span,
    start_span,
    start_trace,
)


@pytest.fixture
async def project_id() -> uuid.UUID:
    """建一个真实 project 供 trace 外键使用，用例结束级联清理。"""
    from app.models.project import Project
    from app.models.user import User

    async with async_session_factory() as session:
        user = User(email=f"obs_{uuid.uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        project = Project(
            user_id=user.id, title="观测测试", tone="professional", page_count=5, theme_id="default"
        )
        session.add(project)
        await session.commit()
        yield project.id
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


@pytest.fixture(autouse=True)
async def _clean_context() -> None:
    # contextvar 泄漏到其他用例会串 trace，进出各复位一次
    token = context.set_trace_id(None)
    span_token = context.set_span_id(None)
    yield
    context.reset_trace_id(token)
    context.reset_span_id(span_token)


async def _fetch_trace(trace_id: uuid.UUID) -> Trace | None:
    async with async_session_factory() as session:
        return await session.get(Trace, trace_id)


async def _fetch_span(span_id: int) -> Span | None:
    async with async_session_factory() as session:
        return await session.get(Span, span_id)


async def test_trace_lifecycle_start_and_finish(project_id: uuid.UUID) -> None:
    trace_id = await start_trace(kind="outline", project_id=project_id, job_id="job-1")
    assert trace_id is not None
    row = await _fetch_trace(trace_id)
    assert row is not None
    assert row.kind == "outline"
    assert row.status == "running"
    assert row.job_id == "job-1"
    assert row.finished_at is None

    await finish_trace(trace_id, "failed", error_code="llm_error", error_message="模型失败")
    row = await _fetch_trace(trace_id)
    assert row is not None
    assert row.status == "failed"
    assert row.error_code == "llm_error"
    assert row.error_message == "模型失败"
    assert row.finished_at is not None


async def test_span_requires_trace_context(project_id: uuid.UUID) -> None:
    assert await start_span("no-trace", "task") is None
    # finish(None) 应静默通过
    await finish_span(None, "succeeded")


async def test_span_lifecycle_with_contextvar(project_id: uuid.UUID) -> None:
    trace_id = await start_trace(kind="deck", project_id=project_id)
    assert trace_id is not None
    token = context.set_trace_id(trace_id)
    try:
        handle = await start_span("generate_deck", "task", attributes={"pages": 3})
        assert handle is not None
        row = await _fetch_span(handle.span_id)
        assert row is not None
        assert row.name == "generate_deck"
        assert row.span_kind == "task"
        assert row.status == "running"
        assert row.trace_id == trace_id
        assert row.attributes == {"pages": 3}
        assert row.duration_ms is None

        await finish_span(
            handle,
            "succeeded",
            model="deepseek-v4-flash",
            purpose="outline",
            prompt_tokens=100,
            completion_tokens=200,
        )
        row = await _fetch_span(handle.span_id)
        assert row is not None
        assert row.status == "succeeded"
        assert row.model == "deepseek-v4-flash"
        assert row.purpose == "outline"
        assert row.prompt_tokens == 100
        assert row.completion_tokens == 200
        assert row.duration_ms is not None
        assert row.finished_at is not None
    finally:
        context.reset_trace_id(token)


async def test_span_context_manager_success_and_failure(project_id: uuid.UUID) -> None:
    trace_id = await start_trace(kind="outline", project_id=project_id)
    assert trace_id is not None
    token = context.set_trace_id(trace_id)
    try:
        async with span("ok_step", "node", page=1) as handle:
            assert handle is not None
            # 进入 span 后嵌套 span 的 parent 应指向当前 span
            child = await start_span("child", "llm")
            assert child is not None
            child_row = await _fetch_span(child.span_id)
            assert child_row is not None
            assert child_row.parent_span_id == handle.span_id
        row = await _fetch_span(handle.span_id)
        assert row is not None
        assert row.status == "succeeded"
        # 退出后 span contextvar 复位
        assert context.current_span_id() is None

        with pytest.raises(ValueError, match="炸了"):
            async with span("bad_step", "node") as failing:
                assert failing is not None
                raise ValueError("炸了")
        failed_row = await _fetch_span(failing.span_id)
        assert failed_row is not None
        assert failed_row.status == "failed"
        assert failed_row.error_code == "ValueError"
        assert "炸了" in (failed_row.error_message or "")
    finally:
        context.reset_trace_id(token)


async def test_recorder_swallows_db_errors(
    project_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """埋点失败绝不影响业务路径：全部落库函数吞异常。"""
    from app.observability import recorder

    class _Raising:
        def __aenter__(self):
            raise RuntimeError("db down")

        def __aexit__(self, *exc):
            return False

    # 用 __aenter__ 即抛错的假 session 工厂覆盖所有落库路径
    monkeypatch.setattr(recorder, "async_session_factory", lambda *a, **k: _Raising())

    # 不抛错即通过；start_trace 降级返回 None
    assert await start_trace(kind="outline", project_id=project_id) is None
    await finish_trace(uuid.uuid4(), "failed")  # 不抛错
    assert await start_span("x", "task") is None  # 无上下文也是 None
    token = context.set_trace_id(uuid.uuid4())
    try:
        assert await start_span("y", "task") is None  # 落库失败降级 None
        async with span("z", "task") as handle:
            assert handle is None  # 内部 start 失败，句柄为 None
    finally:
        context.reset_trace_id(token)
    assert await latest_outline_trace_id(project_id) is None


async def test_latest_outline_trace_id(project_id: uuid.UUID) -> None:
    async with async_session_factory() as session:
        rows = [
            Trace(kind="deck", project_id=project_id),
            Trace(kind="outline", project_id=project_id),
            Trace(kind="outline", project_id=project_id),
        ]
        session.add_all(rows)
        await session.flush()
        # created_at 默认同事务同秒，显式错开保证排序稳定
        from datetime import UTC, datetime, timedelta

        base = datetime.now(UTC)
        rows[0].created_at = base - timedelta(minutes=30)
        rows[1].created_at = base - timedelta(minutes=20)
        rows[2].created_at = base - timedelta(minutes=10)
        await session.commit()
        expected = rows[2].id

    assert await latest_outline_trace_id(project_id) == expected
    # 无 outline trace 的项目返回 None
    other = uuid.uuid4()
    assert await latest_outline_trace_id(other) is None


def test_trace_id_filter_injects_trace_id() -> None:
    flt = TraceIdFilter()
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )
    assert flt.filter(record) is True
    assert record.trace_id == "-"

    token = context.set_trace_id(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    try:
        record2 = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hi",
            args=(),
            exc_info=None,
        )
        assert flt.filter(record2) is True
        assert record2.trace_id == "12345678-1234-5678-1234-567812345678"
    finally:
        context.reset_trace_id(token)


def test_setup_logging_configures_root_with_filter(capsys) -> None:
    setup_logging()
    root = logging.getLogger()
    assert root.level == logging.INFO
    handlers = root.handlers
    assert handlers, "root 应有 handler"
    has_trace_filter = any(any(isinstance(f, TraceIdFilter) for f in h.filters) for h in handlers)
    assert has_trace_filter, "console handler 应挂 TraceIdFilter"

    token = context.set_trace_id(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    try:
        logging.getLogger("obs.test").info("带 trace 的日志")
    finally:
        context.reset_trace_id(token)
    out = capsys.readouterr().err + capsys.readouterr().out
    assert "[trace_id=12345678-1234-5678-1234-567812345678]" in out
