"""obs#7：SSE 进度事件携带 trace_id。

套路沿用 test_span_integration.py（真实 PG + FakeQueue + Fake 生成器），
把 deck/outline 的事件发布替换为捕获，断言 worker 各发布点带上的
contextvar trace_id 与 API 首帧锚点一致。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import get_queue
from app.core.db import async_session_factory
from app.main import app
from app.models.project import Project
from app.schemas.deck import DeckEvent
from app.schemas.outline import OutlineEvent
from app.services.deck import deck_events
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
        json={"email": f"obs7_{uuid.uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _pages(count: int) -> list[dict]:
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
        json={"title": "obs7 deck trace anchor", "page_count": 5},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "把内部工具沉淀为平台能力"},
        headers=headers,
    )
    async with async_session_factory() as session:
        result = await session.execute(
            select(Project)
            .options(selectinload(Project.outline))
            .where(Project.id == uuid.UUID(project["id"]))
        )
        record = result.scalar_one()
        session.add(
            ProjectOutline(
                project_id=record.id,
                status="confirmed",
                pages=_pages(5),
                revision=2,
            )
        )
        record.status = "outline_ready"
        await session.commit()
    return project


def _fake_flex_draft():
    from app.domain.flex_layout import FlexContainer, FlexLeaf
    from app.domain.slide_draft import (
        FlexBulletsContent,
        FlexKpiContent,
        FlexSlideDraft,
        FlexTextContent,
    )

    blocks = [
        FlexTextContent(id="title", text="本页核心结论与行动建议"),
        FlexBulletsContent(id="body", items=["要点一", "要点二", "要点三"]),
        FlexKpiContent(id="kpi_1", value="37%", label="增长"),
    ]
    tree = FlexContainer(
        type="column",
        id="root",
        children=[FlexLeaf(id=f"leaf-{b.id}", block_id=b.id, grow=1.0) for b in blocks],
    )
    return FlexSlideDraft(blocks=blocks, layout_tree=tree, speaker_notes="讲稿提示")


class _FakeSlideGenerator:
    async def generate(self, payload):
        return _fake_flex_draft()


class _FakeOutlineGenerator:
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


async def test_deck_events_carry_trace_id(
    client: AsyncClient, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """deck 全流程：API 首帧与 worker 每条事件都带同一个 trace_id。"""
    published: list[DeckEvent] = []

    async def capture(_key, event: DeckEvent) -> None:
        published.append(event)

    # deck_tasks 与 API 都引用同一个 EventStream 单例，替换其实例方法即可全收
    monkeypatch.setattr(deck_events, "publish", capture)

    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]

    await generate_deck(
        {"slide_generator": _FakeSlideGenerator()},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )

    trace_id = uuid.UUID(body["trace_id"])
    types = [event.type for event in published]
    # API 首帧 + 每页 started/completed + 终态 completed
    assert types[0] == "slide_started"
    assert "completed" in types
    assert len(published) >= 2 * 5 + 2
    for event in published:
        assert event.trace_id == trace_id


async def test_outline_events_carry_trace_id(
    client: AsyncClient, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """outline 全流程：API 首帧与 worker progress/completed 都带同一个 trace_id。"""
    published: list[OutlineEvent] = []

    async def capture(_key, event: OutlineEvent) -> None:
        published.append(event)

    monkeypatch.setattr("app.api.v1.outlines.publish_outline_event", capture)
    monkeypatch.setattr("app.worker.tasks.publish_outline_event", capture)

    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "obs7 outline trace anchor", "page_count": 5},
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
        {"outline_generator": _FakeOutlineGenerator(), "job_try": 1},
        project["id"],
        body["job_id"],
        body["trace_id"],
    )

    trace_id = uuid.UUID(body["trace_id"])
    types = [event.type for event in published]
    assert types[0] == "progress"
    assert types[-1] == "completed"
    assert "progress" in types[1:-1]
    for event in published:
        assert event.trace_id == trace_id


async def test_events_without_trace_id_stay_compatible() -> None:
    """旧事件缺 trace_id：字段缺省 None，老 payload 仍能校验通过（前端不崩）。"""
    legacy = DeckEvent.model_validate_json(
        '{"type":"snapshot","status":"generating","progress":0,'
        '"message":"等待生成任务","ready":0,"failed":0,"total":3}'
    )
    assert legacy.trace_id is None
    assert DeckEvent(type="snapshot", status="generating", progress=0, message="").trace_id is None
    legacy_outline = OutlineEvent.model_validate_json(
        '{"type":"progress","status":"generating","progress":10,"message":"正在整理输入材料"}'
    )
    assert legacy_outline.trace_id is None
    assert (
        OutlineEvent(type="progress", status="generating", progress=10, message="").trace_id is None
    )
