"""三路评分器：结构指标 / 内容指标（LLM judge）/ 幻觉指标（eval#3）。

执行器（runner.py）跑完单题主流程后收集 CaseArtifacts，本模块负责：
1. 结构评分：从 GET /deck 响应构建内容层 Deck，复用 run_export_check 与
   verify_pptx 的既有校验（不重造），产出 Schema 合法、页数达成、导出成功；
2. 内容评分：DeepSeek 自审作 judge——StructuredChatClient 的 json_mode
   结构化输出，逐条 requirement 判定 pass/fail 并给 0-10 总分；
3. 幻觉评分：仅文档题，app/eval/hallucination 的数字出处核对。

Token 用量口径：埋点在 worker 进程内（take_usage_records 不可跨进程读取），
HTTP 驱动的评测器无法取到，v1 按 0 记录并在报告注明，待 eval 后续票
在 API 侧暴露用量后接通。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.domain.content import Block, Deck
from app.domain.export_check import run_export_check
from app.eval.cases import EvalCase
from app.eval.hallucination import HallucinationReport, check_numbers_against_source
from app.llm.client import StructuredChatClient, create_chat_model


class JudgeRequirementResult(BaseModel):
    """judge 对单条 requirement 的判定。"""

    text: str
    passed: bool = Field(alias="pass")
    reason: str = ""

    model_config = {"populate_by_name": True}

    @property
    def label(self) -> str:
        return "通过" if self.passed else "未过"


class JudgeVerdict(BaseModel):
    """judge 结构化输出：逐条判定 + 0-10 总分。"""

    requirements: list[JudgeRequirementResult] = Field(default_factory=list)
    content_score: float = Field(ge=0, le=10)

    @property
    def coverage_rate(self) -> float:
        """需求覆盖率：通过条数 / 判定条数；无判定时记 0。"""
        if not self.requirements:
            return 0.0
        return sum(1 for item in self.requirements if item.passed) / len(self.requirements)


class StructuralMetrics(BaseModel):
    """结构路指标（复用既有校验，不重造）。"""

    schema_valid: bool  # run_export_check 无 error 级问题
    export_allowed: bool  # 同上（口径一致，分开呈现便于阅读）
    error_issue_count: int = 0
    warning_issue_count: int = 0
    issue_messages: list[str] = Field(default_factory=list)


class CaseScore(BaseModel):
    """单题三路评分汇总。"""

    structural: StructuralMetrics | None = None
    judge: JudgeVerdict | None = None
    judge_error: str | None = None
    hallucination: HallucinationReport | None = None


def deck_from_response(payload: dict) -> Deck:
    """GET /projects/{id}/deck 响应 → 内容层 Deck（只取 ready 页块）。

    响应里的 slides[].blocks 与内容层 Block 同构（schemas.deck 直接复用
    domain 模型），整个 slide 子集直接走 Deck 的嵌套校验。
    """
    slides = []
    for slide in payload.get("slides", []):
        if slide.get("status") != "ready":
            continue
        if not slide.get("blocks"):
            continue
        slides.append(
            {
                "id": slide["id"],
                "layout_id": slide.get("layout_id", "blank"),
                "layout_mode": slide.get("layout_mode") or "flex",
                "layout_tree": slide.get("layout_tree"),
                "blocks": slide["blocks"],
            }
        )
    return Deck.model_validate(
        {
            "id": str(payload.get("project_id", "eval-deck")),
            "title": payload.get("title", ""),
            "theme_id": payload.get("theme_id", "ivory"),
            "slides": slides,
        }
    )


def _block_plain_text(block: Block) -> str:
    """块 → 纯文本（judge 摘要与幻觉抽取共用）。"""
    match block.type:
        case "text":
            return block.text
        case "bullets":
            return "\n".join(block.items)
        case "kpi":
            parts = [block.value, block.label]
            if block.note:
                parts.append(block.note)
            return "\n".join(parts)
        case "table":
            rows = ["\t".join(block.header), *["\t".join(row) for row in block.rows]]
            return "\n".join(rows)
        case "chart":
            series = " ".join(
                f"{item.name}:{'/'.join(str(v) for v in item.values)}" for item in block.series
            )
            return f"{' '.join(block.categories)} {series}"
        case "image":
            return block.alt
        case "cards":
            return "\n".join(f"{item.title}\n{item.desc}" for item in block.items)
        case "callout":
            return block.text
        case _:
            return ""


def deck_text_summary(deck: Deck, *, max_chars_per_slide: int = 600) -> str:
    """deck → 纯文本摘要（每页标题 + 文本块内容），给 judge 看的形态。

    不把整个 JSON 塞给模型：只保留可读文本，超出单页上限截断，
    避免长表格把 prompt 撑爆。
    """
    lines: list[str] = []
    for index, slide in enumerate(deck.slides, start=1):
        parts = [f"第 {index} 页：{slide.id}"]
        body: list[str] = []
        for block in slide.blocks:
            text = _block_plain_text(block).strip()
            if text:
                body.append(text)
        joined = "\n".join(body)
        if len(joined) > max_chars_per_slide:
            joined = joined[:max_chars_per_slide] + "…"
        if joined:
            parts.append(joined)
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def deck_full_text(deck: Deck, *, title_by_slide_id: dict[str, str] | None = None) -> str:
    """deck 全部可见文本 + 备注幻灯片标题（幻觉核对用）。"""
    titles = title_by_slide_id or {}
    chunks: list[str] = [deck.title]
    for slide in deck.slides:
        title = titles.get(slide.id, "")
        if title:
            chunks.append(title)
        for block in slide.blocks:
            text = _block_plain_text(block).strip()
            if text:
                chunks.append(text)
        if slide.speaker_notes:
            chunks.append(slide.speaker_notes)
    return "\n".join(chunks)


def score_structure(deck: Deck, *, content_density: str | None = None) -> StructuralMetrics:
    """结构评分：复用 run_export_check（质量检查 + 导出检查同源）。"""
    report = run_export_check(deck, content_density=content_density)
    errors = [issue for issue in report.issues if issue.severity == "error"]
    warnings = [issue for issue in report.issues if issue.severity == "warning"]
    return StructuralMetrics(
        schema_valid=report.export_allowed,
        export_allowed=report.export_allowed,
        error_issue_count=len(errors),
        warning_issue_count=len(warnings),
        issue_messages=[f"[{issue.severity}] {issue.message}" for issue in report.issues],
    )


_JUDGE_SYSTEM_PROMPT = (
    "你是 PPT 内容评审助手。必须只输出一个 JSON 对象，不要 Markdown，不要额外说明。\n"
    "JSON 结构必须为：\n"
    '{"requirements":[{"text":"...","pass":true,"reason":"..."}],'
    '"content_score":7.5}\n'
    "判定规则：\n"
    "1. requirements 数组逐条对应给定的需求清单，text 原样回填。\n"
    "2. pass 为布尔值：PPT 文本中能找到满足该需求的具体内容（数字、名称、结论）"
    "才算 true；只有笼统表述不算。\n"
    "3. reason 一句话说明依据（找到了什么 / 缺了什么）。\n"
    "4. content_score 是 0-10 的整体内容质量分：信息具体性、结构完整性、"
    "与原始指令的贴合度，可以有 0.5 粒度。"
)


class DeckJudge:
    """DeepSeek 同模型自审 judge：json_mode 结构化输出，Pydantic 约束。"""

    def __init__(self, chat: StructuredChatClient) -> None:
        self._chat = chat

    @classmethod
    def for_settings(cls, settings) -> DeckJudge:
        """从应用配置构造（评测脚本用；测试注入 fake chat 走 __init__）。"""
        return cls(
            StructuredChatClient(model=create_chat_model(settings), api_key=settings.llm_api_key)
        )

    async def judge(self, case: EvalCase, deck: Deck) -> JudgeVerdict:
        summary = deck_text_summary(deck)
        user = (
            f"用户的原始指令：\n{case.input}\n\n"
            f"需求清单：\n{json.dumps(case.requirements, ensure_ascii=False)}\n\n"
            f"生成的 PPT 文本摘要（每页标题与文本块内容）：\n{summary}"
        )
        return await self._chat.complete(
            JudgeVerdict,
            system=_JUDGE_SYSTEM_PROMPT,
            user=user,
            purpose="评测内容评审",
        )


def score_hallucination(deck: Deck, source_text: str) -> HallucinationReport:
    """幻觉评分：deck 全文数字回材料核对（仅文档题调用）。"""
    return check_numbers_against_source(deck_full_text(deck), source_text)
