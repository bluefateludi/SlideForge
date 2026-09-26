"""edit 评分器（obs/10）：四类纯规则断言的判定语义。"""

from __future__ import annotations

from app.eval.edit_cases import EditCase, EditExpectation, EditSeed
from app.eval.edit_scorer import score_edit_case


def _case(**expect_overrides) -> EditCase:
    expect = {"op_types": ["replace"], "max_ops": 2}
    expect.update(expect_overrides)
    return EditCase(
        id="replace/x",
        instruction="改标题",
        seed=EditSeed(topic="一家公司的年度经营汇报要点", page_count=2, title="汇报"),
        expect=EditExpectation(**expect),
    )


def _op(op: str = "replace", block_id: str = "t1", after: dict | None = None) -> dict:
    if after is None:
        after = {"text": "营收翻倍之路"}
    return {"op": op, "block_id": block_id, "after": after}


def test_all_checks_pass() -> None:
    score = score_edit_case(
        _case(after_contains=["营收翻倍之路"]),
        operations=[_op()],
        locked_ids=["b9"],
    )
    assert score.passed is True
    assert all(check.passed for check in score.checks)


def test_op_type_out_of_whitelist_fails() -> None:
    score = score_edit_case(_case(), operations=[_op(op="delete")], locked_ids=[])
    assert score.passed is False
    named = {check.name: check for check in score.checks}
    assert named["op_types"].passed is False
    assert "delete" in named["op_types"].detail


def test_over_editing_fails_max_ops() -> None:
    ops = [_op(block_id=f"b{i}") for i in range(3)]
    score = score_edit_case(_case(), operations=ops, locked_ids=[])
    named = {check.name: check for check in score.checks}
    assert named["max_ops"].passed is False


def test_after_contains_missing_keyword_fails() -> None:
    score = score_edit_case(
        _case(after_contains=["不存在的关键词"]),
        operations=[_op()],
        locked_ids=[],
    )
    assert score.passed is False
    assert any(
        check.name.startswith("after_contains:") and not check.passed
        for check in score.checks
    )


def test_locked_block_touched_fails() -> None:
    score = score_edit_case(_case(), operations=[_op(block_id="t1")], locked_ids=["t1"])
    named = {check.name: check for check in score.checks}
    assert named["locked_untouched"].passed is False
    assert "t1" in named["locked_untouched"].detail
    assert score.passed is False


def test_locked_check_skipped_without_locked_ids() -> None:
    score = score_edit_case(_case(), operations=[_op()], locked_ids=[])
    names = {check.name for check in score.checks}
    assert "locked_untouched" not in names
