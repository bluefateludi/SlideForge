"""评测报告 → eval_runs 落库（eval#4）。

report.py 的 EvalReport（run 级聚合 + 逐题行）在这里转换为 EvalRun
模型并写入数据库：纯转换函数（report_to_run / row_to_detail /
build_category_scores / compute_cases_version）不触库，save_run 接受
外部 session（脚本直连与测试共用同一条真实写入路径）。

obs#4：写入前按各题 trace_id join spans 表，把真实 token 与分段耗时
并进明细行，再重新聚合 run 级指标（token 平均口径见 trace_join.py）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.eval.cases import CATEGORIES, EvalCase
from app.eval.report import CaseRow, EvalReport, aggregate
from app.eval.trace_join import join_trace_metrics
from app.models.eval_run import EvalRun

# 逐题明细 JSONB 的键白名单：报告行里 rest 字段属于控制台呈现，
# 不入库；新增字段在此登记即可，无需改表。
_DETAIL_KEYS = (
    "case_id",
    "ok",
    "stage",
    "error",
    "ready_pages",
    "expected_pages",
    "pages_met",
    "schema_valid",
    "export_succeeded",
    "fabricated_numbers",
    "total_numbers",
    "judge_coverage",
    "judge_score",
    "judge_error",
    "hallucination_rate",
    "failed_slide_seen",
    "retried_slides",
    "total_slides",
    "elapsed_seconds",
    "prompt_tokens",
    "completion_tokens",
    "tokens_source",
    "measurement_complete",
    "measurement_issues",
    "cost_configured",
    "observed_models",
    "outline_trace_id",
    "deck_trace_id",
    "outline_duration_ms",
    "slide_durations_ms",
    "export_duration_ms",
    "ai_image_count",
    "cost",
    "repair_spans",
    "repaired_succeeded",
)


def compute_cases_version(cases: list[EvalCase]) -> str:
    """题集 v2 身份：语义字段和材料原始字节；不受加载顺序或绝对路径影响。"""
    payload = []
    for case in sorted(cases, key=lambda item: item.id):
        source_hash = None
        if case.category == "documents":
            if case.source_path is None:
                raise ValueError(f"{case.id} 材料尚未加载")
            try:
                source_hash = hashlib.sha256(case.source_path.read_bytes()).hexdigest()
            except OSError as error:
                raise ValueError(f"{case.id} 材料无法读取：{case.source}") from error
        payload.append(
            {
                "id": case.id,
                "input": case.input,
                "expected_pages": case.expected_pages,
                "requirements": case.requirements,
                "category": case.category,
                "source": case.source.as_posix() if case.source else None,
                "source_sha256": source_hash,
            }
        )
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"cases-v2-{digest[:12]}"


def row_to_detail(row: CaseRow, category: str) -> dict[str, Any]:
    """报告行 → 明细 JSONB 条目（按 _DETAIL_KEYS 白名单裁剪，附类别）。"""
    data = row.model_dump(mode="json")
    return {"category": category, **{key: data[key] for key in _DETAIL_KEYS}}


def build_category_scores(details: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """逐题明细 → 四类各自的小结。

    四类恒占位（未出现的类别 total_cases=0），前端四列无需判空；
    avg_judge_score 只对有 judge 分的题取均值，无分时为 None。
    """
    scores: dict[str, dict[str, Any]] = {
        category: {"total_cases": 0, "ok_cases": 0, "avg_judge_score": None, "success_rate": 0.0}
        for category in CATEGORIES
    }
    for detail in details:
        bucket = scores[detail["category"]]
        bucket["total_cases"] += 1
        if detail["ok"]:
            bucket["ok_cases"] += 1
        if bucket["total_cases"]:
            bucket["success_rate"] = bucket["ok_cases"] / bucket["total_cases"]
    for category, bucket in scores.items():
        judged = [
            d["judge_score"]
            for d in details
            if d["category"] == category and d.get("judge_score") is not None
        ]
        if judged:
            bucket["avg_judge_score"] = sum(judged) / len(judged)
    return scores


async def join_report_traces(session: AsyncSession, report: EvalReport) -> None:
    """就地补齐 report 里各题的 token / 分段耗时（obs#4）。

    每题按 outline/deck trace_id 查 spans 聚合（查不到按 0 并标注
    tokens_source）；补完后重新跑 aggregate，让 run 级 token 均值与
    分段均耗基于真实数据。join 内部吞掉查询异常，本函数不抛。
    """
    for row in report.rows:
        joined = await join_trace_metrics(
            session,
            outline_trace_id=row.outline_trace_id,
            deck_trace_id=row.deck_trace_id,
        )
        row.prompt_tokens = joined.prompt_tokens
        row.completion_tokens = joined.completion_tokens
        row.tokens_source = joined.tokens_source
        row.outline_duration_ms = joined.outline_duration_ms
        row.slide_durations_ms = joined.slide_durations_ms
        row.export_duration_ms = joined.export_duration_ms
        row.ai_image_count = joined.ai_image_count
        row.cost = joined.total_cost
        row.measurement_complete = joined.measurement_complete
        row.measurement_issues = joined.measurement_issues
        row.cost_configured = joined.cost_configured
        row.observed_models = joined.observed_models
        row.repair_spans = joined.repair_spans
        row.repaired_succeeded = joined.repaired_succeeded
    report.summary = aggregate(report.rows)


def report_to_run(
    report: EvalReport,
    *,
    cases_version: str,
    categories: dict[str, str] | None = None,
    note: str = "",
    status: str = "completed",
) -> EvalRun:
    """EvalReport → EvalRun 模型（纯函数，不触库）。

    categories：case_id → 类别 映射；缺省时按 id 前缀目录推导
    （cases.py 保证 id 形如「类别/文件名」）。
    """
    categories = categories or {}
    details = [
        row_to_detail(row, categories.get(row.case_id, row.case_id.split("/")[0]))
        for row in report.rows
    ]
    for detail in details:
        detail["run_context"] = report.run_context
    s = report.summary
    run = EvalRun(
        cases_version=cases_version,
        total_cases=s.total_cases,
        ok_cases=s.ok_cases,
        success_rate=s.success_rate,
        failure_rate=s.failure_rate,
        schema_valid_cases=s.schema_valid_cases,
        schema_scored_cases=s.schema_scored_cases,
        schema_valid_rate=s.schema_valid_rate,
        pages_met_cases=s.pages_met_cases,
        pages_scored_cases=s.pages_scored_cases,
        pages_met_rate=s.pages_met_rate,
        export_succeeded_cases=s.export_succeeded_cases,
        avg_judge_coverage=s.avg_judge_coverage,
        avg_judge_score=s.avg_judge_score,
        judge_failed_cases=s.judge_failed_cases,
        doc_total_numbers=s.doc_total_numbers,
        doc_fabricated_numbers=s.doc_fabricated_numbers,
        hallucination_rate=s.hallucination_rate,
        avg_elapsed_seconds=s.avg_elapsed_seconds,
        avg_prompt_tokens=s.avg_prompt_tokens,
        avg_completion_tokens=s.avg_completion_tokens,
        avg_cost=s.avg_cost,
        total_cost=s.total_cost,
        total_failed_slides=s.total_failed_slides,
        total_slides=s.total_slides,
        retry_slide_rate=s.retry_slide_rate,
        total_retried_slides=s.total_retried_slides,
        status=status,
        rows=details,
        category_scores=build_category_scores(details),
        note=note,
    )
    if report.run_context.get("source_run_id"):
        import uuid

        run.id = uuid.UUID(report.run_context["source_run_id"])
    return run


async def save_run(session: AsyncSession, run: EvalRun) -> EvalRun:
    """写入一条 run 记录并提交；失败由调用方决定如何呈现。"""
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


__all__ = [
    "build_category_scores",
    "compute_cases_version",
    "join_report_traces",
    "report_to_run",
    "row_to_detail",
    "save_run",
]
