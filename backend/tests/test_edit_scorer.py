"""编辑能力必须在固定页面、明确目标与实际提案副本上比较。"""

from app.eval.edit_cases import load_edit_cases
from app.eval.edit_scorer import score_edit_case


def case(cid="replace/retitle-page"):
    return next(item for item in load_edit_cases() if item.id == cid)


def replace(block_id="title", text="营收翻倍之路"):
    return {
        "op": "replace",
        "block_id": block_id,
        "type": "text",
        "after": {"type": "text", "block_id": block_id, "text": text},
    }


def test_correct_target_and_result_pass():
    assert score_edit_case(case(), operations=[replace()]).passed


def test_empty_proposal_cannot_pass_a_required_edit():
    assert not score_edit_case(case(), operations=[]).passed


def test_right_text_on_wrong_block_cannot_pass():
    assert not score_edit_case(case(), operations=[replace("body")]).passed


def test_wrong_replacement_cannot_pass():
    assert not score_edit_case(case(), operations=[replace(text="其他标题")]).passed


def test_guard_case_requires_no_operations():
    guard = case("guard/locked-title")
    assert score_edit_case(guard, operations=[]).passed
    assert not score_edit_case(guard, operations=[replace(text="降价 30%")]).passed


def test_unrelated_block_change_fails_even_when_target_is_correct():
    current = case()
    current.expect.max_ops = 2
    ops = [
        replace(),
        {
            "op": "replace",
            "block_id": "revenue",
            "type": "kpi",
            "after": {"type": "kpi", "value": "90%", "label": "同比增长"},
        },
    ]
    assert not score_edit_case(current, operations=ops).passed


def test_added_block_must_have_correct_type_anchor_and_text():
    current = case("add/callout-source")
    op = {
        "op": "add",
        "block_id": "new-note",
        "type": "callout",
        "after_block_id": "revenue",
        "after": {
            "id": "new-note",
            "slot_id": "new-note",
            "type": "callout",
            "text": "数据口径见年报附录",
        },
    }
    assert score_edit_case(current, operations=[op]).passed
    op["after_block_id"] = "title"
    assert not score_edit_case(current, operations=[op]).passed


def test_add_keyword_in_icon_cannot_substitute_for_requested_text():
    current = case("add/callout-source")
    op = {
        "op": "add",
        "block_id": "new-note",
        "type": "callout",
        "after_block_id": "revenue",
        "after": {
            "id": "new-note",
            "slot_id": "new-note",
            "type": "callout",
            "text": "错误内容",
            "icon": "数据口径见年报附录",
        },
    }
    assert not score_edit_case(current, operations=[op]).passed
