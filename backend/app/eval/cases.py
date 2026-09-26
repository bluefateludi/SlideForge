"""评测题集加载器：扫描、解析、校验 backend/eval/cases/ 下全部 case（eval#2）。

题集是随仓库提交的静态资产：每次评测题目一致，run 之间分数可比。
加载器对坏数据（缺字段/坏 JSON/材料缺失/重复 id）报清晰中文错误，
输出按 id 排序的有序 case 列表，供 eval#3 执行器与评分器消费。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.core.paths import REPO_ROOT

# 题集根目录：数据在 backend/eval/cases/，加载逻辑在 app/eval/（本包）。
CASES_DIR = REPO_ROOT / "backend" / "eval" / "cases"

# 类别目录白名单。新目录必须显式登记，防止拼写错误的目录静默漏题。
CATEGORIES = ("technology", "business", "education", "documents")

# id 形如 "technology/rag"：目录/文件名去后缀，不允许更深嵌套。
_MAX_ID_PARTS = 2


class EvalCase(BaseModel):
    """一道评测题：输入指令 + 期望页数 + 可判定 requirements；文档题另含材料。"""

    id: str
    input: str = Field(min_length=1)
    expected_pages: int = Field(gt=0)
    requirements: list[str] = Field(min_length=1)
    category: str
    source: Path | None = None
    # 材料绝对路径：由加载器按 source 相对 case 文件解析填充，不参与手写。
    source_path: Path | None = None

    @model_validator(mode="after")
    def _check_id_shape(self) -> Self:
        parts = self.id.split("/")
        if len(parts) != _MAX_ID_PARTS or not all(parts):
            raise ValueError(
                f"id 必须形如 \"目录/文件名\"（实际：{self.id!r}），"
                f"当前支持目录：{list(CATEGORIES)}"
            )
        if parts[0] != self.category:
            raise ValueError(
                f"id 前缀目录 {parts[0]!r} 与 category {self.category!r} 不一致"
            )
        return self


def load_cases(root: Path = CASES_DIR) -> list[EvalCase]:
    """递归扫描题集目录，校验并加载全部 case，按 id 排序返回。

    任何一道题有问题都直接抛 ValueError（中文信息含文件相对路径），
    保证评测开始前题集整体合法，不带病起跑。
    """
    files = sorted(p for p in root.rglob("*.json") if p.is_file())
    if not files:
        raise ValueError(f"题集目录为空或不存在：{root}")

    cases: list[EvalCase] = []
    seen_ids: dict[str, str] = {}
    mismatches: list[str] = []
    for path in files:
        display = _display(path, root)
        case = _load_one(path, display)
        if case.id in seen_ids:
            raise ValueError(
                f"case id 重复：{case.id!r} 同时出现在 "
                f"{seen_ids[case.id]} 与 {display}"
            )
        seen_ids[case.id] = display
        # id/文件名不一致先记录、扫完统一报：若它同时冒用了别处的 id，
        # 上面的重复 id 检查会先抛出更本质的问题
        expected_id = display.removesuffix(".json")
        if case.id != expected_id:
            mismatches.append(
                f"{display} id 应为 {expected_id!r}（目录/文件名去后缀），"
                f"实际 {case.id!r}"
            )
        cases.append(case)

    if mismatches:
        raise ValueError("；".join(mismatches))

    cases.sort(key=lambda case: case.id)
    return cases


def _display(path: Path, root: Path) -> str:
    """统一用相对题集根的正斜杠路径，错误信息在 Windows 下也稳定可读。"""
    return path.relative_to(root).as_posix()


def filter_cases(cases: list[EvalCase], spec: str) -> list[EvalCase]:
    """按逗号分隔的题号子集过滤（run_eval --cases，#32 成本约束）。

    任一题号不在题集中直接抛 ValueError 并列出全部合法题号——拼写错误
    应当当场报错，而不是静默跑空或跑偏。保持原题集顺序，与全量口径一致。
    """
    wanted = [part.strip() for part in spec.split(",") if part.strip()]
    if not wanted:
        raise ValueError("--cases 为空：请给出逗号分隔的题号，或去掉该参数跑全量")
    known = {case.id for case in cases}
    unknown = [case_id for case_id in wanted if case_id not in known]
    if unknown:
        raise ValueError(
            f"未知题号：{', '.join(unknown)}；合法题号：{', '.join(sorted(known))}"
        )
    picked = set(wanted)
    return [case for case in cases if case.id in picked]


def pick_smoke_case(cases: list[EvalCase]) -> EvalCase | None:
    """全量评测前的冒烟题：主题题里挑页数最少的（不传材料、链路最短）。

    run_eval 用它先完整跑通一次真实链路，harness 自身坏了（编排漏步骤、
    桩与真实 API 漂移）在分钟级暴露，而不是烧完一整轮 35-60 分钟后
    才从逐题 422 里发现。无主题题时返回 None（题集不满足冒烟条件，
    由调用方决定是否照常起跑）。
    """
    topic_cases = [case for case in cases if case.category != "documents"]
    if not topic_cases:
        return None
    return min(topic_cases, key=lambda case: (case.expected_pages, case.id))


def _load_one(path: Path, display: str) -> EvalCase:
    directory = path.parent.name
    if directory not in CATEGORIES:
        # 覆盖两类问题：未知目录名，以及嵌套目录（直接父目录不是类别目录）
        raise ValueError(
            f"{display} 不在合法的类别目录下（当前支持：{list(CATEGORIES)}）；"
            f"case 必须直接位于类别目录内"
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{display} 不是合法 JSON：{exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{display} 顶层必须是 JSON 对象")

    try:
        case = EvalCase.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"{display} 字段校验失败：{_format_pydantic(exc)}") from exc

    if case.category == "documents":
        _bind_source(case, path, display)

    return case


def _bind_source(case: EvalCase, path: Path, display: str) -> None:
    if case.source is None:
        raise ValueError(f"{display} 是文档题（documents/）但缺少 source 字段")
    # source 是相对本 case 文件的路径（材料与 case 同目录）
    source_path = path.parent / case.source
    if not source_path.is_file():
        raise ValueError(f"{display} 引用的材料文件不存在：{case.source}")
    case.source_path = source_path


def _format_pydantic(exc: Exception) -> str:
    """把 pydantic 校验错误压成单行可读信息。"""
    lines = []
    for err in getattr(exc, "errors", lambda: [])():
        loc = ".".join(str(part) for part in err["loc"])
        lines.append(f"{loc}: {err['msg']}")
    return "; ".join(lines) or str(exc)
