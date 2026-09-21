"""评测 run 只读 API（eval#4）：列表 / 详情、认证、OpenAPI 暴露。"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.main import app
from app.models.eval_run import EvalRun


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _sign_up(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"user_{uuid.uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _run_row(**overrides) -> dict:
    base = {
        "cases_version": "test-1",
        "total_cases": 2,
        "ok_cases": 1,
        "success_rate": 0.5,
        "failure_rate": 0.5,
        "schema_valid_cases": 1,
        "schema_scored_cases": 1,
        "schema_valid_rate": 1.0,
        "pages_met_cases": 1,
        "pages_scored_cases": 1,
        "pages_met_rate": 1.0,
        "export_succeeded_cases": 1,
        "avg_judge_coverage": 0.5,
        "avg_judge_score": 6.0,
        "judge_failed_cases": 0,
        "doc_total_numbers": 10,
        "doc_fabricated_numbers": 2,
        "hallucination_rate": 0.2,
        "avg_elapsed_seconds": 60.0,
        "avg_prompt_tokens": 0.0,
        "avg_completion_tokens": 0.0,
        "total_failed_slides": 0,
        "total_slides": 16,
        "retry_slide_rate": None,
        "total_retried_slides": 0,
        "status": "completed",
        "note": "",
        "rows": [
            {
                "case_id": "documents/q3_sales",
                "category": "documents",
                "ok": True,
                "stage": "done",
                "error": None,
                "ready_pages": 8,
                "expected_pages": 8,
                "pages_met": True,
                "schema_valid": True,
                "judge_coverage": 0.5,
                "judge_score": 6.0,
                "hallucination_rate": 0.2,
                "failed_slide_seen": 0,
                "total_slides": 8,
                "retried_slides": 0,
                "elapsed_seconds": 60.0,
            },
            {
                "case_id": "technology/rag",
                "category": "technology",
                "ok": False,
                "stage": "deck_wait",
                "error": "RuntimeError: 超时",
                "ready_pages": 0,
                "expected_pages": 8,
                "pages_met": False,
                "schema_valid": None,
                "judge_coverage": None,
                "judge_score": None,
                "hallucination_rate": None,
                "failed_slide_seen": 1,
                "total_slides": 8,
                "retried_slides": 0,
                "elapsed_seconds": 60.0,
            },
        ],
        "category_scores": {
            "technology": {"total_cases": 1, "ok_cases": 0, "avg_judge_score": None},
            "business": {"total_cases": 0, "ok_cases": 0, "avg_judge_score": None},
            "education": {"total_cases": 0, "ok_cases": 0, "avg_judge_score": None},
            "documents": {"total_cases": 1, "ok_cases": 1, "avg_judge_score": 6.0},
        },
    }
    return base | overrides


@pytest.fixture
async def seeded_runs():
    """插入两条 run 记录并在用例结束后清理（复用生产 session factory）。"""
    from datetime import UTC, datetime, timedelta

    from app.core.db import async_session_factory

    older = EvalRun(**_run_row(cases_version="v1", note="较早"))
    newer = EvalRun(**_run_row(cases_version="v2", note="较新"))
    # created_at 默认 now()：同事务插入会同时间戳，而 uuid4 的 id 无时间序，
    # 与库内既有 run 同秒时排序断言会翻车。显式错开时间戳，让断言只考验排序。
    older.created_at = datetime.now(UTC) - timedelta(minutes=10)
    newer.created_at = datetime.now(UTC) - timedelta(minutes=5)
    async with async_session_factory() as session:
        session.add_all([older, newer])
        await session.commit()
    yield [older, newer]
    async with async_session_factory() as session:
        await session.execute(delete(EvalRun).where(EvalRun.id.in_([older.id, newer.id])))
        await session.commit()


async def test_runs_require_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/eval/runs")
    assert response.status_code == 401


async def test_list_runs_newest_first(client: AsyncClient, seeded_runs) -> None:
    headers = await _sign_up(client)
    response = await client.get("/api/v1/eval/runs", headers=headers)
    assert response.status_code == 200
    body = response.json()
    ids = [item["id"] for item in body]
    assert {str(run.id) for run in seeded_runs} <= set(ids)
    # created_at 降序 → 时间较新的 seeded run 必须排在较旧的前面
    # （不能用 uuid 字符串排序当期望：uuid4 无时间序，库里有历史 run 时必翻车）
    assert ids.index(str(seeded_runs[1].id)) < ids.index(str(seeded_runs[0].id))
    row = next(item for item in body if item["id"] == str(seeded_runs[1].id))
    # 列表行字段：列表页不解析明细 JSONB
    assert row["cases_version"] == "v2"
    assert row["total_cases"] == 2
    assert row["success_rate"] == 0.5
    assert row["schema_valid_rate"] == 1.0
    assert row["avg_judge_coverage"] == 0.5
    assert row["hallucination_rate"] == 0.2
    assert row["avg_elapsed_seconds"] == 60.0
    assert row["failure_rate"] == 0.5
    assert row["retry_slide_rate"] is None
    assert row["status"] == "completed"
    assert row["note"] == "较新"
    assert "rows" not in row
    assert "category_scores" not in row


async def test_list_runs_limit_defaults_to_20(client: AsyncClient, seeded_runs) -> None:
    headers = await _sign_up(client)
    response = await client.get("/api/v1/eval/runs", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) <= 20


async def test_get_run_detail(client: AsyncClient, seeded_runs) -> None:
    headers = await _sign_up(client)
    target = seeded_runs[0]
    response = await client.get(f"/api/v1/eval/runs/{target.id}", headers=headers)
    assert response.status_code == 200
    detail = response.json()
    assert detail["id"] == str(target.id)
    assert detail["cases_version"] == "v1"
    assert len(detail["rows"]) == 2
    doc_row = detail["rows"][0]
    assert doc_row["case_id"] == "documents/q3_sales"
    assert doc_row["category"] == "documents"
    assert doc_row["pages_met"] is True
    failed_row = detail["rows"][1]
    assert failed_row["stage"] == "deck_wait"
    assert failed_row["error"] == "RuntimeError: 超时"
    scores = detail["category_scores"]
    assert scores["documents"]["avg_judge_score"] == 6.0
    assert scores["technology"]["avg_judge_score"] is None


async def test_get_run_404_for_unknown_id(client: AsyncClient) -> None:
    headers = await _sign_up(client)
    response = await client.get(f"/api/v1/eval/runs/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404


async def test_openapi_exposes_eval_run_schemas() -> None:
    """前端类型生成（eval#5）依赖 schema 正常暴露。"""
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/eval/runs" in paths
    assert "/api/v1/eval/runs/{run_id}" in paths
    assert "EvalRunPublic" in schema["components"]["schemas"]
    assert "EvalRunDetail" in schema["components"]["schemas"]
    assert "EvalCaseRow" in schema["components"]["schemas"]
