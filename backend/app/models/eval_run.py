import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

EvalRunStatus = Literal["completed", "failed"]


class EvalRun(Base):
    """一次评测 run 的完整结果（eval#4）。

    run_eval 跑完 report 后直连数据库写入（脚本本就在后端代码库内，
    经 API 中转只多一层认证与校验，无对应收益）。聚合指标放独立列，
    列表页不必解析明细 JSONB；逐题明细与分类均分放 JSONB，
    结构随 report.CaseRow 演进，避免频繁改表。
    """

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 题集版本：compute_cases_version 对题集内容的指纹，run 之间分数可比的前提
    cases_version: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # ---- run 级聚合指标（与 app.eval.report.RunSummary 字段一一对应）----
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    ok_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    success_rate: Mapped[float] = mapped_column(Float, nullable=False)
    failure_rate: Mapped[float] = mapped_column(Float, nullable=False)
    schema_valid_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_scored_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_valid_rate: Mapped[float] = mapped_column(Float, nullable=False)
    pages_met_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_scored_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    pages_met_rate: Mapped[float] = mapped_column(Float, nullable=False)
    export_succeeded_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    # 可空：无 judge / 无文档题成功时该指标不存在
    avg_judge_coverage: Mapped[float | None] = mapped_column(Float)
    avg_judge_score: Mapped[float | None] = mapped_column(Float)
    judge_failed_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_total_numbers: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_fabricated_numbers: Mapped[int] = mapped_column(Integer, nullable=False)
    hallucination_rate: Mapped[float | None] = mapped_column(Float)
    avg_elapsed_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    # 口径：worker 进程埋点不可达，v1 恒 0（见 app/eval/report.py）
    avg_prompt_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    avg_completion_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    total_failed_slides: Mapped[int] = mapped_column(Integer, nullable=False)
    total_slides: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_slide_rate: Mapped[float | None] = mapped_column(Float)
    total_retried_slides: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="completed")

    # ---- 明细（JSONB，结构见 app.eval.persistence.row_to_detail）----
    rows: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    # 四类（technology/business/education/documents）各自的 case 数 / 成功数 / judge 均分
    category_scores: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
