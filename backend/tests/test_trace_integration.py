"""obs#1 集成测试：API 建 trace、worker 终态收口、deck 断链修复。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import get_queue
from app.core.db import async_session_factory
from app.llm.errors import InvalidSlideOutputError
from app.main import app
from app.models.project import Project
from app.observability.models import Trace
from app.worker.deck_tasks import generate_deck
from app.worker.tasks import generate_outline


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def enqueue_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return object()


@pytest.fixture
async def queue(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[FakeQueue, None]:
    fake = FakeQueue()

    async def ignore(*_args, **_kwargs) -> None:
        pass

    monkeypatch.setattr("app.api.v1.outlines.publish_outline_event", ignore)
    monkeypatch.setattr("app.worker.tasks.publish_outline_event", ignore)
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _sign_up(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"obsint_{uuid.uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _traces(project_id: uuid.UUID) -> list[Trace]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Trace).where(Trace.project_id == project_id).order_by(Trace.created_at)
        )
        return list(result.scalars())


async def _project_row(project_id: uuid.UUID) -> Project:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Project).options(selectinload(Project.outline)).where(Project.id == project_id)
        )
        return result.scalar_one()


class FakeOutlineGenerator:
    async def generate(self, payload):
        from app.domain.outline import OutlineDraft, OutlinePageDraft

        return OutlineDraft(
            pages=[
                OutlinePageDraft(
                    title=f"第 {index} 页",
                    objective="说明本页的核心目标",
                    key_points=["要点一", "要点二"],
                    source_refs=["S1:1"],
                    layout_id="cover" if index == 1 else "bullets",
                )
                for index in range(1, payload.page_count + 1)
            ]
        )


class BrokenOutlineGenerator:
    async def generate(self, payload):
        from app.llm.errors import InvalidOutlineOutputError

        raise InvalidOutlineOutputError("结构不合法")


async def test_outline_api_creates_running_trace(client: AsyncClient, queue: FakeQueue) -> None:
    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "trace 测试", "page_count": 5},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "把内部工具沉淀为平台能力"},
        headers=headers,
    )

    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/outline/generate", headers=headers
    )
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["trace_id"]

    rows = await _traces(uuid.UUID(project["id"]))
    assert len(rows) == 1
    assert str(rows[0].id) == body["trace_id"]
    assert rows[0].kind == "outline"
    assert rows[0].status == "running"
    assert rows[0].job_id == body["job_id"]
    # #37：单次入队即携带 trace_id（旧的「先入队再二次补投」在真实 arq 上
    # 被 _job_id 去重吞掉，trace_id 永远到不了 worker）
    assert len(queue.calls) == 1
    args, kwargs = queue.calls[0]
    assert args[0] == "generate_outline"
    assert args[1] == project["id"]
    assert args[2] == body["job_id"]
    assert args[3] == body["trace_id"]
    assert kwargs["_job_id"] == body["job_id"]


async def test_outline_enqueue_failure_closes_trace(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入队失败（Redis 挂）：outline 落 failed，trace 显式收口不留孤儿 running（#37）。"""
    from redis.exceptions import RedisError

    class BrokenQueue:
        async def enqueue_job(self, *args, **kwargs):
            raise RedisError("queue down")

    async def ignore(*_args, **_kwargs) -> None:
        pass

    monkeypatch.setattr("app.api.v1.outlines.publish_outline_event", ignore)
    app.dependency_overrides[get_queue] = lambda: BrokenQueue()
    try:
        headers = await _sign_up(client)
        project = (
            await client.post(
                "/api/v1/projects",
                json={"title": "enqueue 失败", "page_count": 5},
                headers=headers,
            )
        ).json()
        await client.post(
            f"/api/v1/projects/{project['id']}/sources",
            json={"kind": "topic", "content": "把内部工具沉淀为平台能力"},
            headers=headers,
        )
        response = await client.post(
            f"/api/v1/projects/{project['id']}/outline/generate", headers=headers
        )
        assert response.status_code == 503

        rows = await _traces(uuid.UUID(project["id"]))
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].error_code == "enqueue_failed"

        record = await _project_row(uuid.UUID(project["id"]))
        assert record.outline is not None
        assert record.outline.status == "failed"
        assert record.outline.error == "任务队列暂时不可用"
    finally:
        app.dependency_overrides.pop(get_queue, None)


