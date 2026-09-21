"""trace 只读 API（obs#3）：列表筛选分页 / 详情 / 指标聚合、认证、OpenAPI 暴露。

断言遵循两条纪律（issue 明确要求）：
1. 不用「uuid 全列表有序」当期望——uuid4 无时间序，库里有历史数据时必翻车；
   排序断言只考验 seeded 数据自身的相对位置。
2. 指标类断言只对「本用例独占的 project / 窗口」做绝对值断言，比例类断言
   用 seeded 子集可判定的方向性/存在性检查，避免与库内历史数据互相污染。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.main import app
from app.models.user import User
from app.observability.models import Span, Trace


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


@pytest.fixture
async def project_id() -> uuid.UUID:
    """独占 project：外键载体 + 指标断言的隔离域（级联清理）。"""
    from app.core.db import async_session_factory
    from app.models.project import Project

    async with async_session_factory() as session:
        user = User(email=f"trace_{uuid.uuid4().hex}@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        project = Project(
            user_id=user.id,
            title="trace API 测试",
            tone="professional",
            page_count=5,
            theme_id="default",
        )
        session.add(project)
        await session.commit()
        pid = project.id
        uid = user.id
    yield pid
    async with async_session_factory() as session:
        await session.execute(delete(User).where(User.id == uid))
        await session.commit()


@pytest.fixture
async def seeded(project_id: uuid.UUID) -> dict:
    """一个 outline trace + 一个 linked deck trace，带三层 spans。

    deck trace 复现 obs#2 语义：外层 slide[2] task span succeeded 收口，
    失败明细在节点级 slide.generate span（status=failed + error_code）。
    """
    from app.core.db import async_session_factory

    now = datetime.now(UTC)
    outline = Trace(
        kind="outline",
        status="succeeded",
        project_id=project_id,
        created_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=10),
        finished_at=now - timedelta(minutes=9, seconds=54),  # 6s
    )
    deck = Trace(
        kind="deck",
        status="failed",
        project_id=project_id,
        linked_trace_id=outline.id,  # 显式指定：outline 先入 session 拿到 id
        error_code="slide_failures",
        error_message="1 页生成失败",
        created_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=4, seconds=50),  # 10s
    )
    async with async_session_factory() as session:
        session.add(outline)
        await session.flush()
        deck.linked_trace_id = outline.id
        session.add(deck)
        await session.commit()

        outline_gen = Span(
            trace_id=outline.id,
            name="outline.generate",
            span_kind="node",
            status="succeeded",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=9, seconds=57),
            duration_ms=3000,
        )
        outline_llm = Span(
            trace_id=outline.id,
            parent_span_id=outline_gen.id,
            name="llm.call",
            span_kind="llm",
            status="succeeded",
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=9, seconds=58),
            duration_ms=2000,
            model="deepseek-v4-flash",
            purpose="outline",
            prompt_tokens=1000,
            completion_tokens=2000,
        )
        # 页 1：全链成功
        slide1 = Span(
            trace_id=deck.id,
            name="slide[1]",
            span_kind="task",
            status="succeeded",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=55),
            duration_ms=5000,
            attributes={"position": 1},
        )
        slide1_gen = Span(
            trace_id=deck.id,
            parent_span_id=slide1.id,
            name="slide.generate",
            span_kind="node",
            status="succeeded",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=57),
            duration_ms=3000,
        )
        slide1_llm = Span(
            trace_id=deck.id,
            parent_span_id=slide1_gen.id,
            name="llm.call",
            span_kind="llm",
            status="succeeded",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=58),
            duration_ms=2000,
            model="deepseek-v4-flash",
            purpose="slide",
            prompt_tokens=3000,
            completion_tokens=4000,
        )
        # 页 2：外层 task succeeded 收口，节点级 failed（obs#2 取舍）
        slide2 = Span(
            trace_id=deck.id,
            name="slide[2]",
            span_kind="task",
            status="succeeded",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=56),
            duration_ms=4000,
            attributes={"position": 2},
        )
        slide2_gen = Span(
            trace_id=deck.id,
            parent_span_id=slide2.id,
            name="slide.generate",
            span_kind="node",
            status="failed",
            error_code="llm_schema_error",
            error_message="结构化输出解析失败",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=58),
            duration_ms=2000,
        )
        slide2_repair = Span(
            trace_id=deck.id,
            parent_span_id=slide2.id,
            name="slide.repair",
            span_kind="node",
            status="succeeded",
            started_at=now - timedelta(minutes=5) + timedelta(seconds=2),
            finished_at=now - timedelta(minutes=4, seconds=57),
            duration_ms=1000,
            attributes={"repair_round": 1},
        )
        # 指标独占探针：名字与 error_code 全库唯一，支撑 metrics 的精确断言
        # （slide.* / llm.call 等真实名字在库内有历史数据，只能做方向性断言）
        probe_node = Span(
            trace_id=deck.id,
            name="obs3.probe_node",
            span_kind="node",
            status="succeeded",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=59),
            duration_ms=1000,
        )
        probe_llm = Span(
            trace_id=deck.id,
            name="obs3.probe_llm",
            span_kind="llm",
            status="succeeded",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=59, milliseconds=200),
            duration_ms=800,
            model="deepseek-v4-flash",
            purpose="probe",
            prompt_tokens=500,
            completion_tokens=700,
        )
        probe_failed = Span(
            trace_id=deck.id,
            name="obs3.probe_failed",
            span_kind="node",
            status="failed",
            error_code="obs3_probe_fail",
            error_message="探针失败",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4, seconds=59, milliseconds=400),
            duration_ms=300,
        )
    session.add_all([outline_gen, slide1, slide2])
    await session.flush()  # 先拿到 task span id，节点/llm span 挂 parent
    outline_llm.parent_span_id = outline_gen.id
    slide1_gen.parent_span_id = slide1.id
    slide1_llm.parent_span_id = slide1_gen.id
    slide2_gen.parent_span_id = slide2.id
    slide2_repair.parent_span_id = slide2.id
    session.add_all(
        [
            outline_llm,
            slide1_gen,
            slide1_llm,
            slide2_gen,
            slide2_repair,
            probe_node,
            probe_llm,
            probe_failed,
        ]
    )
    await session.commit()

    yield {"outline": outline, "deck": deck}

    async with async_session_factory() as session:
        await session.execute(delete(Trace).where(Trace.id.in_([outline.id, deck.id])))
        await session.commit()


async def test_traces_require_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/trace")).status_code == 401
    assert (await client.get(f"/api/v1/trace/{uuid.uuid4()}")).status_code == 401
    assert (await client.get("/api/v1/trace/metrics/summary")).status_code == 401


async def test_list_traces_newest_first_with_pagination(client: AsyncClient, seeded: dict) -> None:
    headers = await _sign_up(client)
    response = await client.get(
        "/api/v1/trace", headers=headers, params={"project_id": str(seeded["deck"].project_id)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    ids = [item["id"] for item in body["items"]]
    # 只断言 seeded 两条的相对顺序（created_at 降序），不断言全库顺序
    assert ids.index(str(seeded["deck"].id)) < ids.index(str(seeded["outline"].id))

    row = body["items"][0]
    assert row["kind"] == "deck"
    assert row["status"] == "failed"
    assert row["error_code"] == "slide_failures"
    assert row["linked_trace_id"] == str(seeded["outline"].id)
    assert row["duration_ms"] == 10000
    assert row["job_id"] is None
    assert "spans" not in row

    # 未收口 trace 的 duration_ms 为 null
    response = await client.get(
        "/api/v1/trace",
        headers=headers,
        params={"project_id": str(seeded["deck"].project_id), "limit": 1},
    )
    page1 = response.json()
    assert page1["total"] == 2
    assert len(page1["items"]) == 1
    response = await client.get(
        "/api/v1/trace",
        headers=headers,
        params={"project_id": str(seeded["deck"].project_id), "limit": 1, "offset": 1},
    )
    page2 = response.json()
    assert len(page2["items"]) == 1
    assert page1["items"][0]["id"] != page2["items"][0]["id"]


async def test_list_traces_filters(client: AsyncClient, seeded: dict) -> None:
    headers = await _sign_up(client)
    project = str(seeded["deck"].project_id)

    for params, expected_ids in [
        ({"kind": "outline", "project_id": project}, {str(seeded["outline"].id)}),
        ({"status": "failed", "project_id": project}, {str(seeded["deck"].id)}),
        (
            {"status": "succeeded", "kind": "deck", "project_id": project},
            set(),
        ),
    ]:
        response = await client.get("/api/v1/trace", headers=headers, params=params)
        assert response.status_code == 200
        assert {item["id"] for item in response.json()["items"]} == expected_ids
        assert response.json()["total"] == len(expected_ids)


async def test_list_traces_rejects_bad_pagination(client: AsyncClient) -> None:
    headers = await _sign_up(client)
    for params in [{"limit": 0}, {"limit": 101}, {"offset": -1}]:
        response = await client.get("/api/v1/trace", headers=headers, params=params)
        assert response.status_code == 422


async def test_get_trace_detail_with_span_tree(client: AsyncClient, seeded: dict) -> None:
    headers = await _sign_up(client)
    response = await client.get(f"/api/v1/trace/{seeded['deck'].id}", headers=headers)
    assert response.status_code == 200
    detail = response.json()

    trace = detail["trace"]
    assert trace["id"] == str(seeded["deck"].id)
    assert trace["kind"] == "deck"
    assert trace["status"] == "failed"
    assert trace["error_code"] == "slide_failures"
    assert trace["error_message"] == "1 页生成失败"
    assert trace["linked_trace_id"] == str(seeded["outline"].id)
    assert trace["duration_ms"] == 10000

    spans = detail["spans"]
    # deck 侧：页面链 6 条（2 task + 2 generate + 1 llm + 1 repair）+ 3 条指标探针
    assert len(spans) == 9
    by_name = {span["name"]: span for span in spans}
    slide2 = by_name["slide[2]"]
    # obs#2 语义：外层 task succeeded 收口，节点级才是 failed 明细
    assert slide2["status"] == "succeeded"
    assert slide2["attributes"] == {"position": 2}
    failed = by_name["slide.generate"]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "llm_schema_error"
    assert failed["error_message"] == "结构化输出解析失败"
    assert failed["parent_span_id"] == slide2["id"]
    repair = by_name["slide.repair"]
    assert repair["parent_span_id"] == slide2["id"]
    assert repair["attributes"] == {"repair_round": 1}
    llm = by_name["llm.call"]
    assert llm["span_kind"] == "llm"
    assert llm["model"] == "deepseek-v4-flash"
    assert llm["prompt_tokens"] == 3000
    assert llm["completion_tokens"] == 4000
    # 排序：按 started_at, id（slide[1] 先于 slide[2]）
    assert [span["name"] for span in spans].index("slide[1]") == 0

    # running trace 的 duration_ms 为 null（outline 不在此例，另造一条验证空值路径）
    from app.core.db import async_session_factory

    running = Trace(kind="outline", status="running", project_id=seeded["deck"].project_id)
    async with async_session_factory() as session:
        session.add(running)
        await session.commit()
        rid = running.id
    try:
        response = await client.get(f"/api/v1/trace/{rid}", headers=headers)
        assert response.status_code == 200
        assert response.json()["trace"]["duration_ms"] is None
        assert response.json()["spans"] == []
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(Trace).where(Trace.id == rid))
            await session.commit()


async def test_get_trace_404_for_unknown_id(client: AsyncClient) -> None:
    headers = await _sign_up(client)
    response = await client.get(f"/api/v1/trace/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404


async def test_metrics_summary(client: AsyncClient, seeded: dict) -> None:
    headers = await _sign_up(client)
    response = await client.get(
        "/api/v1/trace/metrics/summary", headers=headers, params={"days": 7}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    assert body["deck_duration_p50_ms"] is not None
    assert body["deck_duration_p95_ms"] is not None
    assert body["deck_duration_p50_ms"] <= body["deck_duration_p95_ms"]

    # trace 成功率按 kind 分组，seeded 两种 kind 都在窗口内
    kinds = {item["kind"]: item for item in body["trace_success"]}
    assert {"outline", "deck"} <= set(kinds)
    for item in body["trace_success"]:
        assert item["total"] >= item["succeeded"]
        assert 0.0 <= item["success_rate"] <= 1.0

    # slide 成功率：slide[N] task span 口径不受节点级失败影响（seeded 两条均 succeeded）
    assert body["slide_succeeded"] >= 2
    assert body["slide_total"] >= body["slide_succeeded"]
    assert body["slide_success_rate"] is not None

    # token 均值/总和：探针 llm span 提供精确锚点（500/700）
    assert body["avg_prompt_tokens"] is not None
    assert body["avg_completion_tokens"] is not None
    assert body["total_prompt_tokens"] >= 4500
    assert body["total_completion_tokens"] >= 6700

    # failure_breakdown：独占探针 error_code 精确断言；ratio 在 (0,1] 且分母为失败总数
    breakdown = {item["error_code"]: item for item in body["failure_breakdown"]}
    assert breakdown["obs3_probe_fail"]["count"] == 1
    failed_counts = [item["count"] for item in body["failure_breakdown"]]
    assert sum(failed_counts) >= 2  # 至少含探针 + slide.generate 的历史失败
    for item in body["failure_breakdown"]:
        assert 0.0 < item["ratio"] <= 1.0

    # node_stats：独占探针 name 的精确断言（真实 name 只做存在性/方向性断言）
    stats = {item["name"]: item for item in body["node_stats"]}
    assert stats["obs3.probe_node"]["total"] == 1
    assert stats["obs3.probe_node"]["failed"] == 0
    assert stats["obs3.probe_node"]["failure_rate"] == 0.0
    assert stats["obs3.probe_node"]["avg_ms"] == 1000.0
    assert stats["obs3.probe_node"]["avg_completion_tokens"] is None  # 非 llm span
    assert stats["obs3.probe_failed"]["failed"] == 1
    assert stats["obs3.probe_failed"]["failure_rate"] == 1.0
    # llm 探针：token 均值只算 llm span（case 过滤非 llm 的 null）
    assert stats["obs3.probe_llm"]["avg_completion_tokens"] == 700.0
    assert stats["obs3.probe_llm"]["avg_ms"] == 800.0
    assert stats["slide.generate"]["failed"] >= 1
    assert stats["slide.generate"]["avg_completion_tokens"] is None


async def test_metrics_summary_excludes_old_traces(
    client: AsyncClient, seeded: dict, project_id: uuid.UUID
) -> None:
    """窗口外（30 天前）的 trace 及其 span 不进指标。"""
    from app.core.db import async_session_factory

    old = datetime.now(UTC) - timedelta(days=30)
    old_trace = Trace(
        kind="deck",
        status="failed",
        project_id=project_id,
        created_at=old,
        started_at=old,
        finished_at=old + timedelta(seconds=99),
    )
    async with async_session_factory() as session:
        session.add(old_trace)
        await session.flush()
        session.add(
            Span(
                trace_id=old_trace.id,
                name="slide[99]",
                span_kind="task",
                status="failed",
                error_code="llm_error",
                started_at=old,
                duration_ms=1000,
            )
        )
        await session.commit()
        old_id = old_trace.id
    try:
        headers = await _sign_up(client)
        response = await client.get(
            "/api/v1/trace/metrics/summary", headers=headers, params={"days": 7}
        )
        assert response.status_code == 200
        body = response.json()
        # 旧 trace（99s）没有进窗口：分位数仍小于 99000ms
        assert body["deck_duration_p50_ms"] is None or body["deck_duration_p50_ms"] < 99000
        assert body["deck_duration_p95_ms"] is None or body["deck_duration_p95_ms"] < 99000
        stats = {item["name"]: item for item in body["node_stats"]}
        assert "slide[99]" not in stats
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(Trace).where(Trace.id == old_id))
            await session.commit()


async def test_metrics_summary_rejects_bad_days(client: AsyncClient) -> None:
    headers = await _sign_up(client)
    for days in [0, 91]:
        response = await client.get(
            "/api/v1/trace/metrics/summary", headers=headers, params={"days": days}
        )
        assert response.status_code == 422


async def test_openapi_exposes_trace_schemas() -> None:
    """前端类型生成依赖 schema 正常暴露。"""
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/trace" in paths
    assert "/api/v1/trace/{trace_id}" in paths
    assert "/api/v1/trace/metrics/summary" in paths
    for name in [
        "TracePublic",
        "TracePage",
        "TraceDetail",
        "SpanPublic",
        "TraceMetricsSummary",
        "FailureBreakdownItem",
        "NodeStatItem",
    ]:
        assert name in schema["components"]["schemas"]
