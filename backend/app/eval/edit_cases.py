"""ai_edit 工具环评测题集加载器（obs/10）。

题集是仓库内静态资产（backend/eval/edit_cases/<类别>/<case>.json），
与主评测题集同套路：坏数据在起跑前就报清晰中文错误。
每道题 = 指令 + 种子材料（1 个 topic 项目跑出真实页面）+ 规则断言
（操作类型 / 操作数上限 / 结果包含关键词 / locked 块零误改）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.core.paths import REPO_ROOT

EDIT_CASES_DIR = REPO_ROOT / "backend" / "eval" / "edit_cases"

# 类别目录白名单：replace（替换既有块）/ add（新增块）/ guard（防护断言）
CATEGORIES = ("replace", "add", "guard")


class EditSeed(BaseModel):
    """种出一个真实项目：topic 材料 + 页数（首页为封面，取内容最丰富页作为靶页）。"""

    topic: str = Field(min_length=10)
    page_count: int = Field(default=2, ge=2, le=3)
    title: str = Field(min_length=1, max_length=100)


class EditExpectation(BaseModel):
    op_types: list[str] = Field(min_length=1)
    max_ops: int = Field(ge=1, le=10)
    after_contains: list[str] = Field(default_factory=list, max_length=3)
    locked_zero_edit: bool = True


class EditCase(BaseModel):
    id: str
    instruction: str = Field(min_length=1, max_length=500)
    seed: EditSeed
    # none：不锁块；body：锁靶页第一个非标题 text/bullets 块；title：锁第一个 text 块
    lock: str = "none"
    expect: EditExpectation

    @model_validator(mode="after")
    def _check_id_shape(self) -> Self:
        parts = self.id.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f'id 必须形如 "类别/文件名"（实际：{self.id!r}）')
        if parts[0] not in CATEGORIES:
            raise ValueError(f"id 前缀 {parts[0]!r} 不在类别白名单 {list(CATEGORIES)}")
        if self.lock not in {"none", "body", "title"}:
            raise ValueError(f"lock 取值必须是 none/body/title（实际 {self.lock!r}）")
        return self

    @property
    def category(self) -> str:
        return self.id.split("/")[0]


def load_edit_cases(root: Path = EDIT_CASES_DIR) -> list[EditCase]:
    """扫描并校验全部 edit 题，按 id 排序；题集为空或坏数据直接抛错。"""
    files = sorted(p for p in root.rglob("*.json") if p.is_file())
    if not files:
        raise ValueError(f"edit 题集目录为空或不存在：{root}")
    cases: list[EditCase] = []
    seen: dict[str, str] = {}
    for path in files:
        display = path.relative_to(root).as_posix()
        if path.parent.name not in CATEGORIES:
            raise ValueError(
                f"{display} 不在合法类别目录下（{list(CATEGORIES)}）；case 必须直接位于类别目录内"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{display} 不是合法 JSON：{exc}") from exc
        try:
            case = EditCase.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"{display} 字段校验失败：{exc}") from exc
        if case.id in seen:
            raise ValueError(f"case id 重复：{case.id!r}（{seen[case.id]} 与 {display}）")
        seen[case.id] = display
        expected_id = display.removesuffix(".json")
        if case.id != expected_id:
            raise ValueError(f"{display} id 应为 {expected_id!r}，实际 {case.id!r}")
        cases.append(case)
    cases.sort(key=lambda case: case.id)
    return cases


def compute_edit_cases_version(cases: list[EditCase]) -> str:
    """题集内容指纹（与主评测 compute_cases_version 同套路）。"""
    import hashlib

    payload = [
        {
            "id": case.id,
            "instruction": case.instruction,
            "seed": case.seed.model_dump(),
            "lock": case.lock,
            "expect": case.expect.model_dump(),
        }
        for case in cases
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"edit-{digest[:12]}"


__all__ = [
    "CATEGORIES",
    "EDIT_CASES_DIR",
    "EditCase",
    "EditExpectation",
    "EditSeed",
    "compute_edit_cases_version",
    "load_edit_cases",
]
