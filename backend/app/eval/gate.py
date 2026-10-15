"""可信回归基线：格式、样本与数据完整性先于数值比较。"""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.paths import REPO_ROOT
from app.eval.report import CaseRow, EvalReport, RunSummary
from app.models.eval_run import EvalRun

BASELINE_PATH = REPO_ROOT / "backend" / "eval" / "baseline.json"
JUDGE_SCORE_FLOOR_DELTA = 0.5
RATIO_LIMIT = 1.3


class MetricRule(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    direction: Literal["min", "max_ratio"]
    tolerance: float = Field(ge=0)


DEFAULT_RULES = {
    **{
        name: MetricRule(direction="min", tolerance=0)
        for name in (
            "success_rate",
            "schema_valid_rate",
            "export_succeeded_cases",
            "pages_met_rate",
        )
    },
    "avg_judge_score": MetricRule(direction="min", tolerance=JUDGE_SCORE_FLOOR_DELTA),
    "avg_judge_coverage": MetricRule(direction="min", tolerance=0),
    **{
        name: MetricRule(direction="max_ratio", tolerance=0.3)
        for name in (
            "avg_prompt_tokens",
            "avg_completion_tokens",
            "avg_cost",
            "total_cost",
            "p95_elapsed_seconds",
        )
    },
}
DIAGNOSTIC_METRICS = ("hallucination_rate", "retry_slide_rate", "total_failed_slides")
_RATE_METRICS = {
    "success_rate",
    "schema_valid_rate",
    "pages_met_rate",
    "avg_judge_coverage",
    "hallucination_rate",
    "retry_slide_rate",
}


class Prices(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    prompt_per_mtok: float = Field(gt=0)
    completion_per_mtok: float = Field(gt=0)
    image_per_unit: float = Field(ge=0)


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_run_id: uuid.UUID
    code_revision: str = Field(min_length=7)
    generation_models: list[str] = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    prices: Prices
    # 明确是当次 harness 配置；不是对远端 Worker 配置的推断。
    runtime: dict[str, Any] = Field(min_length=1)


class Samples(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_cases: int = Field(gt=0)
    schema_cases: int = Field(ge=0)
    page_cases: int = Field(ge=0)
    judge_cases: int = Field(ge=0)
    trace_cases: int = Field(ge=0)
    document_cases: int = Field(ge=0)
    numeric_findings: int = Field(ge=0)

    @model_validator(mode="after")
    def complete(self):
        for name in ("schema_cases", "page_cases", "judge_cases", "trace_cases"):
            if getattr(self, name) != self.total_cases:
                raise ValueError(f"{name} 样本不完整")
        if self.document_cases > self.total_cases:
            raise ValueError("文档题数超过总题数")
        return self


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    format_version: Literal[2]
    status: Literal["ready"]
    cases_version: str = Field(min_length=1)
    created_at: datetime
    provenance: Provenance
    samples: Samples
    metrics: dict[str, float | None]
    rules: dict[str, MetricRule]

    @model_validator(mode="after")
    def valid_contract(self):
        required = set(DEFAULT_RULES) | set(DIAGNOSTIC_METRICS)
        if set(self.metrics) != required or set(self.rules) != set(DEFAULT_RULES):
            raise ValueError("基线指标或规则不完整")
        for name, value in self.metrics.items():
            if value is None:
                if name in DEFAULT_RULES:
                    raise ValueError(f"{name} 指标缺失")
                continue
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 指标必须为非负有限数")
            if name in _RATE_METRICS and value > 1:
                raise ValueError(f"{name} 必须在 0 到 1 之间")
        if self.metrics["p95_elapsed_seconds"] <= 0:
            raise ValueError("P95 耗时必须为正有限数")
        if self.metrics["avg_judge_score"] > 10:
            raise ValueError("内容评分超过 10")
        if self.metrics["export_succeeded_cases"] > self.samples.total_cases:
            raise ValueError("导出成功数超过题目数")
        for name, rule in self.rules.items():
            if rule.direction != DEFAULT_RULES[name].direction:
                raise ValueError(f"{name} 规则方向不正确")
        return self


class GateMetricCheck(BaseModel):
    name: str
    rule: str
    baseline: float | None
    current: float | None
    passed: bool
    skipped: bool = False
    detail: str = ""


class GateReport(BaseModel):
    passed: bool
    baseline_cases_version: str | None
    current_cases_version: str
    checks: list[GateMetricCheck] = Field(default_factory=list)
    reason: str = ""


def p95_elapsed(rows: list[Any]) -> float | None:
    values = []
    for row in rows:
        elapsed = row.get("elapsed_seconds") if isinstance(row, dict) else row.elapsed_seconds
        if elapsed is not None:
            values.append(float(elapsed))
    if not values:
        return None
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("耗时必须为正有限数")
    values.sort()
    return values[math.ceil(0.95 * len(values)) - 1]


def collect_gate_metrics(report: EvalReport) -> dict[str, float | None]:
    s = report.summary
    return {
        "success_rate": s.success_rate,
        "schema_valid_rate": s.schema_valid_rate if s.schema_scored_cases else None,
        "export_succeeded_cases": float(s.export_succeeded_cases),
        "pages_met_rate": s.pages_met_rate if s.pages_scored_cases else None,
        "avg_judge_score": s.avg_judge_score,
        "avg_judge_coverage": s.avg_judge_coverage,
        "avg_prompt_tokens": s.avg_prompt_tokens,
        "avg_completion_tokens": s.avg_completion_tokens,
        "avg_cost": s.avg_cost,
        "total_cost": s.total_cost,
        "p95_elapsed_seconds": p95_elapsed(report.rows),
        "hallucination_rate": s.hallucination_rate,
        "retry_slide_rate": s.retry_slide_rate,
        "total_failed_slides": float(s.total_failed_slides),
    }


def report_integrity_issues(report: EvalReport) -> list[str]:
    issues = []
    if not report.rows or len(report.rows) != report.summary.total_cases:
        issues.append("报告题数不完整")
    if len({row.case_id for row in report.rows}) != len(report.rows):
        issues.append("报告有重复题目")
    for row in report.rows:
        if (
            not row.measurement_complete
            or row.tokens_source != "trace"
            or not row.cost_configured
            or row.measurement_issues
        ):
            issues.append(f"{row.case_id}: Trace/成本数据不完整")
        for value in (row.prompt_tokens, row.completion_tokens, row.cost, row.elapsed_seconds):
            if not math.isfinite(value) or value < 0:
                issues.append(f"{row.case_id}: 测量值非法")
        if row.elapsed_seconds <= 0:
            issues.append(f"{row.case_id}: 耗时缺失")
        if row.schema_valid is None or row.expected_pages <= 0:
            issues.append(f"{row.case_id}: 结构/页数样本不完整")
        if (
            row.judge_score is None
            or row.judge_coverage is None
            or row.judge_error
            or not math.isfinite(row.judge_score)
            or not math.isfinite(row.judge_coverage)
        ):
            issues.append(f"{row.case_id}: 评审样本不完整")
        if row.case_id.startswith("documents/") and row.total_numbers is None:
            issues.append(f"{row.case_id}: 文档数字核对样本缺失")
    return issues


def _samples(report: EvalReport) -> Samples:
    return Samples(
        total_cases=len(report.rows),
        schema_cases=sum(row.schema_valid is not None for row in report.rows),
        page_cases=sum(row.expected_pages > 0 for row in report.rows),
        judge_cases=sum(
            row.judge_score is not None and row.judge_coverage is not None and not row.judge_error
            for row in report.rows
        ),
        trace_cases=sum(row.measurement_complete for row in report.rows),
        document_cases=sum(row.case_id.startswith("documents/") for row in report.rows),
        numeric_findings=sum(row.total_numbers or 0 for row in report.rows),
    )


def build_baseline(report: EvalReport, *, cases_version: str) -> dict[str, Any]:
    issues = report_integrity_issues(report)
    if issues:
        raise ValueError("报告数据不完整，拒绝建立基线：" + "；".join(issues))
    return Baseline(
        format_version=2,
        status="ready",
        cases_version=cases_version,
        created_at=datetime.now(UTC),
        provenance=Provenance.model_validate(report.run_context),
        samples=_samples(report),
        metrics=collect_gate_metrics(report),
        rules={name: rule.model_copy() for name, rule in DEFAULT_RULES.items()},
    ).model_dump(mode="json")


def write_baseline(path: Path, report: EvalReport, *, cases_version: str) -> None:
    # 校验成功之后才写临时文件，再原子替换，失败不破坏已有基线。
    payload = build_baseline(report, cases_version=cases_version)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_baseline(path: Path) -> dict[str, Any] | None:
    try:
        data = Baseline.model_validate_json(path.read_text(encoding="utf-8"))
        return data.model_dump(mode="json")
    except (OSError, ValueError):
        return None


def validate_baseline(baseline: dict, *, cases_version: str) -> Baseline:
    data = Baseline.model_validate(baseline)
    if data.cases_version != cases_version:
        raise ValueError("题集版本不一致，请在确认新报告后重立基线")
    return data


def compare_with_baseline(
    report: EvalReport,
    *,
    cases_version: str,
    baseline: dict[str, Any],
) -> GateReport:
    base_version = baseline.get("cases_version")
    try:
        data = validate_baseline(baseline, cases_version=cases_version)
        issues = report_integrity_issues(report)
        if issues:
            raise ValueError("报告数据不完整：" + "；".join(issues))
        current = collect_gate_metrics(report)
        # 同样校验当前结果的范围、样本与来源；不只检查平均数。
        current_data = Baseline.model_validate(
            {
                **data.model_dump(mode="json"),
                "metrics": current,
                "samples": _samples(report).model_dump(),
                "provenance": report.run_context,
            }
        )
        if current_data.samples.total_cases != data.samples.total_cases:
            raise ValueError("题目样本数与基线不一致")
        if current_data.provenance.prices != data.provenance.prices:
            raise ValueError("成本单价配置变化，不能直接比较成本，请确认后重立基线")
    except ValueError as error:
        return GateReport(
            passed=False,
            baseline_cases_version=base_version,
            current_cases_version=cases_version,
            reason=str(error),
        )
    checks = []
    for name, rule in data.rules.items():
        base, value = data.metrics[name], current[name]
        if rule.direction == "min":
            limit = base - rule.tolerance
            passed = value >= limit - 1e-9
            detail = f"下限 {limit:.4g}"
        else:
            limit = base * (1 + rule.tolerance)
            passed = value <= limit + 1e-9
            detail = f"上限 {limit:.4g}"
        checks.append(
            GateMetricCheck(
                name=name,
                rule=rule.direction,
                baseline=base,
                current=value,
                passed=passed,
                detail=detail,
            )
        )
    return GateReport(
        passed=all(check.passed for check in checks),
        checks=checks,
        baseline_cases_version=base_version,
        current_cases_version=cases_version,
    )


async def latest_run_report(
    session: AsyncSession,
    *,
    cases_version: str,
) -> tuple[str, EvalReport] | None:
    result = await session.execute(
        select(EvalRun)
        .where(EvalRun.cases_version == cases_version)
        .order_by(EvalRun.created_at.desc())
        .limit(1)
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None
    rows = [CaseRow.model_validate(row) for row in run.rows]
    contexts = [row.get("run_context", {}) for row in run.rows]
    if contexts and any(context != contexts[0] for context in contexts):
        raise ValueError("逐题运行来源不一致")
    summary = RunSummary(
        **{name: getattr(run, name) for name in RunSummary.model_fields if hasattr(run, name)}
    )
    report = EvalReport(
        rows=rows,
        summary=summary,
        run_context=contexts[0] if contexts else {},
    )
    # 不拿当前配置冒充历史配置，不补造缺失字段。
    return str(run.id), report


def format_gate_report(report: GateReport) -> str:
    lines = [f"=== 回归门禁：{'通过' if report.passed else '未通过'} ==="]
    if report.reason:
        lines.append(report.reason)
    for check in report.checks:
        lines.append(
            f"{check.name}: {check.baseline:.4g} → {check.current:.4g} "
            f"[{'OK' if check.passed else 'FAIL'}] {check.detail}"
        )
    lines.append("幻觉/重试仅作诊断；缺失数据、旧格式基线不允许放行。")
    return "\n".join(lines) + "\n"
