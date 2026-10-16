"""编辑提案的确定性评分：在固定副本上验证目标、结果与保护规则。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.domain.content import Slide
from app.domain.edit_ops import add_block, change_block_type, delete_block
from app.domain.slide_patch import apply_patches, filter_patches, parse_block_patches
from app.domain.theme import resolve_theme
from app.domain.validation import validate_slide
from app.eval.edit_cases import EditCase


class EditCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class EditCaseScore(BaseModel):
    case_id: str
    passed: bool
    result_slide: Slide | None = None
    checks: list[EditCheck] = Field(default_factory=list)


def score_edit_case(case: EditCase, *, operations: list[dict[str, Any]]) -> EditCaseScore:
    checks = []

    def check(name: str, passed: bool, detail: str = ""):
        checks.append(EditCheck(name=name, passed=passed, detail="" if passed else detail))

    expect = case.expect
    check(
        "op_count",
        expect.min_ops <= len(operations) <= expect.max_ops,
        f"操作数 {len(operations)}，要求 {expect.min_ops}–{expect.max_ops}",
    )
    check(
        "op_types", all(op.get("op") in expect.op_types for op in operations), "操作类型不符合要求"
    )
    before = {block.id: block.model_dump(mode="json") for block in case.initial_slide.blocks}
    existing_touched = {str(op.get("block_id")) for op in operations if op.get("op") != "add"}
    allowed = set(expect.target_block_ids)
    check("target_blocks", existing_touched <= allowed, "操作命中了未指定的内容块")
    locked = {key for key, block in before.items() if block["locked"]}
    check("locked_untouched", not existing_touched & locked, "提案操作了锁定块")

    slide = case.initial_slide.model_copy(deep=True)
    added = []
    try:
        seen = set()
        for op in operations:
            kind, block_id = op.get("op"), str(op.get("block_id") or "")
            if not block_id or block_id in seen:
                raise ValueError("操作目标为空或重复")
            seen.add(block_id)
            after = op.get("after") or {}
            if kind == "replace":
                if after.get("block_id", block_id) != block_id:
                    raise ValueError("操作目标与 after.block_id 不一致")
                patches = parse_block_patches([{**after, "block_id": block_id}])
                filtered = filter_patches(slide.blocks, patches)
                if filtered.discarded:
                    raise ValueError("替换操作被领域规则拒绝")
                slide.blocks = apply_patches(slide.blocks, filtered.accepted)
            elif kind == "add":
                if block_id in before or slide.layout_tree is None:
                    raise ValueError("新增块重复或缺少布局树")
                if after.get("id", block_id) != block_id:
                    raise ValueError("新增块 ID 不一致")
                if after.get("type") != expect.new_block_type:
                    raise ValueError("新增块类型错误")
                if op.get("after_block_id") != expect.new_block_after:
                    raise ValueError("新增块位置错误")
                slide.blocks, slide.layout_tree, created = add_block(
                    slide.blocks,
                    slide.layout_tree,
                    block_type=after["type"],
                    after_block_id=op["after_block_id"],
                    content=after,
                    block_id=block_id,
                )
                added.append(created.id)
            elif kind == "delete":
                if slide.layout_tree is None:
                    raise ValueError("删除操作缺少布局树")
                slide.blocks, slide.layout_tree, _ = delete_block(
                    slide.blocks,
                    slide.layout_tree,
                    block_id,
                )
            elif kind == "change_type":
                slide.blocks, slide.layout_tree, _, _ = change_block_type(
                    slide.blocks,
                    slide.layout_tree,
                    block_id=block_id,
                    new_type=after["type"],
                    content=after,
                )
            else:
                raise ValueError("未知操作")
        check("proposal_valid", True)
    except (ValueError, KeyError, TypeError) as error:
        check("proposal_valid", False, str(error))
        return EditCaseScore(case_id=case.id, passed=False, checks=checks)

    final = {block.id: block.model_dump(mode="json") for block in slide.blocks}
    check(
        "unrelated_untouched",
        all(final.get(key) == raw for key, raw in before.items() if key not in allowed),
        "未指定内容或锁定状态发生变化",
    )
    changed = {key for key in before if final.get(key) != before[key]}
    if expect.min_ops:
        check("actual_change", bool(changed or added), "操作没有产生实际修改")
        check("required_targets", allowed <= changed, "指定内容块没有全部完成修改")
    for target, fields in expect.expected_after.items():
        check(
            f"expected_after:{target}",
            bool(added if target == "$new" else [target])
            and all(
                key in final
                and all(final[key].get(field) == value for field, value in fields.items())
                for key in (added if target == "$new" else [target])
            ),
            "目标块的结果与期望不一致",
        )
    for target, keywords in expect.after_contains.items():
        ids = added if target == "$new" else [target]
        content_fields = ("text", "items", "value", "label", "note", "header", "rows")
        text = json.dumps(
            [
                {field: final[key][field] for field in content_fields if field in final[key]}
                for key in ids
                if key in final
            ],
            ensure_ascii=False,
        )
        check(
            f"after_contains:{target}",
            all(word in text for word in keywords),
            "目标结果缺少指定文字",
        )
    errors = [
        issue
        for issue in validate_slide(slide, theme=resolve_theme(case.theme_id))
        if issue.severity == "error"
    ]
    check("structure_valid", not errors, "；".join(issue.message for issue in errors))
    return EditCaseScore(
        case_id=case.id,
        passed=all(item.passed for item in checks),
        checks=checks,
        result_slide=slide,
    )
