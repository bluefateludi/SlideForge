"""trace 只读查询端点（obs#3）。

trace/span 由 recorder 旁路写入（obs#1/#2）；这里只提供列表、详情与
指标聚合三个 GET。任何登录用户可查：数据不含用户维度的隔离信息。
指标全部在 SQL 侧聚合，不在 Python 里拉全表。

路由顺序注意：/metrics/summary 必须先于 /{trace_id} 注册，
否则 "metrics" 会被当成 trace id 解析。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.db import get_session
from app.models.user import User
from app.observability.cost import compute_cost
from app.observability.models import Span, Trace
from app.schemas.trace import (
    FailureBreakdownItem,
    KindSuccessRate,
    NodeStatItem,
    SpanPublic,
    TraceCostSummary,
    TraceDetail,
    TraceMetricsSummary,
    TracePage,
    TracePublic,
)

router = APIRouter(prefix="/trace", tags=["trace"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _trace_duration_ms_expr():
    """trace 时延（毫秒）：只有 finished_at 收口的 trace 才有值。"""
    started = func.extract("epoch", Trace.started_at)
    finished = func.extract("epoch", Trace.finished_at)
    return (finished - started) * 1000.0


def _trace_public(trace: Trace, duration_ms: float | None) -> TracePublic:
    return TracePublic.model_validate(trace).model_copy(
        update={"duration_ms": None if duration_ms is None else int(duration_ms)}
    )


@router.get("", response_model=TracePage)
async def list_traces(
    session: SessionDep,
    current_user: CurrentUser,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    kind: Annotated[str | None, Query()] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TracePage:
    """按 created_at 倒序分页返回 trace 列表（列表页不拉 spans）。"""
    conditions = []
    if status_filter is not None:
        conditions.append(Trace.status == status_filter)
    if kind is not None:
        conditions.append(Trace.kind == kind)
    if project_id is not None:
        conditions.append(Trace.project_id == project_id)

    total = await session.scalar(select(func.count()).select_from(Trace).where(*conditions))
    result = await session.execute(
        select(Trace, _trace_duration_ms_expr().label("duration_ms"))
        .where(*conditions)
        .order_by(Trace.created_at.desc(), Trace.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [_trace_public(row[0], row.duration_ms) for row in result]
    return TracePage(items=items, total=total or 0)


@router.get("/metrics/summary", response_model=TraceMetricsSummary)
async def get_metrics_summary(
    session: SessionDep,
    current_user: CurrentUser,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> TraceMetricsSummary:
    """时间窗（trace.created_at >= now - days）内的 SQL 侧指标聚合。

    spans 无独立时间戳/项目维度：一律经 trace_id 关联到窗口内 trace。
    """
    window_start = datetime.now(UTC) - timedelta(days=days)
    trace_window = Trace.created_at >= window_start
    window_trace_ids = select(Trace.id).where(trace_window)
    span_in_window = Span.trace_id.in_(window_trace_ids)

    # 1) deck trace 时延分位（只算已收口的）
    latency = _trace_duration_ms_expr()
    p50, p95 = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(latency),
                func.percentile_cont(0.95).within_group(latency),
            ).where(
                trace_window,
                Trace.kind == "deck",
                Trace.finished_at.is_not(None),
            )
        )
    ).one()

    # 2) trace 成功率（按 kind 分组）
    kind_rows = (
        await session.execute(
            select(
                Trace.kind,
                func.count().label("total"),
                func.count().filter(Trace.status == "succeeded").label("succeeded"),
            )
            .where(trace_window)
            .group_by(Trace.kind)
            .order_by(Trace.kind)
        )
    ).all()
    trace_success = [
        KindSuccessRate(
            kind=row.kind,
            total=row.total,
            succeeded=row.succeeded,
            success_rate=round(row.succeeded / row.total, 3) if row.total else 0.0,
        )
        for row in kind_rows
    ]

    # 3) slide 级成功率：task span 且 name like 'slide[%]'
    slide_total, slide_succeeded = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(Span.status == "succeeded").label("succeeded"),
            ).where(span_in_window, Span.span_kind == "task", Span.name.like("slide[%]"))
        )
    ).one()
    slide_success_rate = round(slide_succeeded / slide_total, 3) if slide_total else None

    # 4) llm token 平均与总和
    avg_prompt, avg_completion, total_prompt, total_completion = (
        await session.execute(
            select(
                func.avg(Span.prompt_tokens),
                func.avg(Span.completion_tokens),
                func.coalesce(func.sum(Span.prompt_tokens), 0),
                func.coalesce(func.sum(Span.completion_tokens), 0),
            ).where(span_in_window, Span.span_kind == "llm")
        )
    ).one()

    # 5) failure_breakdown：失败 span 按 error_code 分组（无码归 unknown），按量降序
    error_code_group = func.coalesce(Span.error_code, "unknown").label("error_code")
    failed_total = await session.scalar(
        select(func.count()).where(span_in_window, Span.status == "failed")
    )
    failure_rows = (
        await session.execute(
            select(error_code_group, func.count().label("count"))
            .where(span_in_window, Span.status == "failed")
            .group_by(error_code_group)
            .order_by(func.count().desc())
        )
    ).all()
    breakdown = [
        FailureBreakdownItem(
            error_code=row.error_code,
            count=row.count,
            ratio=round(row.count / failed_total, 3) if failed_total else 0.0,
        )
        for row in failure_rows
    ]

    # 6) node_stats：按 name 聚合失败率与耗时（token 只算 llm span，否则为 null）
    node_rows = (
        await session.execute(
            select(
                Span.name,
                func.count().label("total"),
                func.count().filter(Span.status == "failed").label("failed"),
                func.avg(Span.duration_ms).label("avg_ms"),
                func.avg(case((Span.span_kind == "llm", Span.completion_tokens))).label(
                    "avg_completion_tokens"
                ),
            )
            .where(span_in_window)
            .group_by(Span.name)
            .order_by(Span.name)
        )
    ).all()
    node_stats = [
        NodeStatItem(
            name=row.name,
            total=row.total,
            failed=row.failed,
            failure_rate=round(row.failed / row.total, 3) if row.total else 0.0,
            avg_ms=round(row.avg_ms, 1) if row.avg_ms is not None else None,
            avg_completion_tokens=(
                round(row.avg_completion_tokens, 1)
                if row.avg_completion_tokens is not None
                else None
            ),
        )
        for row in node_rows
    ]

    def round1(value: float | None) -> float | None:
        return round(value, 1) if value is not None else None

    return TraceMetricsSummary(
        days=days,
        deck_duration_p50_ms=round1(p50),
        deck_duration_p95_ms=round1(p95),
        trace_success=trace_success,
        slide_total=slide_total,
        slide_succeeded=slide_succeeded,
        slide_success_rate=slide_success_rate,
        avg_prompt_tokens=round1(avg_prompt),
        avg_completion_tokens=round1(avg_completion),
        total_prompt_tokens=total_prompt or 0,
        total_completion_tokens=total_completion or 0,
        failure_breakdown=breakdown,
        node_stats=node_stats,
    )


@router.get("/{trace_id}", response_model=TraceDetail)
async def get_trace(
    trace_id: Annotated[uuid.UUID, Path()],
    session: SessionDep,
    current_user: CurrentUser,
) -> TraceDetail:
    """单条 trace 详情：本体 + spans 全量（按 started_at, id 排序）。"""
    trace = await session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace 不存在")
    spans = list(
        (
            await session.execute(
                select(Span).where(Span.trace_id == trace_id).order_by(Span.started_at, Span.id)
            )
        )
        .scalars()
        .all()
    )
    duration_ms = None
    if trace.finished_at is not None and trace.started_at is not None:
        duration_ms = (trace.finished_at - trace.started_at).total_seconds() * 1000
    return TraceDetail(
        trace=_trace_public(trace, duration_ms),
        spans=[SpanPublic.model_validate(s) for s in spans],
        cost=_trace_cost(spans),
    )


def _trace_cost(spans: list[Span]) -> TraceCostSummary:
    """spans 已全量在手，成本直接在内存里折算（obs#9）。

    计费口径与 eval 侧 trace_join 一致：llm token 求和 + image.ai
    succeeded 计张数，单价走 env（见 app/observability/cost.py）。
    """
    prompt = sum(s.prompt_tokens or 0 for s in spans if s.span_kind == "llm")
    completion = sum(s.completion_tokens or 0 for s in spans if s.span_kind == "llm")
    ai_images = sum(
        1
        for s in spans
        if s.span_kind == "image" and s.name == "image.ai" and s.status == "succeeded"
    )
    cost = compute_cost(prompt, completion, ai_images)
    return TraceCostSummary(
        configured=cost.configured,
        prompt_tokens=prompt,
        completion_tokens=completion,
        ai_image_count=ai_images,
        llm_cost=round(cost.llm_cost, 4),
        image_cost=round(cost.image_cost, 4),
        total_cost=round(cost.total_cost, 4),
    )
