"""报告 → eval_runs 记录转换与落库（eval#4）：纯函数 + 真实 Postgres 写入。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.core.db import async_session_factory
from app.eval.cases import EvalCase
from app.eval.evaluator import CaseScore, JudgeVerdict, StructuralMetrics
from app.eval.hallucination import HallucinationReport
from app.eval.persistence import (
    build_category_scores,
    compute_cases_version,
    report_to_run,
    save_run,
)
from app.eval.report import build_report
from app.eval.runner import CaseArtifacts
from app.models.eval_run import EvalRun


def _artifacts(case_id: str, *, ok: bool = True) -> CaseArtifacts:
    slides = [
        {"id": f"s{i}", "status": "ready" if ok else "failed", "title": f"页{i}"} for i in range(8)
    ]
    return CaseArtifacts(
        case_id=case_id,
        ok=ok,
        stage="done" if ok else "deck_wait",
        error=None if ok else "RuntimeError: 超时",
        deck_response={"slides": slides} if ok else None,
        export_succeeded=ok,
        elapsed_seconds=60.0,
        total_slides=8,
        failed_slide_seen=0 if ok else 1,
        outline_trace_id="0e2d0f6e-0f9a-4b1e-9a64-7e5f1f0a1001" if ok else None,
        deck_trace_id="0e2d0f6e-0f9a-4b1e-9a64-7e5f1f0a1002" if ok else None,
    )


def _sample_report():
    """两题报告：documents 题（judge+幻觉）+ technology 失败题。"""
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
    return build_report(
        [
            (
                "documents/q3_sales",
                8,
                _artifacts("documents/q3_sales"),
                CaseScore(
                    structural=StructuralMetrics(
                        schema_valid=True, export_allowed=True, error_issue_count=0
                    ),
                    judge=judge,
                    hallucination=halluc,
                ),
            ),
            ("technology/rag", 8, _artifacts("technology/rag", ok=False), CaseScore()),
        ]
    )


class TestReportToRun:
    def test_maps_summary_fields_to_columns(self) -> None:
        run = report_to_run(_sample_report(), cases_version="test-1")
        # 列值与 report.RunSummary 指标一一对应
        assert run.total_cases == 2
        assert run.ok_cases == 1
        assert run.success_rate == 0.5
        assert run.failure_rate == 0.5
        assert run.schema_valid_cases == 1
        assert run.schema_scored_cases == 1
        assert run.schema_valid_rate == 1.0
        assert run.pages_met_cases == 1
        assert run.pages_scored_cases == 1
        assert run.pages_met_rate == 1.0
        assert run.export_succeeded_cases == 1
        assert run.avg_judge_coverage == 0.5
        assert run.avg_judge_score == 6.0
        assert run.judge_failed_cases == 0
        assert run.doc_total_numbers == 10
        assert run.doc_fabricated_numbers == 2
        assert run.hallucination_rate == 0.2
        assert run.avg_elapsed_seconds == 60.0
        assert run.total_failed_slides == 1
        assert run.total_slides == 16
        assert run.total_retried_slides == 0
        assert run.retry_slide_rate == 1 / 16
        assert run.status == "completed"

    def test_row_detail_jsonb_content(self) -> None:
        run = report_to_run(_sample_report(), cases_version="test-1")
        assert [item["case_id"] for item in run.rows] == [
            "documents/q3_sales",
            "technology/rag",
        ]
        doc_row = run.rows[0]
        assert doc_row["category"] == "documents"
        assert doc_row["ok"] is True
        assert doc_row["pages_met"] is True
        assert doc_row["schema_valid"] is True
        assert doc_row["judge_coverage"] == 0.5
        assert doc_row["judge_score"] == 6.0
        assert doc_row["hallucination_rate"] == 0.2
        # obs#4：token / 分段耗时 / trace 锚点进明细（join 前默认 0 + unavailable）
        assert doc_row["prompt_tokens"] == 0
        assert doc_row["tokens_source"] == "unavailable"
        assert doc_row["outline_trace_id"] == "0e2d0f6e-0f9a-4b1e-9a64-7e5f1f0a1001"
        assert doc_row["deck_trace_id"] == "0e2d0f6e-0f9a-4b1e-9a64-7e5f1f0a1002"
        assert doc_row["outline_duration_ms"] == 0
        assert doc_row["slide_durations_ms"] == []
        assert doc_row["export_duration_ms"] == 0
        failed_row = run.rows[1]
        assert failed_row["category"] == "technology"
        assert failed_row["ok"] is False
        assert failed_row["stage"] == "deck_wait"
        assert failed_row["error"] == "RuntimeError: 超时"
        assert failed_row["schema_valid"] is None
        assert failed_row["judge_coverage"] is None

    def test_category_scores_derived_from_rows(self) -> None:
        run = report_to_run(_sample_report(), cases_version="test-1")
        assert run.category_scores["documents"]["ok_cases"] == 1
        assert run.category_scores["documents"]["success_rate"] == 1.0
        assert run.category_scores["technology"]["ok_cases"] == 0
        assert run.category_scores["technology"]["success_rate"] == 0.0
        # 未出现的类别也占位，前端四列恒可渲染
        assert set(run.category_scores) == {"technology", "business", "education", "documents"}

    def test_category_scores_averages_skip_missing_judge(self) -> None:
        run = report_to_run(_sample_report(), cases_version="test-1")
        # technology 题失败无 judge：均分为 None 而非 0
        assert run.category_scores["technology"]["avg_judge_score"] is None
        assert run.category_scores["documents"]["avg_judge_score"] == 6.0

    def test_note_and_cases_version_kept(self) -> None:
        run = report_to_run(_sample_report(), cases_version="git:abc1234", note="回归前基线")
        assert run.cases_version == "git:abc1234"
        assert run.note == "回归前基线"


class TestBuildCategoryScores:
    def test_empty_rows_yield_all_placeholders(self) -> None:
        scores = build_category_scores([])
        assert set(scores) == {"technology", "business", "education", "documents"}
        assert scores["business"]["total_cases"] == 0
        assert scores["business"]["avg_judge_score"] is None


class TestComputeCasesVersion:
    def test_version_stable_and_sensitive_to_content(self) -> None:
        def case(pages: int) -> EvalCase:
            return EvalCase(
                id="technology/rag",
                input="生成一份《RAG》的 5 页 PPT",
                expected_pages=pages,
                requirements=["包含检索流程"],
                category="technology",
            )

        base = compute_cases_version([case(5)])
        assert base == compute_cases_version([case(5)])
        assert base != compute_cases_version([case(8)])
        assert base.startswith("cases-")


@pytest.mark.asyncio
async def test_save_run_persists_and_reads_back() -> None:
    """真实 Postgres 落库往返：save_run 写入后字段完整可读。"""
    run = report_to_run(_sample_report(), cases_version="test-1", note="入库测试")
    async with async_session_factory() as session:
        try:
            await save_run(session, run)
            stored = (
                await session.execute(select(EvalRun).where(EvalRun.id == run.id))
            ).scalar_one()
            assert stored.total_cases == 2
            assert stored.success_rate == 0.5
            assert stored.hallucination_rate == 0.2
            assert stored.note == "入库测试"
            assert stored.rows[0]["case_id"] == "documents/q3_sales"
            assert stored.category_scores["documents"]["ok_cases"] == 1
            assert stored.created_at is not None
            assert stored.created_at.tzinfo is not None
            assert abs(stored.created_at - datetime.now(UTC)) < timedelta(minutes=5)
        finally:
            await session.execute(delete(EvalRun).where(EvalRun.id == run.id))
            await session.commit()
