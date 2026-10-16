from types import SimpleNamespace

from app.observability.convergence import summarize_convergence


def span(id, name, status="succeeded", parent=None, **attrs):
    return SimpleNamespace(
        id=id,
        name=name,
        status=status,
        parent_span_id=parent,
        attributes=attrs,
        span_kind="task" if name.startswith("slide[") else "node",
    )


def test_quality_not_task_status_and_failed_pages_stay_in_denominator():
    result = summarize_convergence(
        [
            span(1, "slide[1]"),
            span(2, "slide.check", parent=1, quality_passed=True),
            span(3, "slide[2]"),
            span(4, "slide.repair", parent=3),
            span(5, "slide.check", parent=3, quality_passed=False),
            span(6, "slide[3]", status="failed"),
        ]
    )
    assert result.total == 3
    assert result.first_pass == 1
    assert result.repaired == 1
    assert result.repaired_passed == 0
    assert result.first_pass_rate == 1 / 3
    assert result.repair_success_rate == 0


def test_missing_check_is_unknown_and_repair_rounds_count_once():
    result = summarize_convergence(
        [
            span(1, "slide[1]"),
            span(2, "slide.repair", parent=1),
            span(3, "slide.repair", parent=1),
            span(4, "slide[2]", status="running"),
        ]
    )
    assert result.total == 1
    assert result.repaired == 1
    assert result.missing == 1
    assert result.first_pass_rate is None
    assert result.repair_success_rate is None


def test_latest_check_decides_repair_success():
    result = summarize_convergence(
        [
            span(1, "slide[1]"),
            span(2, "slide.check", parent=1, quality_passed=False),
            span(3, "slide.repair", parent=1),
            span(4, "slide.check", parent=1, quality_passed=True),
        ]
    )
    assert result.repaired_passed == 1
    assert result.first_pass_rate == 0
    assert result.repair_success_rate == 1
