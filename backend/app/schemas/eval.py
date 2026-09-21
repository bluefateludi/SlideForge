"""评测 run 查询响应模型（eval#4）：列表行轻量，详情附明细与分类均分。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.eval.cases import CATEGORIES

EvalRunStatus = Literal["completed", "failed"]


class EvalCategoryScore(BaseModel):
    """单类别小结：从逐题明细推导后随 run 一起存储。"""

    total_cases: int = 0
    ok_cases: int = 0
    success_rate: float = 0.0
    # 无 judge 分的类别（全失败 / 未配 LLM）为 None
    avg_judge_score: float | None = None


class EvalCaseRow(BaseModel):
    """逐题明细条目：rows JSONB 的元素结构（入库白名单见 app.eval.persistence）。"""

    case_id: str
    category: Literal[tuple(CATEGORIES)]  # type: ignore[valid-type]
    ok: bool
    stage: str
    error: str | None = None
    ready_pages: int = 0
    expected_pages: int = 0
    pages_met: bool = False
    schema_valid: bool | None = None
    judge_coverage: float | None = None
    judge_score: float | None = None
    judge_error: str | None = None
    hallucination_rate: float | None = None
    failed_slide_seen: int = 0
    retried_slides: int = 0
    total_slides: int = 0
    elapsed_seconds: float = 0.0
    # obs#4：trace join spans 聚合的真实 token 与分段耗时
    # （毫秒；slide_durations_ms 为每页 task span 耗时列表）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # "trace"：spans 聚合值；"unavailable"：查不到 trace（按 0 展示）
    tokens_source: Literal["trace", "unavailable"] = "unavailable"
    outline_trace_id: uuid.UUID | None = None
    deck_trace_id: uuid.UUID | None = None
    outline_duration_ms: int = 0
    slide_durations_ms: list[int] = Field(default_factory=list)
    export_duration_ms: int = 0


class EvalRunPublic(BaseModel):
    """列表行：只含聚合指标列，不解析明细 JSONB。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cases_version: str
    created_at: datetime
    status: EvalRunStatus
    note: str
    total_cases: int
    ok_cases: int
    success_rate: float
    failure_rate: float
    schema_valid_rate: float
    schema_valid_cases: int
    schema_scored_cases: int
    pages_met_rate: float
    pages_met_cases: int
    pages_scored_cases: int
    avg_judge_coverage: float | None
    avg_judge_score: float | None
    hallucination_rate: float | None
    doc_fabricated_numbers: int
    doc_total_numbers: int
    avg_elapsed_seconds: float
    avg_prompt_tokens: float
    avg_completion_tokens: float
    retry_slide_rate: float | None
    total_failed_slides: int
    total_slides: int
    total_retried_slides: int


class EvalRunDetail(EvalRunPublic):
    """详情：在列表行基础上补逐题明细与分类均分。"""

    rows: list[EvalCaseRow]
    category_scores: dict[str, EvalCategoryScore] = Field(
        description="四类（technology/business/education/documents）各自的小结"
    )


__all__ = [
    "EvalCaseRow",
    "EvalCategoryScore",
    "EvalRunDetail",
    "EvalRunPublic",
]