async def test_outline_worker_finishes_trace_succeeded(
    client: AsyncClient, queue: FakeQueue
) -> None:
    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "trace 成功路径", "page_count": 5},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "把内部工具沉淀为平台能力"},
        headers=headers,
    )
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/outline/generate", headers=headers
    )
    body = accepted.json()

    await generate_outline(
        {"outline_generator": FakeOutlineGenerator(), "job_try": 1},
        project["id"],
        body["job_id"],
        body["trace_id"],
    )

    rows = await _traces(uuid.UUID(project["id"]))
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].finished_at is not None


async def test_outline_worker_finishes_trace_failed(client: AsyncClient, queue: FakeQueue) -> None:
    from app.worker.retry import MAX_TRIES

    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "trace 失败路径", "page_count": 5},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "把内部工具沉淀为平台能力"},
        headers=headers,
    )
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/outline/generate", headers=headers
    )
    body = accepted.json()

    await generate_outline(
        {"outline_generator": BrokenOutlineGenerator(), "job_try": MAX_TRIES},
        project["id"],
        body["job_id"],
        body["trace_id"],
    )

    rows = await _traces(uuid.UUID(project["id"]))
    assert rows[0].status == "failed"
    # message 走 _public_error 口径，不落敏感原文
    assert rows[0].error_message == "模型生成大纲失败，请稍后重试"
    assert rows[0].error_code == "llm_error"


async def test_outline_worker_retry_keeps_trace_running(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """job_try 未到上限抛 Retry：trace 继续跑，不收口。"""
    from arq import Retry

    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "trace 重试路径", "page_count": 5},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "把内部工具沉淀为平台能力"},
        headers=headers,
    )
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/outline/generate", headers=headers
    )
    body = accepted.json()

    with pytest.raises(Retry):
        await generate_outline(
            {"outline_generator": BrokenOutlineGenerator(), "job_try": 1},
            project["id"],
            body["job_id"],
            body["trace_id"],
        )

    rows = await _traces(uuid.UUID(project["id"]))
    assert rows[0].status == "running"
    assert rows[0].finished_at is None


def _deck_pages(count: int = 5) -> list[dict]:
    return [
        {
            "id": str(uuid.uuid4()),
            "title": f"第 {index} 页",
            "objective": "说明本页的核心目标",
            "key_points": ["要点一", "要点二"],
            "source_refs": ["S1:1"],
            "layout_id": "cover" if index == 1 else "bullets",
            "page_role": "cover" if index == 1 else "content",
        }
        for index in range(1, count + 1)
    ]


async def _confirmed_project(client: AsyncClient, headers: dict[str, str]) -> dict:
    from app.models.project import ProjectOutline

    response = await client.post(
        "/api/v1/projects",
        json={"title": "deck trace 测试", "page_count": 5},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "把内部工具沉淀为平台能力"},
        headers=headers,
    )
    async with async_session_factory() as session:
        record = await session.get(Project, uuid.UUID(project["id"]))
        assert record is not None
        session.add(
            ProjectOutline(
                project_id=record.id,
                status="confirmed",
                pages=_deck_pages(),
                revision=2,
            )
        )
        record.status = "outline_ready"
        await session.commit()
    return project


class FakeSlideGenerator:
    def __init__(self, *, fail_positions: set[int] | None = None) -> None:
        self.fail_positions = fail_positions or set()

    async def generate(self, payload):
        from app.domain.flex_layout import FlexContainer, FlexLeaf
        from app.domain.slide_draft import (
            FlexBulletsContent,
            FlexImageContent,
            FlexKpiContent,
            FlexSlideDraft,
            FlexTextContent,
        )

        if payload.position in self.fail_positions:
            raise InvalidSlideOutputError("模拟生成失败")
        blocks = [
            FlexTextContent(id="title", text="本页核心结论与行动建议"),
            FlexBulletsContent(id="body", items=["要点一", "要点二", "要点三"]),
            FlexKpiContent(id="kpi_1", value="37%", label="增长"),
            FlexImageContent(id="visual", alt="示意图"),
        ]
        tree = FlexContainer(
            type="column",
            id="root",
            children=[FlexLeaf(id=f"leaf-{b.id}", block_id=b.id, grow=1.0) for b in blocks],
        )
        return FlexSlideDraft(blocks=blocks, layout_tree=tree, speaker_notes="讲稿提示")


