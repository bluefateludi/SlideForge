"""画布高约束（issue #32 方案 B）。

fit_tree_to_content 只按内容自然高度回填 grow，surplus<0 时后续行被原样
推出画布，生成期只能靠 #22 方向 A 的修复轮重写整页。方案 B 在布局层补上
画布高约束：越界时逐档收缩正文字号并重排——装得下就地解决、不惊动 LLM；
收缩到最小档仍装不下，才保留越界交给修复轮兜底。
"""

import uuid

import pytest

from app.domain.block_style import merge_text_style
from app.domain.flex_fit import fit_tree_to_canvas
from app.domain.flex_layout import FlexContainer, FlexLeaf
from app.domain.flex_width import fit_row_widths
from app.domain.slide_draft import (
    FlexBulletsContent,
    FlexSlideDraft,
    FlexTextContent,
    flex_draft_to_slide,
)
from app.domain.theme import resolve_theme
from app.domain.validation import validate_slide
from app.workflows.slide import build_slide_workflow, run_slide_workflow

from .test_slide_workflow import _payload

_THEME = resolve_theme("ivory")
_BULLET_UNIT = "这是一条足够长的要点，会反复折行占满整行高度，"


def _overflow_candidate(
    bullet_chars: int, items_per_block: int, block_count: int = 3
) -> FlexSlideDraft:
    """flex 草稿：标题 + N 个要点块，默认字号下按参数控制越界程度。

    块数给足 4 个（标题 + 3 块），避开「内容偏瘦」修复轮对断言的干扰，
    让修复轮是否触发只由画布约束决定。
    """
    long_text = (_BULLET_UNIT * 40)[:bullet_chars]
    return FlexSlideDraft(
        blocks=[
            FlexTextContent(id="t1", text="章节标题"),
            *(
                FlexBulletsContent(id=f"b{i}", items=[long_text] * items_per_block)
                for i in range(block_count)
            ),
        ],
        layout_tree=FlexContainer(
            type="column",
            id="root",
            children=[
                FlexLeaf(id="l1", block_id="t1", text_style="title"),
                *(FlexLeaf(id=f"l{i + 2}", block_id=f"b{i}") for i in range(block_count)),
            ],
        ),
    )


def _short_draft() -> FlexSlideDraft:
    """修复轮产出：块数与长度都健康的对照页。"""
    return _overflow_candidate(20, 2)


def _fitted_slide(draft: FlexSlideDraft):
    """按生产路径走一遍：草稿 → 定宽 → 画布约束 → 校验。"""
    from app.domain.content import Slide

    slide: Slide = flex_draft_to_slide(uuid.uuid4(), draft)
    widened = fit_row_widths(slide.layout_tree, slide.blocks, theme=_THEME)
    tree, blocks = fit_tree_to_canvas(
        widened, slide.blocks, theme=_THEME, page_role="content"
    )
    slide = slide.model_copy(update={"layout_tree": tree, "blocks": blocks})
    return slide, validate_slide(slide, theme=_THEME)


def test_canvas_fit_no_shrink_when_content_fits() -> None:
    _, issues = _fitted_slide(_overflow_candidate(30, 2))

    assert not any(issue.code == "canvas_overflow" for issue in issues)


def test_canvas_fit_shrinks_body_and_clears_gate() -> None:
    slide, issues = _fitted_slide(_overflow_candidate(115, 4))

    assert not any(issue.code == "canvas_overflow" for issue in issues)
    bullets = next(block for block in slide.blocks if block.type == "bullets")
    default_size = merge_text_style(_THEME, "bullet", None).size_pt
    assert bullets.style is not None and bullets.style.size_pt is not None
    assert bullets.style.size_pt < default_size
    # 标题是页面家具：收缩只作用于正文
    title = next(block for block in slide.blocks if block.id.endswith("t1"))
    assert title.style is None or title.style.size_pt is None


@pytest.mark.asyncio
async def test_workflow_fits_canvas_without_llm_repair() -> None:
    """中度越界应被布局层就地收缩消化：生成器只调一次，不进修复轮。"""
    calls: list[list[str]] = []

    class ScriptedFlex:
        async def generate(self, payload):
            calls.append(list(payload.issues))
            return _overflow_candidate(115, 4)

    slide, issues = await run_slide_workflow(
        build_slide_workflow(ScriptedFlex()), _payload(layout_mode="flex"), uuid.uuid4()
    )

    assert len(calls) == 1, "画布约束应就地解决越界，不应触发修复重写"
    assert not any(issue.code == "canvas_overflow" for issue in issues)
    assert slide.layout_mode == "flex"


@pytest.mark.asyncio
async def test_workflow_repair_when_shrink_floor_exceeded() -> None:
    """收缩到最小档仍装不下（如 12×330 字）：越界保留，交给 #22 方向 A 的修复轮。"""
    overflowing = _overflow_candidate(330, 4)
    short = FlexSlideDraft(
        blocks=[
            FlexTextContent(id="t1", text="章节标题"),
            FlexBulletsContent(id="b1", items=["短要点一", "短要点二"]),
        ],
        layout_tree=FlexContainer(
            type="column",
            id="root",
            children=[
                FlexLeaf(id="l1", block_id="t1", text_style="title"),
                FlexLeaf(id="l2", block_id="b1"),
            ],
        ),
    )
    prompts: list[list[str]] = []

    class ScriptedFlex:
        def __init__(self, drafts: list[FlexSlideDraft]) -> None:
            self._drafts = drafts

        async def generate(self, payload):
            prompts.append(list(payload.issues))
            return self._drafts[min(len(prompts) - 1, len(self._drafts) - 1)]

    slide, issues = await run_slide_workflow(
        build_slide_workflow(ScriptedFlex([overflowing, short])),
        _payload(layout_mode="flex"),
        uuid.uuid4(),
    )

    assert len(prompts) == 2, "最小档仍越界应触发修复重写"
    assert any("底边" in msg or "超出" in msg for msg in prompts[1])
    assert not any(issue.code == "canvas_overflow" for issue in issues)
