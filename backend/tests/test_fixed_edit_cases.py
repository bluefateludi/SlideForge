"""固定页面是题集身份的一部分，评测不再依赖先生成页面。"""

import pytest

from app.eval.edit_cases import compute_edit_cases_version, load_edit_cases


def test_every_edit_case_contains_fixed_initial_slide():
    cases = load_edit_cases()
    assert len(cases) >= 5
    assert all(case.initial_slide.blocks for case in cases)
    assert all(case.initial_slide.layout_tree is not None for case in cases)


def test_fixture_change_changes_version_but_case_order_does_not():
    cases = load_edit_cases()
    version = compute_edit_cases_version(cases)
    assert compute_edit_cases_version(list(reversed(cases))) == version
    changed = [case.model_copy(deep=True) for case in cases]
    changed[0].initial_slide.blocks[0].text = "不同的固定标题"
    assert compute_edit_cases_version(changed) != version


def test_invalid_layout_reference_is_rejected():
    from app.eval.edit_cases import EditCase

    raw = load_edit_cases()[0].model_dump(mode="json")
    raw["initial_slide"]["layout_tree"]["children"][0]["block_id"] = "missing"
    with pytest.raises(ValueError, match="布局"):
        EditCase.model_validate(raw)
