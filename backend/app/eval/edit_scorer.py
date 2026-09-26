"""ai_edit 工具环评分器（obs/10）：纯规则断言，不引入 judge。

输入是 ai-edit 提案返回的 operations（AiEditOperationPublic 形态的 dict）
与运行期锁定的块 id 列表；断言四件事：
1. 操作类型白名单（replace/add/delete/change_type）
2. 操作数上限（过度编辑检查：未点名的块不动）
3. 结果包含关键词（指令达成检查，json 子串匹配）
4. locked 块零误改（权限边界检查）
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from app.eval.edit_cases import EditCase


class EditCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class EditCaseScore(BaseModel):
    case_id: str
    passed: bool
    checks: list[EditCheck] = Field(default_factory=list)


def score_edit_case(
    case: EditCase, *, operations: list[dict[str, Any]], locked_ids: list[str]
) -> EditCaseScore:
    checks: list[EditCheck] = []
    expect = case.expect

    # 1) 操作类型白名单
    bad_types = sorted(
        {str(op.get("op")) for op in operations if str(op.get("op")) not in expect.op_types}
    )
    checks.append(
        EditCheck(
            name="op_types",
            passed=not bad_types,
            detail=f"允许 {expect.op_types}，越界 {bad_types}" if bad_types else "",
        )
    )

    # 2) 操作数上限（过度编辑）
    over = len(operations) - expect.max_ops
    checks.append(
        EditCheck(
            name="max_ops",
            passed=over <= 0,
            detail=f"{len(operations)} 个操作 > 上限 {expect.max_ops}" if over > 0 else "",
        )
    )

    # 3) 结果包含关键词（在任一操作的 after 内容里找到即算）
    for keyword in expect.after_contains:
        haystack = json.dumps([op.get("after") for op in operations], ensure_ascii=False)
        found = keyword in haystack
        checks.append(
            EditCheck(
                name=f"after_contains:{keyword}",
                passed=found,
                detail="" if found else "所有操作的 after 内容均未包含该关键词",
            )
        )

    # 4) locked 块零误改
    if expect.locked_zero_edit and locked_ids:
        touched = sorted({str(op.get("block_id")) for op in operations} & set(locked_ids))
        checks.append(
            EditCheck(
                name="locked_untouched",
                passed=not touched,
                detail=f"locked 块被操作：{touched}" if touched else "",
            )
        )

    return EditCaseScore(
        case_id=case.id,
        passed=all(check.passed for check in checks),
        checks=checks,
    )


__all__ = ["EditCaseScore", "EditCheck", "score_edit_case"]
