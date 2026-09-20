"""指标聚合与报告组装（eval#3）：build_row / aggregate / format_report 纯逻辑。"""

from __future__ import annotations

from app.eval.evaluator import CaseScore, JudgeVerdict, StructuralMetrics
from app.eval.hallucination import HallucinationReport
from app.eval.report import aggregate, build_report, build_row, format_report
from app.eval.runner import CaseArtifacts


def _artifacts(
    case_id: str,
    *,
    ok: bool = True,
    stage: str = "done",
    error: str | None = None,
    ready: int = 8,
    export: bool = True,
    failed_seen: int = 0,
    retried: int = 0,
    elapsed: float = 60.0,
    partial: bool = False,
    partial_ready: int = 5,
) -> CaseArtifacts:
    total = 8
    effective_ready = ready if ok else (partial_ready if partial else 0)
    slides = [
        {"id": f"s{i}", "status": "ready" if i < effective_ready else "failed", "title": f"页{i}"}
        for i in range(total)
    ]
    return CaseArtifacts(
        case_id=case_id,
        ok=ok,
        stage=stage,
        error=error,
        deck_response={"slides": slides} if (ok or partial) else None,
        export_succeeded=export,
        elapsed_seconds=elapsed,
        failed_slide_seen=failed_seen,
        total_slides=len(slides) if (ok or partial) else 0,
        retried_slides=retried,
    )


def _score(
    *,
    structural: StructuralMetrics | None = None,
    judge: JudgeVerdict | None = None,
    judge_error: str | None = None,
    hallucination: HallucinationReport | None = None,
) -> CaseScore:
    return CaseScore(
        structural=structural,
        judge=judge,
        judge_error=judge_error,
        hallucination=hallucination,
    )


_OK_STRUCT = StructuralMetrics(
    schema_valid=True, export_allowed=True, error_issue_count=0, warning_issue_count=1
)


class TestBuildRow:
    def test_ok_case_maps_all_fields(self) -> None:
        judge = JudgeVerdict.model_validate(
            {
                "requirements": [
                    {"text": "r1", "pass": True, "reason": ""},
                    {"text": "r2", "pass": False, "reason": ""},
                ],
                "content_score": 6.0,
            }
        )
        halluc = HallucinationReport(total_numbers=10, fabricated_numbers=2, findings=[])
        artifacts = _artifacts("documents/q3_sales", failed_seen=1, retried=1)
        row = build_row(
            "documents/q3_sales",
            8,
            artifacts,
            _score(structural=_OK_STRUCT, judge=judge, hallucination=halluc),
        )
        assert row.ok is True
        assert row.ready_pages == 8
        assert row.pages_met is True
        assert row.schema_valid is True
        assert row.judge_coverage == 0.5
        assert row.judge_score == 6.0
        assert row.hallucination_rate == 0.2
        assert row.failed_slide_seen == 1
        assert row.retried_slides == 1

    def test_failed_case_keeps_stage_and_error(self) -> None:
        artifacts = _artifacts(
            "technology/rag", ok=False, stage="outline_wait", error="TimeoutError: 大纲生成超时"
        )
        row = build_row("technology/rag", 8, artifacts, _score())
        assert row.ok is False
        assert row.stage == "outline_wait"
        assert row.error == "TimeoutError: 大纲生成超时"
        assert row.schema_valid is None
        assert row.judge_coverage is None
        assert row.hallucination_rate is None

    def test_pages_not_met_when_ready_differs(self) -> None:
        artifacts = _artifacts("education/ml_intro", ready=6)
        row = build_row("education/ml_intro", 8, artifacts, _score(structural=_OK_STRUCT))
        assert row.pages_met is False


