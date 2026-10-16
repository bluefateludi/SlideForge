"""按 trace_id 聚合 spans，反哺评测报告（obs#4）。

runner 记下 outline/deck 生成 202 响应的 trace_id；本模块在报告组装前
查 spans 表，把每题的真实 token 与分段耗时并进明细行：
- token：span_kind='llm' 的 prompt/completion_tokens 求和（跨两段 trace）
- 分段耗时：outline 段 = name like 'outline.%' 的 duration_ms 和；
  deck 段 = span_kind='task' 的每页 duration_ms（列表，按 position 排序）；
  export 段 = name like 'export.%' 的 duration_ms 和（挂在 deck trace 下）
- 成本（obs#9）：token 与 AI 生图张数按 env 单价折算——计费张数 =
  image.ai 且 succeeded 的 span 数，见 app/observability/cost.py
查询失败 / trace 不存在 / llm span 无 token 时按「查不到」降级：
数值回退 0、tokens_source='unavailable'，绝不让 join 失败炸了报告。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.observability.cost import compute_cost
from app.observability.models import Span

TRACE_SOURCE = "trace"
TRACE_UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class TraceJoinResult:
    """单题的 spans 聚合结果（并进 CaseRow 明细）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 分段耗时（毫秒）：大纲各节点 span 之和
    outline_duration_ms: int = 0
    # 每页 task span 耗时（毫秒，列表）；失败/重试页都会各占一条
    slide_durations_ms: list[int] = field(default_factory=list)
    export_duration_ms: int = 0
    # "trace"：至少一条 llm span 带回了 token；"unavailable"：查不到
    # （无 trace_id / 查询失败 / llm span 均无 token），token 数按 0 展示
    tokens_source: str = TRACE_UNAVAILABLE
    # 计费 AI 生图张数（image.ai 且 succeeded；obs#9）
    ai_image_count: int = 0
    repair_spans: int = 0
    repaired_succeeded: int = 0
    # 成本折算结果（人民币元）；单价未配置时各项为 0 且 configured=False
    llm_cost: float = 0.0
    image_cost: float = 0.0
    total_cost: float = 0.0
    cost_configured: bool = False
    measurement_complete: bool = False
    measurement_issues: list[str] = field(default_factory=list)
    observed_models: list[str] = field(default_factory=list)


def _empty(reason: str) -> TraceJoinResult:
    return TraceJoinResult(tokens_source=TRACE_UNAVAILABLE, measurement_issues=[reason])


async def join_trace_metrics(
    session: AsyncSession,
    *,
    outline_trace_id: str | uuid.UUID | None,
    deck_trace_id: str | uuid.UUID | None,
    settings: Settings | None = None,
) -> TraceJoinResult:
    """按两段 trace_id 聚合 spans。任何异常吞掉、按查不到降级。"""
    ids: list[uuid.UUID] = []
    issues: set[str] = set()
    for raw in (outline_trace_id, deck_trace_id):
        if raw is None or raw == "":
            issues.add("missing_trace_id")
            continue
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            issues.add("invalid_trace_id")
            continue
    if not ids:
        return _empty("missing_trace_id")

    try:
        rows = (
            await session.execute(
                select(
                    Span.trace_id,
                    Span.name,
                    Span.span_kind,
                    Span.status,
                    Span.duration_ms,
                    Span.prompt_tokens,
                    Span.completion_tokens,
                    Span.model,
                ).where(Span.trace_id.in_(ids))
            )
        ).all()
    except Exception:
        return _empty("query_error")

    result = TraceJoinResult()
    seen_token = False
    traces_with_usage: set[uuid.UUID] = set()
    models: set[str] = set()
    for trace_id, name, span_kind, status, duration_ms, prompt, completion, model in rows:
        duration = duration_ms or 0
        if span_kind == "llm":
            if model:
                models.add(model)
            if prompt is None or completion is None:
                issues.add("incomplete_llm_usage")
            elif any(not math.isfinite(value) or value < 0 for value in (prompt, completion)):
                issues.add("invalid_usage")
            else:
                traces_with_usage.add(trace_id)
            result.prompt_tokens += prompt or 0
            result.completion_tokens += completion or 0
            if prompt is not None or completion is not None:
                seen_token = True
        if name.startswith("outline."):
            result.outline_duration_ms += duration
        if span_kind == "task":
            result.slide_durations_ms.append(duration)
        if name.startswith("export."):
            result.export_duration_ms += duration
        if span_kind == "image" and name == "image.ai" and status == "succeeded":
            result.ai_image_count += 1

    if not seen_token:
        issues.add("missing_llm_usage")
    if traces_with_usage != set(ids):
        issues.add("partial_trace_usage")
    result.observed_models = sorted(models)
    result.tokens_source = TRACE_SOURCE if seen_token else TRACE_UNAVAILABLE
    result.slide_durations_ms.sort()
    cfg = settings or get_settings()
    cost = compute_cost(
        result.prompt_tokens,
        result.completion_tokens,
        result.ai_image_count,
        settings=cfg,
    )
    result.llm_cost = cost.llm_cost
    result.image_cost = cost.image_cost
    result.total_cost = cost.total_cost
    prices = (
        cfg.llm_price_per_mtok_prompt,
        cfg.llm_price_per_mtok_completion,
        cfg.image_price_per_unit,
    )
    result.cost_configured = (
        all(math.isfinite(value) and value >= 0 for value in prices)
        and cfg.llm_price_per_mtok_prompt > 0
        and cfg.llm_price_per_mtok_completion > 0
        and (result.ai_image_count == 0 or cfg.image_price_per_unit > 0)
    )
    if not result.cost_configured:
        issues.add("price_not_configured")
    result.measurement_issues = sorted(issues)
    result.measurement_complete = not issues
    return result


def merge_join_into_detail(detail: dict, join: TraceJoinResult) -> dict:
    """把 join 结果并进 rows 明细条目（就地改、返回同一 dict）。

    键名与 CaseRow 新字段一一对应；stage 级均耗是 run 级汇总口径，
    不在单题明细里重复。
    """
    detail["prompt_tokens"] = join.prompt_tokens
    detail["completion_tokens"] = join.completion_tokens
    detail["outline_duration_ms"] = join.outline_duration_ms
    detail["slide_durations_ms"] = join.slide_durations_ms
    detail["export_duration_ms"] = join.export_duration_ms
    detail["tokens_source"] = join.tokens_source
    detail["ai_image_count"] = join.ai_image_count
    detail["cost"] = join.total_cost
    detail["measurement_complete"] = join.measurement_complete
    detail["measurement_issues"] = join.measurement_issues
    detail["cost_configured"] = join.cost_configured
    detail["observed_models"] = join.observed_models
    return detail


__all__ = [
    "TRACE_SOURCE",
    "TRACE_UNAVAILABLE",
    "TraceJoinResult",
    "join_trace_metrics",
    "merge_join_into_detail",
]
