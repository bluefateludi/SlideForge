"""edit 题集加载器（obs/10）：仓库题集可加载 + 坏数据报清晰错误。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.edit_cases import (
    CATEGORIES,
    compute_edit_cases_version,
    load_edit_cases,
)


def test_repo_cases_load_and_validate() -> None:
    cases = load_edit_cases()
    assert len(cases) >= 5
    assert [case.id for case in cases] == sorted(case.id for case in cases)
    assert all(case.category in CATEGORIES for case in cases)
    version = compute_edit_cases_version(cases)
    assert version.startswith("edit-") and len(version) == len("edit-") + 12


def _write(root: Path, rel: str, payload: dict) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _valid_case(cid: str = "replace/x") -> dict:
    return {
        "id": cid,
        "instruction": "把标题改为「营收翻倍之路」",
        "seed": {
            "topic": "一家公司的年度经营汇报要点，包含增长与留存",
            "page_count": 2,
            "title": "汇报",
        },
        "expect": {"op_types": ["replace"], "max_ops": 2},
    }


def test_bad_category_directory_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "unknown_dir/x.json", _valid_case())
    with pytest.raises(ValueError, match="类别目录"):
        load_edit_cases(tmp_path)


def test_bad_lock_value_rejected(tmp_path: Path) -> None:
    payload = _valid_case()
    payload["lock"] = "everything"
    _write(tmp_path, "replace/x.json", payload)
    with pytest.raises(ValueError, match="lock"):
        load_edit_cases(tmp_path)


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "replace/x.json", _valid_case())
    _write(tmp_path, "replace/y.json", _valid_case("replace/x"))
    with pytest.raises(ValueError, match="重复"):
        load_edit_cases(tmp_path)


def test_id_filename_mismatch_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "replace/y.json", _valid_case())
    with pytest.raises(ValueError, match="应为"):
        load_edit_cases(tmp_path)


def test_empty_dir_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="为空"):
        load_edit_cases(tmp_path)
