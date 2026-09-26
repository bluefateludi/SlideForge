"""指标聚合与控制台报告（eval#3）。

run 级口径（PR 描述与报告尾部注明）：
- 生成成功率 = ok 题 / 总题（ok 指主流程跑完且全部页 ready）
- Schema 合法率 = 无 error 级质检问题的题 / 有结构评分的题
- 页数达成率 = Σ(ready 页数达标) / 有 deck 的题（按题二值计）
- 需求覆盖分 = judge 平均 coverage_rate（0-1）与平均 content_score（0-10）
- 幻觉率 = Σ编造数字 / Σ提取数字（仅文档题）
- 平均耗时：题均值；平均 Token：成功题的每题 token 总和取均值
  （token 来自 spans 聚合，见 trace_join.py；查不到 trace 的题按 0 参与均值）
- 失败率 = 失败题 / 总题
- 重试率 = 出现过 failed 的页数 / 总页数（ARQ 层 job 重试不可见于 HTTP 的
  近似口径，另记评测器主动触发的逐页重试次数）
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.eval.evaluator import CaseScore
from app.eval.runner import CaseArtifacts

_REQUIRED_COLUMNS = (
    "题号",
    "结果",
    "页数",
    "结构",
    "覆盖",
    "评分",
    "幻觉率",
    "重试",
    "耗时",
    "失败原因",
)


class CaseRow(BaseModel):
    """逐题明细行（报告与未来 eval#4 入库共用）。"""

    case_id: str
    ok: bool
    stage: str
    error: str | None = None
    ready_pages: int = 0
    expected_pages: int = 0
    pages_met: bool = False
    schema_valid: bool | None = None
    export_succeeded: bool = False
    judge_coverage: float | None = None
    judge_score: float | None = None
    judge_error: str | None = None
    hallucination_rate: float | None = None
    fabricated_numbers: int | None = None
    total_numbers: int | None = None
    failed_slide_seen: int = 0
    retried_slides: int = 0
    total_slides: int = 0
    elapsed_seconds: float = 0.0
    # token 与分段耗时：trace_join 按 artifacts 的 trace_id 从 spans 聚合
    # 并进明细（persistence 写库时补齐）；这里保留默认 0 供纯内存报告。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # "trace"：spans 聚合的真实值；"unavailable"：查不到（按 0 展示）
    tokens_source: str = "unavailable"
    outline_trace_id: str | None = None
    deck_trace_id: str | None = None
    outline_duration_ms: int = 0
    slide_durations_ms: list[int] = Field(default_factory=list)
    export_duration_ms: int = 0
    # obs#9：成本口径。cost 为 token×单价 + AI 生图张数×单价（人民币元），
    # 单价未配置时为 0；ai_image_count 是计费张数（image.ai succeeded）
    ai_image_count: int = 0
    cost: float = 0.0
    # 修复收敛（obs/10）：进入修复轮的页数 / 其中最终成功的页数
    repair_spans: int = 0
    repaired_succeeded: int = 0


class RunSummary(BaseModel):
    """run 级聚合指标。"""

    total_cases: int = 0
    ok_cases: int = 0
    success_rate: float = 0.0
    failure_rate: float = 0.0
    schema_valid_cases: int = 0
    schema_scored_cases: int = 0
    schema_valid_rate: float = 0.0
    pages_met_cases: int = 0
    pages_scored_cases: int = 0
    pages_met_rate: float = 0.0
    export_succeeded_cases: int = 0
    avg_judge_coverage: float | None = None
    avg_judge_score: float | None = None
    judge_failed_cases: int = 0
    doc_total_numbers: int = 0
    doc_fabricated_numbers: int = 0
    hallucination_rate: float | None = None
    avg_elapsed_seconds: float = 0.0
    avg_prompt_tokens: float = 0.0
    avg_completion_tokens: float = 0.0
    # 分段耗时均耗（秒；成功题均值，与 avg_prompt_tokens 同口径）：
    # 大纲段（outline.* span）、每页 task span 的平均单页耗时、导出段
    avg_outline_seconds: float = 0.0
    avg_slide_seconds: float = 0.0
    avg_export_seconds: float = 0.0
    # tokens 来源为 trace 的成功题数（小于 ok_cases 即有题查不到 trace）
    token_joined_cases: int = 0
    # 成本口径（obs#9）：avg_cost 与 avg_prompt_tokens 同分母（成功题均值，
    # 便于与 token 均值对照）；total_cost 是全部题的实际花费合计——
    # 失败题同样烧了 token，总花费必须把它算进去
    avg_cost: float = 0.0
    total_cost: float = 0.0
    # 修复收敛（obs/10，成功题口径）：首过率 = 未进修复的页占比；
    # 修复成功率 = 进修复的页最终成功占比（修复轮上限 1）
    slide_first_pass_rate: float | None = None
    slide_repair_success_rate: float | None = None
    total_failed_slides: int = 0
    total_slides: int = 0
    retry_slide_rate: float | None = None
    total_retried_slides: int = 0


