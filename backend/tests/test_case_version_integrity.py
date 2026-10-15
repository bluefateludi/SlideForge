"""题集版本是内容身份，不是一次运行或文件位置的身份。"""
from pathlib import Path

import pytest

from app.eval.cases import load_cases
from app.eval.persistence import compute_cases_version


def _cases(root: Path):
    import json

    folder = root / "documents"
    folder.mkdir(parents=True)
    (folder / "sales.md").write_text("营收：100 万元", encoding="utf-8")
    (folder / "sales.json").write_text(
        json.dumps({
            "id": "documents/sales", "input": "生成销售总结", "expected_pages": 2,
            "requirements": ["包含营收"], "category": "documents", "source": "sales.md",
        }, ensure_ascii=False), encoding="utf-8",
    )
    return load_cases(root)


def test_material_body_change_changes_version(tmp_path):
    cases = _cases(tmp_path)
    original = compute_cases_version(cases)
    cases[0].source_path.write_text("营收：200 万元", encoding="utf-8")
    assert compute_cases_version(cases) != original


def test_same_content_keeps_version_across_locations(tmp_path):
    first = _cases(tmp_path / "first")
    second = _cases(tmp_path / "second")
    assert compute_cases_version(first) == compute_cases_version(second)


def test_order_and_repeated_runs_do_not_change_version(tmp_path):
    from app.eval.cases import EvalCase

    cases = _cases(tmp_path)
    cases.append(EvalCase(
        id="technology/demo", input="技术介绍", expected_pages=2,
        requirements=["包含原理"], category="technology",
    ))
    original = compute_cases_version(cases)
    assert original.startswith("cases-v2-")
    assert compute_cases_version(list(reversed(cases))) == original
    assert compute_cases_version(cases) == original


def test_missing_material_is_not_a_valid_version(tmp_path):
    cases = _cases(tmp_path)
    cases[0].source_path.unlink()
    with pytest.raises(ValueError, match="材料"):
        compute_cases_version(cases)

