"""回归门禁（eval#6）：本轮指标 vs 基线对比，超容差非零退出。

口径（首版；容差随 baseline.json 落盘便于人读与调整）：
- 成功率 / Schema 合法率 / 导出成功题数：零容差，不允许下降
- avg_judge_score：容差 -0.5（评审分有采样噪声）
- token / 成本 / P95 耗时：不允许暴涨（基线 × RATIO_LIMIT）
- 比值型指标的基线为 0/None 时跳过（标注 skipped）：避免「0×1.3=0」
  把任何非零真值判成回归——典型是旧基线跑在单价未配置期（cost 恒 0），
  正确动作是配好单价后重立基线，而不是让门禁永久拦下
- cases_version 不一致：拒绝对比直接判负——分数跨题集版本不可比，
  先重立基线（make eval-baseline）再上门禁
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.paths import REPO_ROOT
from app.eval.report import EvalReport
from app.models.eval_run import EvalRun

BASELINE_PATH = REPO_ROOT / "backend" / "eval" / "baseline.json"

# 首版容差：judge 评分允许降 0.5；token/cost/P95 允许涨 30%
JUDGE_SCORE_FLOOR_DELTA = 0.5
RATIO_LIMIT = 1.3

# 门禁只看 run 级聚合 + P95（由逐题 elapsed 现算）。
# 重试类指标（retry_slide_rate / total_failed_slides）波动大且受
# #22/#32 已知 bug 影响，v1 不进门禁、仅报告展示。
METRIC_RULES: dict[str, str] = {
    "success_rate": "不允许下降（零容差）",
    "schema_valid_rate": "不允许下降（零容差）",
    "export_succeeded_cases": "不允许下降（零容差）",
    "avg_judge_score": f"允许下降 ≤ {JUDGE_SCORE_FLOOR_DELTA}",
    "avg_prompt_tokens": f"不允许上涨超过基线 × {RATIO_LIMIT}",
    "avg_completion_tokens": f"不允许上涨超过基线 × {RATIO_LIMIT}",
    "avg_cost": f"不允许上涨超过基线 × {RATIO_LIMIT}",
    "total_cost": f"不允许上涨超过基线 × {RATIO_LIMIT}",
    "p95_elapsed_seconds": f"不允许上涨超过基线 × {RATIO_LIMIT}",
}

_FLOOR_METRICS = {"avg_judge_score"}


class GateMetricCheck(BaseModel):
    """单个指标的对比结论。skipped 的检查恒 passed（仅提示口径受限）。"""

    name: str
    rule: str
    baseline: float | None
    current: float | None
    passed: bool
    skipped: bool = False
    detail: str = ""


class GateReport(BaseModel):
    """整轮门禁结论。cases_version 不匹配时 passed=False 且 checks 为空。"""

    passed: bool
    baseline_cases_version: str | None
    current_cases_version: str
    checks: list[GateMetricCheck] = Field(default_factory=list)
    reason: str = ""


def p95_elapsed(rows: list[Any]) -> float | None:
    """逐题 elapsed 的 P95（nearest-rank 上取整）；无样本为 None。

    rows 兼容两种形态：CaseRow 列表（report 路径）或明细 dict 列表
    （eval_runs.rows JSONB 路径）。
    """
    values: list[float] = []
    for row in rows:
        elapsed = getattr(row, "elapsed_seconds", None)
        if elapsed is None and isinstance(row, dict):
            elapsed = row.get("elapsed_seconds")
        if elapsed is not None:
            values.append(float(elapsed))
    if not values:
        return None
    values.sort()
    rank = math.ceil(0.95 * len(values))
    return values[min(rank, len(values)) - 1]


def collect_gate_metrics(report: EvalReport) -> dict[str, float | None]:
    """EvalReport → 门禁指标集（P95 由逐题 elapsed 现算）。"""
    s = report.summary
    return {
        "success_rate": s.success_rate,
        "schema_valid_rate": s.schema_valid_rate if s.schema_scored_cases else None,
        "export_succeeded_cases": float(s.export_succeeded_cases),
        "avg_judge_score": s.avg_judge_score,
        "avg_prompt_tokens": s.avg_prompt_tokens,
        "avg_completion_tokens": s.avg_completion_tokens,
        "avg_cost": s.avg_cost,
        "total_cost": s.total_cost,
        "p95_elapsed_seconds": p95_elapsed(report.rows),
    }


def build_baseline(report: EvalReport, *, cases_version: str) -> dict[str, Any]:
    """报告 → 基线字典（指标 + 容差说明，随文件落盘）。"""
    return {
        "cases_version": cases_version,
        "created_at": datetime.now(UTC).isoformat(),
        "metrics": collect_gate_metrics(report),
        "tolerances": dict(METRIC_RULES),
    }


def write_baseline(path: Path, report: EvalReport, *, cases_version: str) -> None:
    payload = build_baseline(report, cases_version=cases_version)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_baseline_from_metrics(
    path: Path, *, cases_version: str, metrics: dict[str, float | None], source: str
) -> None:
    """从已落库指标写基线（--baseline-from-db 路径，不触发任何模型调用）。"""
    payload = {
        "cases_version": cases_version,
        "created_at": datetime.now(UTC).isoformat(),
        "source": source,
        "metrics": metrics,
        "tolerances": dict(METRIC_RULES),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> dict[str, Any] | None:
    """读基线；文件不存在返回 None（门禁按「未立基线」判负）。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "cases_version" not in data or "metrics" not in data:
        return None
    return data


