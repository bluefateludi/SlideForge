"""固定初始页面的编辑题集：不调用模型准备测试输入。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.paths import REPO_ROOT
from app.domain.content import BlockType, Slide
from app.domain.flex_layout import iter_leaf_block_ids
from app.domain.outline import OutlinePageDraft
from app.domain.validation import validate_slide

EDIT_CASES_DIR = REPO_ROOT / "backend" / "eval" / "edit_cases"
CATEGORIES = ("replace", "add", "guard")


class EditExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op_types: list[Literal["replace", "add", "delete", "change_type"]]
    min_ops: int = Field(default=1, ge=0, le=10)
    max_ops: int = Field(default=1, ge=0, le=10)
    target_block_ids: list[str] = Field(default_factory=list)
    expected_after: dict[str, dict[str, Any]] = Field(default_factory=dict)
    after_contains: dict[str, list[str]] = Field(default_factory=dict)
    new_block_type: BlockType | None = None
    new_block_after: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.min_ops > self.max_ops:
            raise ValueError("min_ops 不能超过 max_ops")
        return self


class EditCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    instruction: str = Field(min_length=1, max_length=500)
    title: str = Field(default="编辑评测固定页面", min_length=1, max_length=100)
    theme_id: str = "ivory"
    initial_outline: OutlinePageDraft
    initial_slide: Slide
    expect: EditExpectation

    @model_validator(mode="after")
    def valid_fixture(self) -> Self:
        parts = self.id.split("/")
        if len(parts) != 2 or not all(parts) or parts[0] not in CATEGORIES:
            raise ValueError("id 必须为合法的 类别/文件名")
        if self.initial_outline.layout_id != self.initial_slide.layout_id:
            raise ValueError("固定大纲与初始页面的布局不一致")
        blocks = self.initial_slide.blocks
        ids = [block.id for block in blocks]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("初始页面内容块不能为空或有重复 id")
        tree = self.initial_slide.layout_tree
        if self.initial_slide.layout_mode == "flex":
            if tree is None:
                raise ValueError("flex 初始页面缺少布局树")
            refs = iter_leaf_block_ids(tree)
            if len(set(refs)) != len(refs) or set(refs) != set(ids):
                raise ValueError("布局树必须恰好引用每个内容块一次")
        targets = set(self.expect.target_block_ids)
        expected_targets = set(self.expect.expected_after) | set(self.expect.after_contains)
        if not targets <= set(ids) or not expected_targets <= targets | {"$new"}:
            raise ValueError("期望目标未在初始页面或目标清单中声明")
        if self.expect.new_block_after and self.expect.new_block_after not in ids:
            raise ValueError("新增块锚点不在初始页面")
        if any(
            block.type == "image" and (block.url or block.source != "placeholder")
            for block in blocks
        ):
            raise ValueError("固定夹具的图片只能使用 placeholder")
        errors = [
            issue
            for issue in validate_slide(self.initial_slide, theme_id=self.theme_id)
            if issue.severity == "error"
        ]
        if errors:
            raise ValueError(
                "初始页面格式校验失败：" + "；".join(issue.message for issue in errors)
            )
        return self

    @property
    def category(self) -> str:
        return self.id.split("/")[0]


def load_edit_cases(root: Path = EDIT_CASES_DIR) -> list[EditCase]:
    files = sorted(root.rglob("*.json"))
    if not files:
        raise ValueError(f"edit 题集目录为空：{root}")
    cases = []
    seen = set()
    mismatches = []
    for path in files:
        display = path.relative_to(root).as_posix()
        if path.parent.name not in CATEGORIES:
            raise ValueError(f"{display} 不在合法类别目录")
        try:
            case = EditCase.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"{display} 字段校验失败：{error}") from error
        if case.id in seen:
            raise ValueError(f"case id 重复：{case.id}")
        seen.add(case.id)
        if case.id != display.removesuffix(".json"):
            mismatches.append(f"{display} id 应为 {display.removesuffix('.json')}")
        cases.append(case)
    if mismatches:
        raise ValueError("；".join(mismatches))
    return sorted(cases, key=lambda item: item.id)


def compute_edit_cases_version(cases: list[EditCase]) -> str:
    payload = [case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.id)]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"edit-v2-{digest[:12]}"
