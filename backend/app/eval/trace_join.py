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

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
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
    # 成本折算结果（人民币元）；单价未配置时各项为 0 且 configured=False
    llm_cost: float = 0.0
    image_cost: float = 0.0
    total_cost: float = 0.0
    cost_configured: bool = False


def _empty() -> TraceJoinResult:
    return TraceJoinResult(tokens_source=TRACE_UNAVAILABLE)


async def join_trace_metrics(
    session: AsyncSession,
    *,
    outline_trace_id: str | uuid.UUID | None,
    deck_trace_id: str | uuid.UUID | None,
    settings: Settings | None = None,
) -> TraceJoinResult:
    """按两段 trace_id 聚合 spans。任何异常吞掉、按查不到降级。"""
    ids: list[uuid.UUID] = []
    for raw in (outline_trace_id, deck_trace_id):
        if raw is None or raw == "":
            continue
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    if not ids:
        return _empty()

    try:
        rows = (
            (
                await session.execute(
                    select(
                        Span.trace_id,
                        Span.name,
                        Span.span_kind,
                        Span.status,
                        Span.duration_ms,
                        Span.prompt_tokens,
                        Span.completion_tokens,
                    ).where(Span.trace_id.in_(ids))
                )
            )
            .all()
        )
    except Exception:
        return _empty()

    result = TraceJoinResult()
    seen_token = False
    for _trace_id, name, span_kind, status, duration_ms, prompt, completion in rows:
        duration = duration_ms or 0
        if span_kind == "llm":
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

    result.tokens_source = TRACE_SOURCE if seen_token else TRACE_UNAVAILABLE
    result.slide_durations_ms.sort()
    cost = compute_cost(
        result.prompt_tokens,
        result.completion_tokens,
        result.ai_image_count,
        settings=settings,
    )
    result.llm_cost = cost.llm_cost
    result.image_cost = cost.image_cost
    result.total_cost = cost.total_cost
    result.cost_configured = cost.configured
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
    return detail


__all__ = [
    "TRACE_SOURCE",
    "TRACE_UNAVAILABLE",
    "TraceJoinResult",
    "join_trace_metrics",
    "merge_join_into_detail",
]