def _check(name: str, baseline: float | None, current: float | None) -> GateMetricCheck:
    rule = METRIC_RULES[name]
    if baseline is None:
        return GateMetricCheck(
            name=name,
            rule=rule,
            baseline=None,
            current=current,
            passed=True,
            skipped=True,
            detail="基线无值（该指标当轮未产出），跳过",
        )
    if name in _FLOOR_METRICS:
        # floor 族：不允许低于基线 - 容差；本轮无值 = 无法验证，判负
        limit = baseline - JUDGE_SCORE_FLOOR_DELTA
        if current is None:
            return GateMetricCheck(
                name=name,
                rule=rule,
                baseline=baseline,
                current=None,
                passed=False,
                detail="本轮无值（该指标未产出），无法验证",
            )
        passed = current >= limit - 1e-9
        detail = f"下限 {round(limit, 4)}"
    elif name in {
        "avg_prompt_tokens",
        "avg_completion_tokens",
        "avg_cost",
        "total_cost",
        "p95_elapsed_seconds",
    }:
        if baseline <= 0:
            # 基线 0：×1.3 仍是 0，任何非零真值都会被误判——跳过并提示重立基线
            return GateMetricCheck(
                name=name,
                rule=rule,
                baseline=baseline,
                current=current,
                passed=True,
                skipped=True,
                detail="基线为 0（口径受限，建议条件变化后重立基线）",
            )
        limit = baseline * RATIO_LIMIT
        passed = current is not None and current <= limit + 1e-9
        detail = f"上限 {round(limit, 4)}"
    else:
        # 零容差族：不允许下降；本轮无值 = 无法验证，判负
        if current is None:
            return GateMetricCheck(
                name=name,
                rule=rule,
                baseline=baseline,
                current=None,
                passed=False,
                detail="本轮无值（该指标未产出），无法验证",
            )
        passed = current >= baseline - 1e-9
        detail = "零容差"

    return GateMetricCheck(
        name=name,
        rule=rule,
        baseline=baseline,
        current=current,
        passed=passed,
        skipped=False,
        detail=detail,
    )


def compare_with_baseline(
    report: EvalReport, *, cases_version: str, baseline: dict[str, Any]
) -> GateReport:
    """本轮报告 vs 基线。version 不一致直接判负（提示重立基线）。"""
    base_version = baseline.get("cases_version")
    if base_version != cases_version:
        return GateReport(
            passed=False,
            baseline_cases_version=base_version,
            current_cases_version=cases_version,
            reason=(
                f"题集版本不一致（基线 {base_version} vs 本轮 {cases_version}），"
                "分数跨版本不可比：请先 make eval-baseline 重立基线"
            ),
        )

    baseline_metrics: dict[str, float | None] = baseline.get("metrics", {})
    current = collect_gate_metrics(report)
    checks = [
        _check(name, baseline_metrics.get(name), current.get(name)) for name in METRIC_RULES
    ]
    return GateReport(
        passed=all(check.passed for check in checks),
        baseline_cases_version=base_version,
        current_cases_version=cases_version,
        checks=checks,
    )


async def latest_run_metrics(
    session: AsyncSession, *, cases_version: str
) -> tuple[str, dict[str, float | None]] | None:
    """库里最近一次同题集版本的 run → 门禁指标集（零模型调用）。

    P95 从 rows JSONB 的逐题 elapsed 现算，与 report 路径同口径。
    """
    result = await session.execute(
        select(EvalRun)
        .where(EvalRun.cases_version == cases_version)
        .order_by(EvalRun.created_at.desc())
        .limit(1)
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None
    metrics: dict[str, float | None] = {
        "success_rate": run.success_rate,
        "schema_valid_rate": run.schema_valid_rate if run.schema_scored_cases else None,
        "export_succeeded_cases": float(run.export_succeeded_cases),
        "avg_judge_score": run.avg_judge_score,
        "avg_prompt_tokens": run.avg_prompt_tokens,
        "avg_completion_tokens": run.avg_completion_tokens,
        "avg_cost": run.avg_cost,
        "total_cost": run.total_cost,
        "p95_elapsed_seconds": p95_elapsed(run.rows),
    }
    return str(run.id), metrics


def format_gate_report(report: GateReport) -> str:
    """控制台门禁报告：结论 + 逐指标对齐明细。"""
    lines: list[str] = []
    verdict = "通过" if report.passed else "未通过"
    lines.append(f"=== 回归门禁：{verdict} ===")
    if report.reason:
        lines.append(report.reason)
    if report.checks:
        name_width = max(len(check.name) for check in report.checks)
        lines.append(f"{'指标'.ljust(name_width)}  基线→本轮          规则与判定")
        for check in report.checks:
            base_text = "—" if check.baseline is None else f"{check.baseline:.4g}"
            curr_text = "—" if check.current is None else f"{check.current:.4g}"
            flag = "OK" if check.passed else "FAIL"
            if check.skipped:
                flag = "SKIP"
            lines.append(
                f"{check.name.ljust(name_width)}  {base_text}→{curr_text}"
                f"  [{flag}] {check.rule}"
                + (f"（{check.detail}）" if check.detail else "")
            )
    lines.append("")
    lines.append(
        "口径：重试类指标（重试率/失败页数）不进门禁（波动大且受已知 bug 影响，仅报告展示）；"
        "基线文件 backend/eval/baseline.json，重立基线：make eval-baseline。"
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "BASELINE_PATH",
    "JUDGE_SCORE_FLOOR_DELTA",
    "RATIO_LIMIT",
    "GateMetricCheck",
    "GateReport",
    "build_baseline",
    "collect_gate_metrics",
    "compare_with_baseline",
    "format_gate_report",
    "latest_run_metrics",
    "load_baseline",
    "p95_elapsed",
    "write_baseline",
    "write_baseline_from_metrics",
]
