"""trace 查询响应模型（obs#3）：列表行轻量，详情附全量 spans。

语义注意（obs#2 的取舍）：deck 页失败时外层 slide[N] task span 可能以
succeeded 收口，失败明细在节点级 span 与 trace.error_code 上——消费端
渲染状态必须以每个 span 自身的 status 为准，不从父 span 推断。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TraceStatus = Literal["running", "succeeded", "failed", "cancelled"]
SpanKind = Literal["task", "node", "llm", "export", "image", "tool"]
SpanStatus = Literal["running", "succeeded", "failed"]


class TracePublic(BaseModel):
    """列表行：列表页不拉 spans，只看 trace 一层。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # kind 放宽为 str：表里可能先出现新 kind（如 ai_edit），按 Literal 收紧会让列表端点整体 500
    kind: str
    status: TraceStatus
    project_id: uuid.UUID
    job_id: str | None = None
    linked_trace_id: uuid.UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = Field(
        default=None, description="finished_at - started_at，未收口为空"
    )


class TracePage(BaseModel):
    """分页信封：字段照 eval 列表页惯例（items/total），补 limit/offset 便于前端续拉。"""

    items: list[TracePublic]
    total: int = Field(description="筛选后的总条数，不受 limit/offset 影响")


class SpanPublic(BaseModel):
    """详情页瀑布的单行：attributes（repair_round 等）原样透传。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    trace_id: uuid.UUID
    parent_span_id: int | None = None
    name: str
    span_kind: SpanKind
    status: SpanStatus
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    model: str | None = None
    purpose: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    attributes: dict[str, Any] | None = None


class TraceDetail(BaseModel):
    """详情：trace 本体 + spans 全量（按 started_at, id 排序，树由前端自建）。"""

    trace: TracePublic
    spans: list[SpanPublic]
    # 成本估算（obs#9）：由 spans 的 llm token 与 image.ai 张数按 env 单价
    # 折算；单价未配置时 configured=False，前端不展示成本卡
    cost: TraceCostSummary


class TraceCostSummary(BaseModel):
    """单条 trace 的成本估算（人民币元，展示精度 4 位小数）。"""

    configured: bool = Field(description="任一单价已配置即为 true")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ai_image_count: int = Field(default=0, description="计费张数：image.ai 且 succeeded")
    llm_cost: float = 0.0
    image_cost: float = 0.0
    total_cost: float = 0.0


class KindSuccessRate(BaseModel):
    """单 kind 的 trace 成功率。

    kind 用 str 而非 Literal：traces 表会先于 schema 出现新 kind
    （如 ai_edit），响应模型按已知枚举收紧会让整个端点 500。
    """

    kind: str
    succeeded: int
    total: int
    success_rate: float


class FailureBreakdownItem(BaseModel):
    """失败来源占比：按失败 span 的 error_code 分组。"""

    error_code: str
    count: int
    ratio: float = Field(description="占失败 span 总数的比例，保留 3 位小数")


class NodeStatItem(BaseModel):
    """节点健康度：按 span.name 聚合的失败率与耗时。"""

    name: str
    total: int
    failed: int
    failure_rate: float
    avg_ms: float | None = None
    avg_completion_tokens: float | None = Field(
        default=None, description="仅 llm span 参与；无样本为 null"
    )


class TraceMetricsSummary(BaseModel):
    """时间窗（trace.created_at >= now - days）内的观测聚合。"""

    days: int
    deck_duration_p50_ms: float | None = Field(
        default=None, description="deck trace 时延 P50；窗口内无已收口 trace 为 null"
    )
    deck_duration_p95_ms: float | None = None
    trace_success: list[KindSuccessRate]
    slide_total: int = 0
    slide_succeeded: int = 0
    slide_success_rate: float | None = Field(
        default=None, description="slide[N] task span 口径；窗口内无样本为 null"
    )
    # 修复收敛（obs/10）：首过率 = 未进 repair 的页占比；修复成功率 = 进过
    # repair 的页最终 succeeded 占比。修复轮上限 1，口径见 api/v1/trace.py
    slide_first_pass_rate: float | None = Field(
        default=None, description="无 slide.repair 子 span 的页占比；无样本为 null"
    )
    slide_repair_total: int = Field(default=0, description="进过修复轮的页数")
    slide_repair_success_rate: float | None = Field(
        default=None, description="修复后最终 succeeded 的页数 / 修复页数"
    )
    avg_prompt_tokens: float | None = Field(
        default=None, description="llm span 平均；无样本为 null"
    )
    avg_completion_tokens: float | None = None
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    failure_breakdown: list[FailureBreakdownItem]
    node_stats: list[NodeStatItem]


__all__ = [
    "FailureBreakdownItem",
    "KindSuccessRate",
    "NodeStatItem",
    "SpanPublic",
    "TraceCostSummary",
    "TraceDetail",
    "TraceMetricsSummary",
    "TracePage",
    "TracePublic",
]
