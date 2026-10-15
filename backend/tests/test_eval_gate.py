"""回归门禁（eval#6）：指标采集 / 容差判定 / 基线读写 / 库取基线。

容差边界值（1300 恰好 ×1.3、judge 恰好 -0.5）都取闭区间语义。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.core.db import async_session_factory
from app.eval.gate import (
    collect_gate_metrics,
    compare_with_baseline,
    latest_run_metrics,
    load_baseline,
    p95_elapsed,
    write_baseline,
    write_baseline_from_metrics,
)
from app.eval.report import CaseRow, EvalReport, RunSummary
from app.models.eval_run import EvalRun


def _report(
    *,
    success_rate: float = 1.0,
    schema_valid_rate: float = 1.0,
    schema_scored: int = 20,
    export_cases: int = 20,
    judge: float | None = 8.0,
    prompt: float = 1000.0,
    completion: float = 500.0,
    avg_cost: float = 0.5,
    total_cost: float = 10.0,
    elapsed: list[float] | None = None,
) -> EvalReport:
    rows = [
        CaseRow(case_id=f"c{i}", ok=True, stage="done", elapsed_seconds=seconds)
        for i, seconds in enumerate(elapsed or [60.0] * 20)
    ]
    return EvalReport(
        summary=RunSummary(
            total_cases=20,
            ok_cases=20,
            success_rate=success_rate,
            schema_scored_cases=schema_scored,
            schema_valid_rate=schema_valid_rate,
            export_succeeded_cases=export_cases,
            avg_judge_score=judge,
            avg_prompt_tokens=prompt,
            avg_completion_tokens=completion,
            avg_cost=avg_cost,
            total_cost=total_cost,
        ),
        rows=rows,
    )


def _baseline_dict(report: EvalReport, version: str = "cases-test") -> dict:
    from app.eval.gate import build_baseline

    return build_baseline(report, cases_version=version)


class TestP95:
    def test_even_samples(self) -> None:
        # 20 个值：rank = ceil(0.95×20) = 19 → 第 19 小（即 1..20 中的 19）
        rows = [{"elapsed_seconds": value} for value in range(1, 21)]
        assert p95_elapsed(rows) == 19

    def test_odd_samples_takes_max(self) -> None:
        rows = [{"elapsed_seconds": value} for value in range(1, 11)]
        assert p95_elapsed(rows) == 10

    def test_empty(self) -> None:
        assert p95_elapsed([]) is None

    def test_case_rows_and_dicts_agree(self) -> None:
        case_rows = [CaseRow(case_id="a", ok=True, stage="done", elapsed_seconds=5.0)]
        dicts = [{"elapsed_seconds": 5.0}]
        assert p95_elapsed(case_rows) == p95_elapsed(dicts) == 5.0


class TestCompare:
    def test_improvement_passes(self) -> None:
        base = _baseline_dict(_report())
        current = _report(judge=9.0, schema_valid_rate=1.0)
        result = compare_with_baseline(current, cases_version="cases-test", baseline=base)
        assert result.passed is True
        assert all(check.passed for check in result.checks)

    def test_success_rate_drop_fails(self) -> None:
        base = _baseline_dict(_report())
        current = _report(success_rate=0.95)
        result = compare_with_baseline(current, cases_version="cases-test", baseline=base)
        assert result.passed is False
        failed = [check.name for check in result.checks if not check.passed]
        assert failed == ["success_rate"]

    def test_export_drop_fails(self) -> None:
        base = _baseline_dict(_report())
        result = compare_with_baseline(
            _report(export_cases=19), cases_version="cases-test", baseline=base
        )
        assert result.passed is False

    def test_judge_delta_is_closed_interval(self) -> None:
        base = _baseline_dict(_report(judge=8.0))
        at_floor = compare_with_baseline(
            _report(judge=7.5), cases_version="cases-test", baseline=base
        )
        assert at_floor.passed is True
        below_floor = compare_with_baseline(
            _report(judge=7.49), cases_version="cases-test", baseline=base
        )
        assert below_floor.passed is False

    def test_ratio_limit_is_closed_interval(self) -> None:
        base = _baseline_dict(_report(prompt=1000.0))
        at_limit = compare_with_baseline(
            _report(prompt=1300.0), cases_version="cases-test", baseline=base
        )
        assert at_limit.passed is True
        over_limit = compare_with_baseline(
            _report(prompt=1300.01), cases_version="cases-test", baseline=base
        )
        assert over_limit.passed is False

    def test_zero_baseline_ratio_metric_skipped(self) -> None:
        # 旧基线跑在 token 恒 0 时期：非零真值不应被判成回归，而是跳过
        base = _baseline_dict(_report(prompt=0.0, completion=0.0, avg_cost=0.0, total_cost=0.0))
        result = compare_with_baseline(
            _report(prompt=999.0, avg_cost=1.0),
            cases_version="cases-test",
            baseline=base,
        )
        assert result.passed is True
        skipped = {check.name for check in result.checks if check.skipped}
        assert {"avg_prompt_tokens", "avg_completion_tokens", "avg_cost", "total_cost"} <= skipped

    def test_none_metric_fails_when_baseline_has_value(self) -> None:
        # 基线有 judge 分而本轮没有（如忘配 judge）：无法验证质量 → 判负
        base = _baseline_dict(_report(judge=8.0))
        current = _report(judge=None)
        result = compare_with_baseline(current, cases_version="cases-test", baseline=base)
        judge_check = next(check for check in result.checks if check.name == "avg_judge_score")
        assert judge_check.passed is False
        assert judge_check.skipped is False
        assert "无法验证" in judge_check.detail
        assert result.passed is False

    def test_version_mismatch_refuses(self) -> None:
        base = _baseline_dict(_report(), version="cases-old")
        result = compare_with_baseline(_report(), cases_version="cases-new", baseline=base)
        assert result.passed is False
        assert result.checks == []
        assert "重立基线" in result.reason


class TestBaselineFile:
    def test_write_load_round_trip(self, tmp_path) -> None:
        report = _report()
        path = tmp_path / "baseline.json"
        write_baseline(path, report, cases_version="cases-rt")
        loaded = load_baseline(path)
        assert loaded is not None
        assert loaded["cases_version"] == "cases-rt"
        metrics = collect_gate_metrics(report)
        assert loaded["metrics"]["success_rate"] == metrics["success_rate"]
        assert loaded["metrics"]["p95_elapsed_seconds"] == metrics["p95_elapsed_seconds"]

    def test_load_missing_or_broken_returns_none(self, tmp_path) -> None:
        assert load_baseline(tmp_path / "nope.json") is None
        broken = tmp_path / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        assert load_baseline(broken) is None
        no_version = tmp_path / "partial.json"
        no_version.write_text('{"metrics": {}}', encoding="utf-8")
        assert load_baseline(no_version) is None

    def test_write_from_metrics_keeps_source(self, tmp_path) -> None:
        path = tmp_path / "baseline.json"
        write_baseline_from_metrics(
            path,
            cases_version="cases-x",
            metrics={"success_rate": 1.0},
            source="eval_runs:test",
        )
        loaded = load_baseline(path)
        assert loaded is not None
        assert loaded["source"] == "eval_runs:test"
        assert loaded["metrics"]["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_latest_run_metrics_from_db() -> None:
    """库里最近一次同版本 run → 指标集（P95 从 rows JSONB 现算）。"""
    version = f"cases-gate-{uuid.uuid4().hex[:8]}"
    rows_jsonb = [
        {"case_id": "a", "ok": True, "stage": "done", "elapsed_seconds": 10.0},
        {"case_id": "b", "ok": True, "stage": "done", "elapsed_seconds": 20.0},
        {"case_id": "c", "ok": True, "stage": "done", "elapsed_seconds": 30.0},
    ]
    run = EvalRun(
        cases_version=version,
        total_cases=3,
        ok_cases=3,
        success_rate=1.0,
        failure_rate=0.0,
        schema_valid_cases=3,
        schema_scored_cases=3,
        schema_valid_rate=1.0,
        pages_met_cases=3,
        pages_scored_cases=3,
        pages_met_rate=1.0,
        export_succeeded_cases=3,
        avg_judge_score=8.0,
        judge_failed_cases=0,
        doc_total_numbers=0,
        doc_fabricated_numbers=0,
        hallucination_rate=None,
        avg_elapsed_seconds=20.0,
        avg_prompt_tokens=100.0,
        avg_completion_tokens=50.0,
        avg_cost=0.1,
        total_cost=0.3,
        total_failed_slides=0,
        total_slides=3,
        total_retried_slides=0,
        status="completed",
        rows=rows_jsonb,
        note="gate 测试",
    )
    async with async_session_factory() as session:
        session.add(run)
        await session.commit()
    # 用干净 session 查询（与脚本路径一致）
    async with async_session_factory() as session:
        found = await latest_run_metrics(session, cases_version=version)
        assert found is not None
        run_id, metrics = found
        assert run_id == str(run.id)
        assert metrics["success_rate"] == 1.0
        assert metrics["avg_cost"] == 0.1
        # 3 个样本：rank = ceil(0.95×3) = 3 → 最大值
        assert metrics["p95_elapsed_seconds"] == 30.0
        assert metrics["schema_valid_rate"] == 1.0
        assert metrics["export_succeeded_cases"] == 3.0
        await session.execute(delete(EvalRun).where(EvalRun.id == run.id))
        await session.commit()


@pytest.mark.asyncio
async def test_latest_run_metrics_missing_version_returns_none() -> None:
    async with async_session_factory() as session:
        version = f"cases-none-{uuid.uuid4().hex[:8]}"
        found = await latest_run_metrics(session, cases_version=version)
    assert found is None
