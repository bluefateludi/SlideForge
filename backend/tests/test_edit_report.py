"""缺失的编辑用量不得落成真实零；编辑明细必须能通过公开响应模型。"""

from app.eval.edit_cases import load_edit_cases
from app.eval.edit_runner import EditCaseArtifacts


def test_missing_edit_measurements_are_null_in_storage_and_response():
    import uuid
    from datetime import UTC, datetime

    from app.eval.edit_persistence import build_edit_run
    from app.schemas.eval import EvalRunDetail

    cases = load_edit_cases()[:1]
    item = EditCaseArtifacts(
        case_id=cases[0].id,
        ok=True,
        stage="scored",
        checks=[{"name": "op_count", "passed": True, "detail": ""}],
        measurement_issues=["missing_trace_id"],
    )
    run = build_edit_run(cases, [item])
    run.id, run.created_at = uuid.uuid4(), datetime.now(UTC)
    assert run.avg_prompt_tokens is None
    assert run.avg_cost is None
    response = EvalRunDetail.model_validate(run)
    assert response.rows[0].category == "edit"
    assert response.rows[0].checks[0].passed
    assert response.rows[0].prompt_tokens is None


def test_measured_zero_is_preserved_as_zero():
    from app.eval.edit_persistence import build_edit_run

    cases = load_edit_cases()[:1]
    item = EditCaseArtifacts(
        case_id=cases[0].id,
        ok=True,
        stage="scored",
        prompt_tokens=0,
        completion_tokens=0,
        cost=0,
        measurement_complete=True,
    )
    run = build_edit_run(cases, [item])
    assert run.avg_prompt_tokens == 0
    assert run.avg_cost == 0