class EvalReport(BaseModel):
    """整轮评测结果：汇总 + 逐题行。"""

    summary: RunSummary = Field(default_factory=RunSummary)
    rows: list[CaseRow] = Field(default_factory=list)


def build_row(
    case_id: str, expected_pages: int, artifacts: CaseArtifacts, score: CaseScore
) -> CaseRow:
    """单题产物 + 评分 → 明细行（纯函数）。"""
    ready = 0
    if artifacts.deck_response is not None:
        ready = sum(
            1
            for slide in artifacts.deck_response.get("slides", [])
            if slide.get("status") == "ready"
        )
    row = CaseRow(
        case_id=case_id,
        ok=artifacts.ok,
        stage=artifacts.stage,
        error=artifacts.error,
        ready_pages=ready,
        expected_pages=expected_pages,
        pages_met=ready == expected_pages and ready > 0,
        schema_valid=score.structural.schema_valid if score.structural else None,
        export_succeeded=artifacts.export_succeeded,
        judge_error=score.judge_error,
        failed_slide_seen=artifacts.failed_slide_seen,
        retried_slides=artifacts.retried_slides,
        total_slides=artifacts.total_slides,
        elapsed_seconds=artifacts.elapsed_seconds,
        outline_trace_id=artifacts.outline_trace_id,
        deck_trace_id=artifacts.deck_trace_id,
    )
    if score.judge is not None:
        row.judge_coverage = score.judge.coverage_rate
        row.judge_score = score.judge.content_score
    if score.hallucination is not None:
        row.hallucination_rate = score.hallucination.hallucination_rate
        row.fabricated_numbers = score.hallucination.fabricated_numbers
        row.total_numbers = score.hallucination.total_numbers
    return row


