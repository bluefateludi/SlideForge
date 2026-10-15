"""正式基线不能接受缺失数据；容差来自文件中的数值规则。"""

import pytest

from app.eval.gate import build_baseline, compare_with_baseline, load_baseline, write_baseline
from app.eval.report import CaseRow, EvalReport, aggregate


def valid_report():
    rows = [
        CaseRow(
            case_id=f"technology/c{i}",
            ok=True,
            stage="done",
            expected_pages=2,
            ready_pages=2,
            pages_met=True,
            schema_valid=True,
            export_succeeded=True,
            judge_score=8.5,
            judge_coverage=1,
            elapsed_seconds=60,
            prompt_tokens=1000,
            completion_tokens=200,
            tokens_source="trace",
            measurement_complete=True,
            cost_configured=True,
            observed_models=["test-model"],
            cost=0.0012,
        )
        for i in range(2)
    ]
    return EvalReport(
        summary=aggregate(rows),
        rows=rows,
        run_context={
            "source_run_id": "00000000-0000-0000-0000-000000000001",
            "code_revision": "a" * 40,
            "generation_models": ["test-model"],
            "judge_model": "test-judge",
            "prices": {"prompt_per_mtok": 1, "completion_per_mtok": 1, "image_per_unit": 0},
            "runtime": {"api_base": "http://localhost:39800/api/v1"},
        },
    )


def test_missing_trace_cannot_pass_or_overwrite_baseline(tmp_path):
    good = valid_report()
    baseline = build_baseline(good, cases_version="cases-v2-test")
    path = tmp_path / "baseline.json"
    write_baseline(path, good, cases_version="cases-v2-test")
    original = path.read_bytes()
    bad = valid_report()
    bad.rows[0].measurement_complete = False
    bad.rows[0].tokens_source = "unavailable"
    bad.rows[0].prompt_tokens = 0
    assert not compare_with_baseline(
        bad,
        cases_version="cases-v2-test",
        baseline=baseline,
    ).passed
    with pytest.raises(ValueError, match="完整"):
        write_baseline(path, bad, cases_version="cases-v2-test")
    assert path.read_bytes() == original


def test_legacy_and_empty_metrics_baseline_are_rejected(tmp_path):
    import json

    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"cases_version": "cases-v2-test", "metrics": {}}))
    assert load_baseline(path) is None
    assert not compare_with_baseline(
        valid_report(),
        cases_version="cases-v2-test",
        baseline={"cases_version": "cases-v2-test", "metrics": {}},
    ).passed


def test_numeric_tolerance_in_file_controls_verdict():
    base = build_baseline(valid_report(), cases_version="cases-v2-test")
    current = valid_report()
    current.summary.avg_prompt_tokens = 1200
    assert compare_with_baseline(current, cases_version="cases-v2-test", baseline=base).passed
    base["rules"]["avg_prompt_tokens"]["tolerance"] = 0.1
    assert not compare_with_baseline(
        current,
        cases_version="cases-v2-test",
        baseline=base,
    ).passed


def test_pages_and_requirement_coverage_are_gated():
    base = build_baseline(valid_report(), cases_version="cases-v2-test")
    current = valid_report()
    current.summary.pages_met_rate = 0.5
    current.summary.avg_judge_coverage = 0.5
    result = compare_with_baseline(current, cases_version="cases-v2-test", baseline=base)
    assert {"pages_met_rate", "avg_judge_coverage"} <= {
        check.name for check in result.checks if not check.passed
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_measurement_cannot_establish_baseline(value):
    report = valid_report()
    report.summary.avg_cost = value
    with pytest.raises(ValueError):
        build_baseline(report, cases_version="cases-v2-test")


def test_valid_zero_usage_does_not_disable_cost_protection():
    report = valid_report()
    report.summary.avg_prompt_tokens = 0
    base = build_baseline(report, cases_version="cases-v2-test")
    result = compare_with_baseline(valid_report(), cases_version="cases-v2-test", baseline=base)
    assert not result.passed
    assert not next(c for c in result.checks if c.name == "avg_prompt_tokens").skipped


def test_missing_provenance_cannot_establish_baseline():
    report = valid_report()
    report.run_context = {}
    with pytest.raises(ValueError):
        build_baseline(report, cases_version="cases-v2-test")


def test_partial_judge_coverage_cannot_establish_baseline():
    report = valid_report()
    report.rows[0].judge_score = None
    with pytest.raises(ValueError):
        build_baseline(report, cases_version="cases-v2-test")


def test_baseline_roundtrip_preserves_provenance_and_diagnostics(tmp_path):
    report = valid_report()
    path = tmp_path / "baseline.json"
    write_baseline(path, report, cases_version="cases-v2-test")
    data = load_baseline(path)
    assert data["format_version"] == 2
    assert data["provenance"]["code_revision"] == "a" * 40
    assert data["samples"]["trace_cases"] == 2
    assert "hallucination_rate" in data["metrics"]
    assert "retry_slide_rate" in data["metrics"]
    assert "hallucination_rate" not in data["rules"]


@pytest.mark.asyncio
async def test_database_rebuild_uses_saved_context_not_current_settings():
    from app.eval.gate import latest_run_report
    from app.eval.persistence import report_to_run

    report = valid_report()
    stored = report_to_run(report, cases_version="cases-v2-test")

    class RunStore:
        async def execute(self, statement):
            return self

        def scalar_one_or_none(self):
            return stored

    run_id, restored = await latest_run_report(RunStore(), cases_version="cases-v2-test")
    assert run_id == report.run_context["source_run_id"]
    baseline = build_baseline(restored, cases_version="cases-v2-test")
    assert baseline["provenance"] == report.run_context
    assert baseline["metrics"]["export_succeeded_cases"] == 2
    assert restored.rows[0].measurement_complete is True


@pytest.mark.asyncio
async def test_legacy_database_report_cannot_become_a_baseline():
    from app.eval.gate import latest_run_report
    from app.eval.persistence import report_to_run

    report = valid_report()
    stored = report_to_run(report, cases_version="cases-v2-test")
    for row in stored.rows:
        row.pop("run_context")

    class RunStore:
        async def execute(self, statement):
            return self

        def scalar_one_or_none(self):
            return stored

    _, restored = await latest_run_report(RunStore(), cases_version="cases-v2-test")
    with pytest.raises(ValueError):
        build_baseline(restored, cases_version="cases-v2-test")


def test_zero_p95_is_rejected_before_a_baseline_can_be_loaded(tmp_path):
    import json

    data = build_baseline(valid_report(), cases_version="cases-v2-test")
    data["metrics"]["p95_elapsed_seconds"] = 0
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_baseline(path) is None
