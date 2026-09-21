"""trace join 聚合（obs#4）：造 trace+spans → 断言 token/分段耗时聚合。

真实 Postgres：覆盖 join_trace_metrics 的 token 求和、分段耗时切分、
查不到 trace 的降级语义；join_report_traces 走完整报告路径。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.core.db import async_session_factory
from app.eval.evaluator import CaseScore
from app.eval.persistence import join_report_traces
from app.eval.report import build_report
from app.eval.runner import CaseArtifacts
from app.models.project import Project
from app.models.user import User
from app.observability.models import Span, Trace


@pytest.fixture
async def project_id() -> uuid.UUID:
    """建一个真实 project 供 trace 外键使用，用例结束级联清理。"""
    async with async_session_factory() as session:
        user = User(email=f"evjoin_{uuid.uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        project = Project(
            user_id=user.id,
            title="eval join 测试",
            tone="professional",
            page_count=2,
            theme_id="ivory",
        )
        session.add(project)
        await session.commit()
        yield project.id
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


async def _add_span(
    session,
    trace_id: uuid.UUID,
    *,
    name: str,
    span_kind: str,
    duration_ms: int | None = 500,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    session.add(
        Span(
            trace_id=trace_id,
            name=name,
            span_kind=span_kind,
            status="succeeded",
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    )


@pytest.mark.asyncio
async def test_join_sums_tokens_and_stage_durations(project_id: uuid.UUID) -> None:
    """outline（llm×2 + node）+ deck（llm×2 + task×2 + export×1）→ 聚合正确。"""
    from app.eval.trace_join import TRACE_SOURCE, join_trace_metrics

    async with async_session_factory() as session:
        outline_trace = uuid.uuid4()
        deck_trace = uuid.uuid4()
        session.add(
            Trace(id=outline_trace, kind="outline", status="succeeded", project_id=project_id)
        )
        session.add(
            Trace(
                id=deck_trace,
                kind="deck",
                status="succeeded",
                project_id=project_id,
                linked_trace_id=outline_trace,
            )
        )
        await session.flush()
        # outline 段：2 个 llm 子 span + 2 个 outline.* 节点 span
        await _add_span(
            session, outline_trace, name="outline.prepare", span_kind="node", duration_ms=100
        )
        await _add_span(
            session,
            outline_trace,
            name="llm",
            span_kind="llm",
            duration_ms=800,
            prompt_tokens=1000,
            completion_tokens=200,
        )
        await _add_span(
            session,
            outline_trace,
            name="llm",
            span_kind="llm",
            duration_ms=400,
            prompt_tokens=500,
            completion_tokens=80,
        )
        await _add_span(
            session, outline_trace, name="outline.generate", span_kind="node", duration_ms=300
        )
        # deck 段：每页 task span + 页内 llm span + export 三步 span
        await _add_span(session, deck_trace, name="slide[1]", span_kind="task", duration_ms=900)
        await _add_span(
            session,
            deck_trace,
            name="llm",
            span_kind="llm",
            duration_ms=600,
            prompt_tokens=800,
            completion_tokens=150,
        )
        await _add_span(session, deck_trace, name="slide[2]", span_kind="task", duration_ms=1100)
        await _add_span(
            session,
            deck_trace,
            name="llm",
            span_kind="llm",
            duration_ms=700,
            prompt_tokens=900,
            completion_tokens=180,
        )
        await _add_span(
            session, deck_trace, name="export.quality_check", span_kind="export", duration_ms=50
        )
        await _add_span(
            session, deck_trace, name="export.render", span_kind="export", duration_ms=150
        )
        await _add_span(
            session, deck_trace, name="export.verify", span_kind="export", duration_ms=100
        )
        await session.commit()

        joined = await join_trace_metrics(
            session, outline_trace_id=str(outline_trace), deck_trace_id=str(deck_trace)
        )
        assert joined.prompt_tokens == 1000 + 500 + 800 + 900
        assert joined.completion_tokens == 200 + 80 + 150 + 180
        assert joined.outline_duration_ms == 100 + 300
        assert joined.slide_durations_ms == [900, 1100]
        assert joined.export_duration_ms == 50 + 150 + 100
        assert joined.tokens_source == TRACE_SOURCE

        # 完整报告路径：两题（一题有 trace、一题查不到）→ run 级聚合正确
        report = build_report(
            [
                (
                    "technology/rag",
                    2,
                    _ok_artifacts(
                        "technology/rag", outline=str(outline_trace), deck=str(deck_trace)
                    ),
                    CaseScore(),
                ),
                (
                    "technology/agent",
                    2,
                    _ok_artifacts("technology/agent", outline=None, deck=None),
                    CaseScore(),
                ),
            ]
        )
        await join_report_traces(session, report)
        s = report.summary
        total_prompt = 1000 + 500 + 800 + 900
        total_completion = 200 + 80 + 150 + 180
        # 成功题均值；查不到 trace 的题按 0 参与分母
        assert s.avg_prompt_tokens == pytest.approx(total_prompt / 2)
        assert s.avg_completion_tokens == pytest.approx(total_completion / 2)
        assert s.avg_outline_seconds == pytest.approx((100 + 300) / 2 / 1000)
        assert s.avg_slide_seconds == pytest.approx((900 + 1100) / 2 / 1000)
        assert s.avg_export_seconds == pytest.approx((50 + 150 + 100) / 2 / 1000)
        assert s.token_joined_cases == 1
        row0 = report.rows[0]
        assert row0.tokens_source == TRACE_SOURCE
        assert row0.prompt_tokens == total_prompt
        row1 = report.rows[1]
        assert row1.tokens_source == "unavailable"
        assert row1.prompt_tokens == 0

        await session.execute(delete(Trace).where(Trace.id.in_([outline_trace, deck_trace])))
        await session.commit()


@pytest.mark.asyncio
async def test_join_degrades_when_trace_missing_or_no_tokens(project_id: uuid.UUID) -> None:
    """无 trace_id / 非法 id / trace 不存在 / llm span 无 token → unavailable + 0。"""
    from app.eval.trace_join import TRACE_UNAVAILABLE, join_trace_metrics

    async with async_session_factory() as session:
        missing = await join_trace_metrics(session, outline_trace_id=None, deck_trace_id=None)
        assert missing.tokens_source == TRACE_UNAVAILABLE
        assert missing.prompt_tokens == 0

        # 非法 UUID 与不存在的 trace 同样降级，不抛
        bogus = await join_trace_metrics(
            session, outline_trace_id="not-a-uuid", deck_trace_id=str(uuid.uuid4())
        )
        assert bogus.tokens_source == TRACE_UNAVAILABLE

        # trace 存在但 llm span 未带 token（历史数据）→ unavailable
        trace_id = uuid.uuid4()
        session.add(Trace(id=trace_id, kind="outline", status="failed", project_id=project_id))
        await session.flush()
        await _add_span(
            session,
            trace_id,
            name="llm",
            span_kind="llm",
            duration_ms=100,
            prompt_tokens=None,
            completion_tokens=None,
        )
        await session.commit()

        empty = await join_trace_metrics(
            session, outline_trace_id=str(trace_id), deck_trace_id=None
        )
        assert empty.tokens_source == TRACE_UNAVAILABLE
        assert empty.prompt_tokens == 0

        await session.execute(delete(Trace).where(Trace.id == trace_id))
        await session.commit()


def _ok_artifacts(case_id: str, *, outline: str | None, deck: str | None) -> CaseArtifacts:
    slides = [{"id": f"s{i}", "status": "ready", "title": f"页{i}"} for i in range(2)]
    return CaseArtifacts(
        case_id=case_id,
        ok=True,
        stage="done",
        deck_response={"slides": slides},
        export_succeeded=True,
        elapsed_seconds=30.0,
        total_slides=2,
        outline_trace_id=outline,
        deck_trace_id=deck,
    )
