"""生成期画布底边检查（issue #22 方向 A）。

flex 页 fit_tree_to_content 按内容撑高后，后续行可能被推出画布；
该规则原先只在导出期（export_check.check_content_overflows_canvas）
作为 error 暴露，生成期看不见也不自纠。现在生成期 check 节点同样
跑这条规则（fixed/flex 都查），让自纠回路在生成阶段就看得见越界。
"""

import uuid

import pytest

from app.domain.flex_layout import FlexContainer, FlexLeaf
from app.domain.slide_draft import (
    BulletsContent,
    FlexBulletsContent,
    FlexSlideDraft,
    FlexTextContent,
    SlideDraft,
    TextContent,
    draft_to_slide,
    flex_draft_to_slide,
)
from app.domain.validation import validate_slide
from app.workflows.slide import build_slide_workflow, run_slide_workflow

from .test_slide_workflow import _payload


def _flex_column(*leaf_ids: str) -> FlexContainer:
    return FlexContainer(
        type="column",
        id="root",
        children=[FlexLeaf(id=f"l{n}", block_id=bid) for n, bid in enumerate(leaf_ids, 1)],
    )


def _overflowing_flex_draft() -> FlexSlideDraft:
    """构造一个正常字号下必然把最后一块推出画布底边的 flex 草稿。"""
    long_bullets = ["这是一条足够长的要点，会反复折行占满整行高度，" * 8] * 12
    return FlexSlideDraft(
        blocks=[
            FlexTextContent(id="t1", text="章节标题"),
            FlexBulletsContent(id="b1", items=["短要点一", "短要点二"]),
            FlexBulletsContent(id="b2", items=long_bullets),
        ],
        layout_tree=_flex_column("t1", "b1", "b2"),
    )


def _canvas_overflow_errors(issues: list) -> list:
    return [
        issue for issue in issues if issue.severity == "error" and issue.code == "canvas_overflow"
    ]


def test_validate_flex_slide_flags_canvas_bottom_overflow_as_error() -> None:
    slide = flex_draft_to_slide(uuid.uuid4(), _overflowing_flex_draft())

    issues = validate_slide(slide, theme_id="ivory")

    assert _canvas_overflow_errors(issues), f"应产生画布底边 error，实际 issues={issues}"


def test_validate_fixed_slide_also_flags_canvas_bottom_overflow() -> None:
    """导出期检查遍历全部页，生成期不能只挂 flex：fixed 底槽长文同样要拦。"""
    draft = SlideDraft(
        blocks=[
            TextContent(slot_id="title", text="章节标题"),
            BulletsContent(
                slot_id="body",
                items=["固定布局底部长文要点，占用高度必然越过画布底边，" * 8] * 10,
            ),
        ]
    )
    slide = draft_to_slide(uuid.uuid4(), "bullets", draft)

    issues = validate_slide(slide, theme_id="ivory")

    assert _canvas_overflow_errors(issues), f"fixed 页越界应同样报 error，实际 issues={issues}"


def test_is_repair_worthy_allows_canvas_overflow() -> None:
    from app.domain.quality import is_repair_worthy
    from app.domain.validation import StructureIssue

    issue = StructureIssue(
        severity="error",
        slide_id="s1",
        slot_id="b1",
        message="内容渲染后将超出页面底边",
        code="canvas_overflow",
    )
    assert is_repair_worthy(issue)


@pytest.mark.asyncio
async def test_workflow_repairs_canvas_bottom_overflow() -> None:
    """越界 flex 页应触发一轮自纠：修复轮的 prompt 里带上越界反馈。"""
    overflowing = _overflowing_flex_draft()
    short = FlexSlideDraft(
        blocks=[
            FlexTextContent(id="t1", text="章节标题"),
            FlexBulletsContent(id="b1", items=["短要点一", "短要点二"]),
        ],
        layout_tree=_flex_column("t1", "b1"),
    )

    prompts: list[list[str]] = []

    class ScriptedFlex:
        def __init__(self, drafts: list[FlexSlideDraft]) -> None:
            self._drafts = drafts

        async def generate(self, payload):
            prompts.append(list(payload.issues))
            return self._drafts[min(len(prompts) - 1, len(self._drafts) - 1)]

    generator = ScriptedFlex([overflowing, short])
    workflow = build_slide_workflow(generator)

    slide, issues = await run_slide_workflow(workflow, _payload(layout_mode="flex"), uuid.uuid4())

    assert len(prompts) == 2, "越界 error 应触发一轮 repair 重写"
    assert any("底边" in msg or "超出" in msg for msg in prompts[1])
    assert not _canvas_overflow_errors(issues)
    assert slide.layout_mode == "flex"
