"""评分器纯逻辑（eval#3）：deck 构建、文本摘要、结构评分、judge 结构化输出。

fake chat 只回 Pydantic 对象，不触网、不消耗 API 额度。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from app.domain.content import Deck
from app.eval.cases import EvalCase
from app.eval.evaluator import (
    DeckJudge,
    JudgeVerdict,
    deck_from_response,
    deck_full_text,
    deck_text_summary,
    score_hallucination,
    score_structure,
)


def _flex_tree(block_ids: list[str]) -> dict:
    """合法的最小 flex 布局树：纵向容器逐块排布。"""
    return {
        "type": "column",
        "id": "root",
        "children": [{"type": "block", "id": f"leaf-{bid}", "block_id": bid} for bid in block_ids],
    }


def _slide(
    slide_id: str,
    blocks: list[dict],
    *,
    status: str = "ready",
    title: str = "标题",
    layout_mode: str = "flex",
) -> dict:
    return {
        "id": slide_id,
        "layout_id": "two-column",
        "layout_mode": layout_mode,
        "layout_tree": _flex_tree([b["id"] for b in blocks]) if layout_mode == "flex" else None,
        "title": title,
        "status": status,
        "blocks": blocks,
    }


def _deck_response() -> dict:
    return {
        "project_id": "p1",
        "title": "评测演示",
        "theme_id": "ivory",
        "status": "ready",
        "total": 2,
        "ready": 2,
        "failed": 0,
        "slides": [
            _slide(
                "s1",
                [
                    {"id": "b1", "slot_id": "title", "type": "text", "text": "营收概览"},
                    {
                        "id": "b2",
                        "slot_id": "body",
                        "type": "bullets",
                        "items": ["Q3 营收 1.86 亿元", "同比 +24%"],
                    },
                ],
            ),
            _slide(
                "s2",
                [
                    {
                        "id": "b3",
                        "slot_id": "kpi",
                        "type": "kpi",
                        "value": "126 家",
                        "label": "大客户",
                        "note": "同比 +28.6%",
                    }
                ],
            ),
        ],
    }


class TestDeckFromResponse:
    def test_builds_content_deck_from_ready_slides(self) -> None:
        deck = deck_from_response(_deck_response())
        assert isinstance(deck, Deck)
        assert deck.title == "评测演示"
        assert deck.theme_id == "ivory"
        assert len(deck.slides) == 2
        types = {block.type for slide in deck.slides for block in slide.blocks}
        assert types == {"text", "bullets", "kpi"}

    def test_skips_non_ready_or_empty_slides(self) -> None:
        payload = _deck_response()
        payload["slides"][1]["status"] = "failed"
        deck = deck_from_response(payload)
        assert len(deck.slides) == 1


class TestDeckTextSummary:
    def test_summary_contains_page_numbers_and_text(self) -> None:
        deck = deck_from_response(_deck_response())
        summary = deck_text_summary(deck)
        assert "第 1 页" in summary
        assert "Q3 营收 1.86 亿元" in summary
        assert "126 家" in summary
        # 不把整个 JSON 塞给 judge：无引号包裹的键名
        assert '"slot_id"' not in summary

    def test_summary_truncates_overlong_slide(self) -> None:
        deck = deck_from_response(
            {
                **_deck_response(),
                "slides": [
                    _slide(
                        "s1",
                        [
                            {
                                "id": "b1",
                                "slot_id": "body",
                                "type": "text",
                                "text": "长" * 1000,
                            }
                        ],
                    )
                ],
            }
        )
        summary = deck_text_summary(deck, max_chars_per_slide=100)
        body = summary.split("第 1 页：s1", 1)[1]
        assert len(body.strip()) <= 101  # 100 字 + 省略号


class TestDeckFullText:
    def test_includes_titles_notes_and_all_block_text(self) -> None:
        deck = deck_from_response(_deck_response())
        titles = {"s1": "营收概览页", "s2": "客户结构页"}
        text = deck_full_text(deck, title_by_slide_id=titles)
        assert "营收概览页" in text
        assert "客户结构页" in text
        assert "同比 +28.6%" in text

    def test_includes_speaker_notes(self) -> None:
        deck = deck_from_response(_deck_response())
        deck.slides[0].speaker_notes = "本页强调 24% 的增长"
        assert "本页强调 24% 的增长" in deck_full_text(deck)


class TestScoreStructure:
    def test_valid_deck_passes_schema_check(self) -> None:
        deck = deck_from_response(_deck_response())
        metrics = score_structure(deck)
        # 该 deck 未配 flex 布局树：fixed 槽位校验按布局定义走，
        # 不应产生 error 级问题（warning 可能存在，不阻断）
        assert metrics.error_issue_count == 0
        assert metrics.schema_valid is True

    def test_error_issues_mark_schema_invalid(self) -> None:
        # 直接构造会触发 error 的场景：块 id 重复导致放置冲突较难稳定构造，
        # 用猴补的方式验证指标映射逻辑本身
        from app.eval import evaluator

        class _FakeReport(BaseModel):
            class Issue(BaseModel):
                severity: str
                message: str

            issues: list
            export_allowed: bool
            fonts_precise: bool = True

        fake = _FakeReport(
            issues=[
                _FakeReport.Issue(severity="error", message="槽位超出画布"),
                _FakeReport.Issue(severity="warning", message="文字偏多"),
            ],
            export_allowed=False,
        )

        def _fake_run(deck, **kwargs: Any):
            return fake

        original = evaluator.run_export_check
        evaluator.run_export_check = _fake_run
        try:
            metrics = score_structure(deck_from_response(_deck_response()))
        finally:
            evaluator.run_export_check = original

        assert metrics.schema_valid is False
        assert metrics.export_allowed is False
        assert metrics.error_issue_count == 1
        assert metrics.warning_issue_count == 1
        assert metrics.issue_messages[0].startswith("[error]")


class TestJudgeVerdict:
    def test_parses_pass_field_alias(self) -> None:
        verdict = JudgeVerdict.model_validate(
            {
                "requirements": [
                    {"text": "包含营收数字", "pass": True, "reason": "找到 1.86 亿元"},
                    {"text": "包含同比", "pass": False, "reason": "未出现同比数据"},
                ],
                "content_score": 7.5,
            }
        )
        assert verdict.requirements[0].passed is True
        assert verdict.requirements[1].passed is False
        assert verdict.coverage_rate == 0.5
        assert verdict.content_score == 7.5

    def test_coverage_rate_zero_when_empty(self) -> None:
        verdict = JudgeVerdict.model_validate({"requirements": [], "content_score": 0})
        assert verdict.coverage_rate == 0.0

    def test_content_score_bounds_enforced(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            JudgeVerdict.model_validate({"requirements": [], "content_score": 11})


class _FakeChat:
    """伪 StructuredChatClient：记录 prompt 并返回预置 verdict。"""

    def __init__(self, verdict: JudgeVerdict) -> None:
        self.verdict = verdict
        self.calls: list[dict] = []

    async def complete(
        self, schema: type[BaseModel], *, system: str, user: str, purpose: str
    ) -> BaseModel:
        self.calls.append({"schema": schema, "system": system, "user": user, "purpose": purpose})
        return self.verdict


class TestDeckJudge:
    async def test_judge_sends_requirements_and_summary(self) -> None:
        verdict = JudgeVerdict.model_validate(
            {
                "requirements": [{"text": "包含营收数字", "pass": True, "reason": "有"}],
                "content_score": 8.0,
            }
        )
        chat = _FakeChat(verdict)
        judge = DeckJudge(chat)  # type: ignore[arg-type]
        case = EvalCase(
            id="documents/q3_sales",
            input="根据材料生成《Q3 销售总结》的 8 页 PPT",
            expected_pages=8,
            requirements=["包含营收数字"],
            category="documents",
        )
        deck = deck_from_response(_deck_response())

        result = await judge.judge(case, deck)

        assert result is verdict
        assert len(chat.calls) == 1
        call = chat.calls[0]
        assert call["schema"] is JudgeVerdict
        assert call["purpose"] == "评测内容评审"
        assert "包含营收数字" in call["user"]
        assert case.input in call["user"]
        assert "Q3 营收 1.86 亿元" in call["user"]


class TestScoreHallucination:
    def test_documents_flow_uses_full_text(self) -> None:
        deck = deck_from_response(_deck_response())
        source = "Q3 营收 1.86 亿元 大客户 126 家 同比 24% 28.6%"
        report = score_hallucination(deck, source)
        assert report.total_numbers >= 4
        assert report.fabricated_numbers == 0