def aggregate(rows: list[CaseRow]) -> RunSummary:
    """逐题行 → run 级指标（纯函数，无 IO）。"""
    summary = RunSummary(total_cases=len(rows))
    if not rows:
        return summary

    summary.ok_cases = sum(1 for row in rows if row.ok)
    summary.success_rate = summary.ok_cases / summary.total_cases
    summary.failure_rate = 1 - summary.success_rate

    schema_rows = [row for row in rows if row.schema_valid is not None]
    summary.schema_scored_cases = len(schema_rows)
    summary.schema_valid_cases = sum(1 for row in schema_rows if row.schema_valid)
    if schema_rows:
        summary.schema_valid_rate = summary.schema_valid_cases / len(schema_rows)

    deck_rows = [row for row in rows if row.ready_pages > 0 or row.ok]
    summary.pages_scored_cases = len(deck_rows)
    summary.pages_met_cases = sum(1 for row in deck_rows if row.pages_met)
    if deck_rows:
        summary.pages_met_rate = summary.pages_met_cases / len(deck_rows)

    summary.export_succeeded_cases = sum(1 for row in rows if row.export_succeeded)

    judge_rows = [row for row in rows if row.judge_coverage is not None]
    if judge_rows:
        summary.avg_judge_coverage = sum(row.judge_coverage or 0 for row in judge_rows) / len(
            judge_rows
        )
        summary.avg_judge_score = sum(row.judge_score or 0 for row in judge_rows) / len(judge_rows)
    summary.judge_failed_cases = sum(1 for row in rows if row.judge_error)

    doc_rows = [row for row in rows if row.total_numbers is not None]
    if doc_rows:
        summary.doc_total_numbers = sum(row.total_numbers or 0 for row in doc_rows)
        summary.doc_fabricated_numbers = sum(row.fabricated_numbers or 0 for row in doc_rows)
        if summary.doc_total_numbers:
            summary.hallucination_rate = summary.doc_fabricated_numbers / summary.doc_total_numbers
        else:
            summary.hallucination_rate = 0.0

    summary.avg_elapsed_seconds = sum(row.elapsed_seconds for row in rows) / len(rows)
    # Token 口径（obs#4）：成功题的每题 token 总和（outline+deck 两段 trace
    # 的 llm span 聚合，见 trace_join.py）取均值，与 avg_elapsed 的分母
    # 语义对齐（成功题）；查不到 trace 的题按 0 参与。
    ok_rows = [row for row in rows if row.ok]
    if ok_rows:
        summary.avg_prompt_tokens = sum(row.prompt_tokens for row in ok_rows) / len(ok_rows)
        summary.avg_completion_tokens = (
            sum(row.completion_tokens for row in ok_rows) / len(ok_rows)
        )
        summary.avg_outline_seconds = (
            sum(row.outline_duration_ms for row in ok_rows) / len(ok_rows) / 1000
        )
        slide_seconds = [ms / 1000 for row in ok_rows for ms in row.slide_durations_ms]
        summary.avg_slide_seconds = (
            sum(slide_seconds) / len(slide_seconds) if slide_seconds else 0.0
        )
        summary.avg_export_seconds = (
            sum(row.export_duration_ms for row in ok_rows) / len(ok_rows) / 1000
        )
        summary.token_joined_cases = sum(
            1 for row in ok_rows if row.tokens_source == "trace"
        )
        summary.avg_cost = sum(row.cost for row in ok_rows) / len(ok_rows)
    else:
        summary.avg_prompt_tokens = 0.0
        summary.avg_completion_tokens = 0.0

    # 总花费按全部题累计：失败题同样消耗 token 与生图配额
    summary.total_cost = sum(row.cost for row in rows)

    # 修复收敛（成功题口径，与 token 均值同分母）
    ok_slide_total = sum(len(row.slide_durations_ms) for row in ok_rows)
    ok_repair_total = sum(row.repair_spans for row in ok_rows)
    ok_repaired_succeeded = sum(row.repaired_succeeded for row in ok_rows)
    if ok_slide_total:
        summary.slide_first_pass_rate = (ok_slide_total - ok_repair_total) / ok_slide_total
    if ok_repair_total:
        summary.slide_repair_success_rate = ok_repaired_succeeded / ok_repair_total

    summary.total_failed_slides = sum(row.failed_slide_seen for row in rows)
    summary.total_slides = sum(row.total_slides for row in rows)
    summary.total_retried_slides = sum(row.retried_slides for row in rows)
    if summary.total_slides:
        summary.retry_slide_rate = summary.total_failed_slides / summary.total_slides
    return summary