class TestAggregate:
    def test_aggregates_run_level_metrics(self) -> None:
        rows = [
            build_row(
                "documents/q3_sales",
                8,
                _artifacts("documents/q3_sales", failed_seen=1),
                _score(
                    structural=_OK_STRUCT,
                    judge=JudgeVerdict.model_validate(
                        {
                            "requirements": [{"text": "r", "pass": True, "reason": ""}],
                            "content_score": 8.0,
                        }
                    ),
                    hallucination=HallucinationReport(
                        total_numbers=10, fabricated_numbers=1, findings=[]
                    ),
                ),
            ),
            build_row(
                "technology/rag",
                8,
                _artifacts(
                    "technology/rag",
                    ok=False,
                    stage="deck_wait",
                    error="RuntimeError: 2 页重试后仍失败",
                    partial=True,  # 超时失败但拿到过部分页：参与页数达成分母
                ),
                _score(),
            ),
        ]
        summary = aggregate(rows)

        assert summary.total_cases == 2
        assert summary.ok_cases == 1
        assert summary.success_rate == 0.5
        assert summary.failure_rate == 0.5
        assert summary.schema_valid_rate == 1.0
        assert summary.schema_scored_cases == 1
        assert summary.pages_met_cases == 1
        assert summary.pages_met_rate == 0.5
        assert summary.avg_judge_coverage == 1.0
        assert summary.avg_judge_score == 8.0
        assert summary.hallucination_rate == 1 / 10
        assert summary.doc_total_numbers == 10
        assert summary.doc_fabricated_numbers == 1
        assert summary.total_failed_slides == 1
        assert summary.total_slides == 16
        assert summary.retry_slide_rate == 1 / 16
        assert summary.total_retried_slides == 0

    def test_empty_rows_yield_neutral_summary(self) -> None:
        summary = aggregate([])
        assert summary.total_cases == 0
        assert summary.success_rate == 0.0
        assert summary.hallucination_rate is None
        assert summary.retry_slide_rate is None

    def test_hallucination_rate_zero_when_no_numbers_extracted(self) -> None:
        rows = [
            build_row(
                "documents/ev_market",
                8,
                _artifacts("documents/ev_market"),
                _score(hallucination=HallucinationReport(total_numbers=0, fabricated_numbers=0)),
            )
        ]
        summary = aggregate(rows)
        assert summary.hallucination_rate == 0.0

    def test_judge_failure_counted_separately(self) -> None:
        rows = [
            build_row(
                "business/saas_model",
                8,
                _artifacts("business/saas_model"),
                _score(structural=_OK_STRUCT, judge_error="InvalidModelOutputError: bad"),
            )
        ]
        summary = aggregate(rows)
        assert summary.judge_failed_cases == 1
        assert summary.avg_judge_coverage is None


class TestFormatReport:
    def test_report_contains_summary_and_rows(self) -> None:
        report = build_report(
            [
                (
                    "documents/q3_sales",
                    8,
                    _artifacts("documents/q3_sales", failed_seen=1, retried=1),
                    _score(
                        structural=_OK_STRUCT,
                        judge=JudgeVerdict.model_validate(
                            {
                                "requirements": [{"text": "r", "pass": True, "reason": ""}],
                                "content_score": 8.0,
                            }
                        ),
                        hallucination=HallucinationReport(
                            total_numbers=4, fabricated_numbers=1, findings=[]
                        ),
                    ),
                ),
                (
                    "technology/rag",
                    8,
                    _artifacts(
                        "technology/rag",
                        ok=False,
                        stage="deck_wait",
                        error="RuntimeError: 超时",
                        partial=True,
                    ),
                    _score(),
                ),
            ]
        )
        text = format_report(report)

        assert "=== SlideForge 评测报告 ===" in text
        assert "成功：1（50.0%）" in text
        assert "Schema 合法率：1/1（100.0%）" in text
        assert "页数达成率：1/2（50.0%）" in text
        assert "幻觉率（文档题）：1/4（25.0%）" in text
        assert "documents/q3_sales" in text
        assert "technology/rag" in text
        assert "RuntimeError: 超时" in text
        # 逐题明细表头与口径注记
        assert "题号" in text
        assert "口径注记" in text
        assert "重试率" in text

    def test_report_token_note_documents_zero_token_scope(self) -> None:
        report = build_report(
            [("education/ml_intro", 8, _artifacts("education/ml_intro"), _score())]
        )
        text = format_report(report)
        assert "worker 进程埋点不可达" in text
