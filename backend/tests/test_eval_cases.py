"""评测题集加载器（eval#2）：扫描 backend/eval/cases/ 并校验全部 case。

两类用例：
- test_load_real_benchmark_cases 系列对仓库内真实题集做整体断言（20 题、
  分目录配比、字段完整性），题集本身是一等公民评测资产；
- test_missing_* / test_bad_* / test_duplicate_* 用 tmp_path 造坏数据，
  验证缺字段、坏 JSON、材料缺失、重复 id 都报清晰中文错误。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.eval.cases import CASES_DIR, CATEGORIES, EvalCase, load_cases, pick_smoke_case


def _write_case(
    root: Path,
    rel: str,
    *,
    id_: str | None = None,
    category: str | None = None,
    source: str | None = None,
    extra: dict | None = None,
    raw: str | None = None,
) -> Path:
    """往临时题集里写一个 case；raw 非空时直接落盘（构造坏 JSON）。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    parts = rel.split("/")
    payload = {
        "id": id_ if id_ is not None else f"{parts[0]}/{Path(rel).stem}",
        "input": f"生成一份测试用 {Path(rel).stem} 的 8 页 PPT",
        "expected_pages": 8,
        "requirements": ["包含测试要求"],
        "category": category if category is not None else parts[0],
    }
    if source is not None:
        payload["source"] = source
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------- 真实题集整体断言 ----------


def test_load_real_benchmark_cases() -> None:
    cases = load_cases()
    assert len(cases) == 20
    # 输出按 id 排序，评测报告逐题明细顺序稳定
    ids = [case.id for case in cases]
    assert ids == sorted(ids)

    by_category: dict[str, list[EvalCase]] = {name: [] for name in CATEGORIES}
    for case in cases:
        by_category[case.category].append(case)
    assert {name: len(items) for name, items in by_category.items()} == {
        "technology": 5,
        "business": 5,
        "education": 5,
        "documents": 5,
    }


def test_real_case_fields_are_complete_and_consistent() -> None:
    for case in load_cases():
        assert case.input, case.id
        assert case.expected_pages > 0, case.id
        # input 页数措辞与 expected_pages 一致，防止题目自相矛盾
        assert f"{case.expected_pages} 页" in case.input, case.id
        assert case.requirements, case.id
        assert all(req.strip() for req in case.requirements), case.id
        if case.category == "documents":
            assert case.source is not None, case.id
            assert case.source_path is not None and case.source_path.is_file(), case.id
        else:
            assert case.source is None, case.id


def test_real_case_ids_match_directories() -> None:
    files = sorted(CASES_DIR.rglob("*.json"))
    assert len(files) == 20
    expected_ids = {f"{p.relative_to(CASES_DIR).parts[0]}/{p.stem}" for p in files}
    assert {case.id for case in load_cases()} == expected_ids


def test_real_document_sources_are_material_files() -> None:
    for case in load_cases():
        if case.category == "documents":
            assert case.source is not None and case.source.suffix == ".md", case.id
            assert case.source.name == f"{case.id.split('/')[1]}.md", case.id


# ---------- 坏数据异常用例 ----------


def test_missing_required_field_reports_path_and_field(tmp_path: Path) -> None:
    path = _write_case(tmp_path, "technology/rag.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["requirements"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "technology/rag.json" in str(exc_info.value)
    assert "requirements" in str(exc_info.value)


def test_bad_json_reports_clear_error(tmp_path: Path) -> None:
    _write_case(tmp_path, "technology/rag.json", raw="{ 不是 JSON")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "不是合法 JSON" in str(exc_info.value)


def test_document_case_missing_source_material(tmp_path: Path) -> None:
    _write_case(tmp_path, "documents/q3_sales.json", source="q3_sales.md")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "材料文件不存在" in str(exc_info.value)
    assert "q3_sales.md" in str(exc_info.value)


def test_document_case_without_source_field(tmp_path: Path) -> None:
    _write_case(tmp_path, "documents/q3_sales.json")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "缺少 source" in str(exc_info.value)


def test_duplicate_id_reports_both_locations(tmp_path: Path) -> None:
    # 两份不同文件名、手写相同 id（business/rag2.json 伪造 technology/rag），
    # 绕开 id/文件名一致性校验，制造跨目录 id 重复
    _write_case(tmp_path, "technology/rag.json")
    _write_case(tmp_path, "business/rag2.json", id_="technology/rag", category="technology")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    message = str(exc_info.value)
    assert "id 重复" in message
    assert "technology/rag.json" in message and "business/rag2.json" in message


def test_empty_cases_dir_is_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "题集目录为空或不存在" in str(exc_info.value)


def test_id_mismatch_with_filename_is_error(tmp_path: Path) -> None:
    _write_case(tmp_path, "technology/rag.json", id_="technology/other")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "id 应为" in str(exc_info.value)


def test_unknown_category_directory_is_error(tmp_path: Path) -> None:
    _write_case(tmp_path, "tech/rag.json")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "不在合法的类别目录" in str(exc_info.value)


def test_nested_case_file_is_error(tmp_path: Path) -> None:
    # case 必须直接位于类别目录下，嵌套目录说明数据放错了位置
    _write_case(tmp_path, "technology/sub/rag.json")

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "不在合法的类别目录" in str(exc_info.value)


def test_non_positive_expected_pages_is_error(tmp_path: Path) -> None:
    _write_case(tmp_path, "technology/rag.json", extra={"expected_pages": 0})

    with pytest.raises(ValueError) as exc_info:
        load_cases(tmp_path)
    assert "expected_pages" in str(exc_info.value)


# ---------- 冒烟选题（run_eval 全量前的 1 题预检） ----------


def test_pick_smoke_case_prefers_topic_case_with_fewest_pages() -> None:
    cases = load_cases()
    smoke = pick_smoke_case(cases)

    # 主题题不传材料、链路最短；同为此类里挑页数最少的，冒烟耗时可控
    assert smoke is not None
    assert smoke.category != "documents"
    assert smoke.source is None
    min_topic_pages = min(c.expected_pages for c in cases if c.category != "documents")
    assert smoke.expected_pages == min_topic_pages
    # 返回值必须来自题集本身，保证后续全量按 id 去重时不会漏跑
    assert smoke in cases


def test_pick_smoke_case_skips_documents_even_when_shortest() -> None:
    # 构造：文档题 3 页最短，主题题 5 页 → 仍选主题题
    topic = EvalCase(
        id="technology/rag",
        input="生成一份测试的 5 页 PPT",
        expected_pages=5,
        requirements=["包含测试要求"],
        category="technology",
    )
    doc = EvalCase(
        id="documents/q3",
        input="根据材料生成一份 3 页 PPT",
        expected_pages=3,
        requirements=["包含测试要求"],
        category="documents",
        source="q3.md",
    )
    smoke = pick_smoke_case([doc, topic])
    assert smoke is topic


def test_pick_smoke_case_none_when_no_topic_case() -> None:
    doc = EvalCase(
        id="documents/q3",
        input="根据材料生成一份 3 页 PPT",
        expected_pages=3,
        requirements=["包含测试要求"],
        category="documents",
        source="q3.md",
    )
    assert pick_smoke_case([doc]) is None