def build_report(results: list[tuple[str, int, CaseArtifacts, CaseScore]]) -> EvalReport:
    rows = [
        build_row(case_id, expected, artifacts, score)
        for case_id, expected, artifacts, score in results
    ]
    return EvalReport(summary=aggregate(rows), rows=rows)


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _fmt_float(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def format_report(report: EvalReport) -> str:
    """控制台整份报告：run 级汇总 + 逐题对齐明细 + 口径注记。"""
    s = report.summary
    lines: list[str] = []
    lines.append("=== SlideForge 评测报告 ===")
    lines.append(
        f"题数：{s.total_cases}　成功：{s.ok_cases}（{_fmt_pct(s.success_rate)}）"
        f"　失败率：{_fmt_pct(s.failure_rate)}"
    )
    lines.append(
        f"Schema 合法率：{s.schema_valid_cases}/{s.schema_scored_cases}"
        f"（{_fmt_pct(s.schema_valid_rate)}）"
    )
    lines.append(
        f"页数达成率：{s.pages_met_cases}/{s.pages_scored_cases}（{_fmt_pct(s.pages_met_rate)}）"
    )
    lines.append(f"导出成功：{s.export_succeeded_cases}/{s.total_cases}")
    lines.append(
        f"需求覆盖分：覆盖 {_fmt_pct(s.avg_judge_coverage)}"
        f"　内容均分 {_fmt_float(s.avg_judge_score)}/10"
        + (f"　judge 失败 {s.judge_failed_cases} 题" if s.judge_failed_cases else "")
    )
    lines.append(
        f"幻觉率（文档题）：{s.doc_fabricated_numbers}/{s.doc_total_numbers}"
        f"（{_fmt_pct(s.hallucination_rate)}）"
    )
    lines.append(f"平均耗时：{_fmt_float(s.avg_elapsed_seconds)}s")
    lines.append(
        f"平均 Token：prompt {_fmt_float(s.avg_prompt_tokens, 0)} / "
        f"completion {_fmt_float(s.avg_completion_tokens, 0)}"
        f"（口径：按 trace join spans 聚合，成功题均值；查不到 trace 的题按 0）"
    )
    lines.append(
        f"成本：平均 {_fmt_float(s.avg_cost, 4)} 元 / 题　合计 {_fmt_float(s.total_cost, 4)} 元"
        f"（口径：token×单价 + AI 生图张数×单价；单价走 env，未配置则记 0）"
    )
    lines.append(
        f"首过合法率：{_fmt_pct(s.slide_first_pass_rate)}"
        f"　修复成功率：{_fmt_pct(s.slide_repair_success_rate)}"
        f"（口径：成功题的页级 span 统计；修复轮上限 1）"
    )
    lines.append(
        f"重试率：{s.total_failed_slides}/{s.total_slides}（{_fmt_pct(s.retry_slide_rate)}）"
        f"　主动逐页重试 {s.total_retried_slides} 次"
    )
    lines.append("")
    lines.append("--- 逐题明细 ---")

    widths = {
        "题号": max(len("题号"), *(len(row.case_id) for row in report.rows))
        if report.rows
        else len("题号"),
        "结果": 4,
        "页数": 7,
        "结构": 4,
        "覆盖": 5,
        "评分": 4,
        "幻觉率": 6,
        "重试": 6,
        "耗时": 7,
        "失败原因": 20,
    }
    header = "  ".join(name.ljust(widths[name]) for name in _REQUIRED_COLUMNS)
    lines.append(header)
    for row in report.rows:
        values = {
            "题号": row.case_id,
            "结果": "成功" if row.ok else "失败",
            "页数": f"{row.ready_pages}/{row.expected_pages}",
            "结构": ("合法" if row.schema_valid else "error")
            if row.schema_valid is not None
            else "-",
            "覆盖": _fmt_pct(row.judge_coverage),
            "评分": _fmt_float(row.judge_score),
            "幻觉率": _fmt_pct(row.hallucination_rate),
            "重试": f"{row.failed_slide_seen}/{row.total_slides}",
            "耗时": f"{row.elapsed_seconds:.1f}s",
            "失败原因": (row.error or row.judge_error or ("@" + row.stage if not row.ok else ""))[
                :48
            ],
        }
        lines.append("  ".join(values[name].ljust(widths[name]) for name in _REQUIRED_COLUMNS))

    lines.append("")
    lines.append("--- 口径注记 ---")
    lines.append(
        "- 幻觉率：数字子串核对（含年份豁免与序号过滤），不懂语义；"
        "材料 1.86 亿 vs deck 18600 万 这类换算不识别，详见 app/eval/hallucination.py 模块头。"
    )
    lines.append(
        "- 重试率：ARQ 层 job 重试不可见于 HTTP，按「生成阶段出现过 failed 状态的页数 / 总页数」"
        "近似；另有评测器主动触发的逐页重试次数。"
    )
    lines.append(
        "- 平均 Token：按题的 trace_id join spans 表聚合（llm span 求和）；"
        "成功题均值，查不到 trace 的题按 0 并标注来源。"
    )
    lines.append(
        "- 成本：llm token（百万 token 单价）+ AI 生图张数（image.ai succeeded，"
        "每张单价）；单价经 LLM_PRICE_PER_MTOK_* / IMAGE_PRICE_PER_UNIT 配置，"
        "未配置时成本记 0，不代表免费。"
    )
    lines.append("- 无硬门禁：任何指标不达标不影响退出码（环境性错误除外）。")
    return "\n".join(lines) + "\n"