async def test_deck_api_records_trace_and_fixes_job_link(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """obs#1 核心：deck job_id 随 trace 落库并传参给 worker。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)

    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["trace_id"]

    rows = await _traces(uuid.UUID(project["id"]))
    assert len(rows) == 1
    trace = rows[0]
    assert trace.kind == "deck"
    assert trace.status == "running"
    assert trace.job_id == body["job_id"], "job_id 必须随 trace 落库（断链修复）"
    assert trace.linked_trace_id is None  # 本项目没跑过 outline trace

    # enqueue 参数：project_id, slide_ids, trace_id, job_id
    args, kwargs = queue.calls[0]
    assert args[0] == "generate_deck"
    assert args[3] == body["trace_id"]
    assert args[4] == body["job_id"]
    assert kwargs["_job_id"] == body["job_id"]
    # deck 侧只 enqueue 一次（trace 在 enqueue 前建好）
    assert len(queue.calls) == 1

    row = await _project_row(uuid.UUID(project["id"]))
    assert row.last_deck_trace_id == trace.id


async def test_deck_trace_links_to_outline_trace(client: AsyncClient, queue: FakeQueue) -> None:
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    # 先造一条本项目的历史 outline trace
    async with async_session_factory() as session:
        outline_trace = Trace(
            kind="outline",
            project_id=uuid.UUID(project["id"]),
            status="succeeded",
        )
        session.add(outline_trace)
        await session.commit()
        outline_trace_id = outline_trace.id

    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    assert accepted.status_code == 202
    rows = await _traces(uuid.UUID(project["id"]))
    deck_trace = next(t for t in rows if t.kind == "deck")
    assert deck_trace.linked_trace_id == outline_trace_id


async def test_deck_worker_trace_outcomes(client: AsyncClient, queue: FakeQueue) -> None:
    headers = await _sign_up(client)
    # 成功路径
    project = await _confirmed_project(client, headers)
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]
    await generate_deck(
        {"slide_generator": FakeSlideGenerator()},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )
    rows = await _traces(uuid.UUID(project["id"]))
    deck_trace = next(t for t in rows if t.kind == "deck")
    assert deck_trace.status == "succeeded"
    assert deck_trace.finished_at is not None

    # 失败路径：一页失败 → failed + slide_failures
    headers2 = await _sign_up(client)
    project2 = await _confirmed_project(client, headers2)
    accepted2 = await client.post(
        f"/api/v1/projects/{project2['id']}/deck/generate", json={}, headers=headers2
    )
    body2 = accepted2.json()
    slide_ids2 = queue.calls[-1][0][2]
    await generate_deck(
        {"slide_generator": FakeSlideGenerator(fail_positions={2})},
        project2["id"],
        slide_ids2,
        body2["trace_id"],
        body2["job_id"],
    )
    rows2 = await _traces(uuid.UUID(project2["id"]))
    failed_trace = next(t for t in rows2 if t.kind == "deck")
    assert failed_trace.status == "failed"
    assert failed_trace.error_code == "slide_failures"
    assert failed_trace.error_message == "1 页失败"


async def test_deck_worker_without_trace_still_works(client: AsyncClient, queue: FakeQueue) -> None:
    """不传 trace_id（旧调用/直测）时任务照常完成，只是不记录。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    await client.post(f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers)
    slide_ids = queue.calls[0][0][2]

    await generate_deck({"slide_generator": FakeSlideGenerator()}, project["id"], slide_ids)

    deck = await client.get(f"/api/v1/projects/{project['id']}/deck", headers=headers)
    assert deck.json()["status"] == "ready"
