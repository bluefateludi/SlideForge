"""#33 / ADR-0001：stuck generating 惰性对账的验收测试。

构造「worker 死后遗留的孤儿状态」（generating + started_at 回拨），
断言 GET deck / retry / regenerate / cancel / outline 五条路径全部放行，
且未超时的活任务仍受 409 保护、ready 页原样保留。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import get_queue
from app.core.db import async_session_factory
from app.llm.errors import InvalidSlideOutputError, LLMNotConfiguredError
from app.main import app
from app.models.project import Project, ProjectOutline
from app.models.slide import Slide
from app.services.deck import (
    SLIDE_ERROR_LLM_NOT_CONFIGURED,
    SLIDE_ERROR_LLM_OUTPUT_INVALID,
    SLIDE_ERROR_LLM_TIMEOUT,
    SLIDE_ERROR_WORKER_DEAD,
    classify_generation_error,
)
from app.worker.deck_tasks import _mark_generating


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
        json={"email": f"stuck_{uuid.uuid4().hex}@example.com", "password": "password123"},
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


async def _confirmed_project(client: AsyncClient, headers: dict[str, str], pages: int = 5) -> dict:
    response = await client.post(
        "/api/v1/projects",
        json={"title": "对账测试", "page_count": pages, "layout_mode": "fixed"},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "验证崩溃恢复的惰性对账"},
        headers=headers,
    )
    async with async_session_factory() as session:
        result = await session.execute(
            select(Project).options(selectinload(Project.outline)).where(
                Project.id == uuid.UUID(project["id"])
            )
        )
        record = result.scalar_one()
        session.add(
            ProjectOutline(
                project_id=record.id,
                status="confirmed",
                pages=_pages(pages),
                revision=2,
            )
        )
        record.status = "outline_ready"
        await session.commit()
    return project


async def _start_generation(client: AsyncClient, headers: dict[str, str], project_id: str) -> None:
    response = await client.post(
        f"/api/v1/projects/{project_id}/deck/generate", json={}, headers=headers
    )
    assert response.status_code == 202, response.text


async def _set_slide_state(
    slide_id: uuid.UUID,
    *,
    status: str,
    started_at: datetime | None,
) -> None:
    async with async_session_factory() as session:
        slide = await session.get(Slide, slide_id, with_for_update=True)
        assert slide is not None
        slide.status = status
        slide.started_at = started_at
        await session.commit()


async def _slides_by_position(project_id: uuid.UUID) -> list[Slide]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Slide).where(Slide.project_id == project_id).order_by(Slide.position)
        )
        return list(result.scalars())


def _stuck_since(minutes: int = 11) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _fresh_since(minutes: int = 5) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


# ---------- GET deck 读路径对账 ----------


@pytest.mark.asyncio
async def test_get_deck_reconciles_stuck_slide(client: AsyncClient, queue: FakeQueue) -> None:
    """超时 generating 页在 GET deck 时复位 failed；未超时页与 ready 页不动。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    await _set_slide_state(slides[0].id, status="ready", started_at=None)
    await _set_slide_state(slides[1].id, status="generating", started_at=_stuck_since(11))
    await _set_slide_state(slides[2].id, status="generating", started_at=_fresh_since(5))

    response = await client.get(f"/api/v1/projects/{project['id']}/deck", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    by_position = {slide["position"]: slide for slide in payload["slides"]}
    assert by_position[1]["status"] == "ready"
    assert by_position[2]["status"] == "failed"
    assert by_position[2]["error_code"] == SLIDE_ERROR_WORKER_DEAD
    assert by_position[2]["error"] == "生成中断（worker 异常终止），请重试"
    # 未超时的活任务不受影响
    assert by_position[3]["status"] == "generating"
    assert payload["status"] == "generating"


@pytest.mark.asyncio
async def test_get_deck_resets_project_status_when_all_stuck(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """全部页卡死时，project.status 一并归位，deck 不再显示 generating。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers, pages=5)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    for slide in slides:
        await _set_slide_state(slide.id, status="generating", started_at=_stuck_since(12))

    response = await client.get(f"/api/v1/projects/{project['id']}/deck", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "partial"
    assert all(slide["status"] == "failed" for slide in response.json()["slides"])


# ---------- retry / regenerate / cancel 三条写路径 ----------


@pytest.mark.asyncio
async def test_retry_stuck_slide_accepted(client: AsyncClient, queue: FakeQueue) -> None:
    """卡死页重试从 409 变为 202。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    stuck = slides[1]
    await _set_slide_state(stuck.id, status="generating", started_at=_stuck_since(11))

    response = await client.post(
        f"/api/v1/projects/{project['id']}/deck/slides/{stuck.id}/retry", headers=headers
    )
    assert response.status_code == 202, response.text
    assert response.json()["pending"] == 1


@pytest.mark.asyncio
async def test_regenerate_accepted_after_reconcile(client: AsyncClient, queue: FakeQueue) -> None:
    """整份再生成从 409 变为 202，且 ready 页保留、只重做卡死/待生成页。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers, pages=5)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    await _set_slide_state(slides[0].id, status="ready", started_at=None)
    await _set_slide_state(slides[1].id, status="generating", started_at=_stuck_since(11))
    await _set_slide_state(slides[2].id, status="generating", started_at=_stuck_since(15))
    # slides[3] 保持 pending

    response = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    assert response.status_code == 202, response.text
    # ready 页不重做：pending = 2 个卡死页 + 2 个原本的 pending 页
    assert response.json()["pending"] == 4

    # ready 页内容原样保留（断点续跑语义）
    refreshed = await _slides_by_position(project_id)
    assert refreshed[0].status == "ready"
    assert refreshed[0].revision == slides[0].revision


@pytest.mark.asyncio
async def test_cancel_stuck_deck_resets_without_flag(client: AsyncClient, queue: FakeQueue) -> None:
    """全部页卡死时，cancel 直接完成复位（202），deck 落到 partial。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers, pages=5)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    for slide in slides:
        await _set_slide_state(slide.id, status="generating", started_at=_stuck_since(10))

    response = await client.post(f"/api/v1/projects/{project['id']}/deck/cancel", headers=headers)
    assert response.status_code == 202

    deck = await client.get(f"/api/v1/projects/{project['id']}/deck", headers=headers)
    assert deck.json()["status"] == "partial"
    assert all(slide["status"] == "failed" for slide in deck.json()["slides"])


@pytest.mark.asyncio
async def test_fresh_generating_still_protected(client: AsyncClient, queue: FakeQueue) -> None:
    """未超时的 generating 页是活任务：retry 与再生成仍 409，不误杀。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    fresh = slides[1]
    await _set_slide_state(fresh.id, status="generating", started_at=_fresh_since(5))

    retry = await client.post(
        f"/api/v1/projects/{project['id']}/deck/slides/{fresh.id}/retry", headers=headers
    )
    assert retry.status_code == 409

    regenerate = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    assert regenerate.status_code == 409


@pytest.mark.asyncio
async def test_legacy_stuck_row_without_started_at_untouched(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """历史脏行（started_at 为 NULL）不可判定：不自动复位，仍按活任务保护。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    legacy = slides[1]
    await _set_slide_state(legacy.id, status="generating", started_at=None)

    response = await client.get(f"/api/v1/projects/{project['id']}/deck", headers=headers)
    by_position = {slide["position"]: slide for slide in response.json()["slides"]}
    assert by_position[2]["status"] == "generating"


# ---------- outline 对称死锁 ----------


@pytest.mark.asyncio
async def test_stuck_outline_can_regenerate(client: AsyncClient, queue: FakeQueue) -> None:
    """卡死的大纲：GET 显示 failed，再生成从 409 变 202。"""
    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "大纲对账", "page_count": 5, "layout_mode": "flex"},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "验证大纲侧的崩溃恢复"},
        headers=headers,
    )
    project_id = uuid.UUID(project["id"])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Project).options(selectinload(Project.outline)).where(Project.id == project_id)
        )
        record = result.scalar_one()
        session.add(
            ProjectOutline(
                project_id=record.id,
                status="generating",
                job_id=f"outline-{project_id}-deadbeef",
                started_at=_stuck_since(30),
            )
        )
        await session.commit()

    outline = await client.get(f"/api/v1/projects/{project['id']}/outline", headers=headers)
    assert outline.status_code == 200
    assert outline.json()["status"] == "failed"

    regenerate = await client.post(
        f"/api/v1/projects/{project['id']}/outline/generate", headers=headers
    )
    assert regenerate.status_code == 202, regenerate.text

    # 新一轮生成写入了新的 started_at
    async with async_session_factory() as session:
        result = await session.execute(
            select(ProjectOutline).where(ProjectOutline.project_id == project_id)
        )
        row = result.scalar_one()
        assert row.status == "generating"
        assert row.started_at is not None
        assert row.started_at > _stuck_since(1)


@pytest.mark.asyncio
async def test_fresh_outline_still_protected(client: AsyncClient, queue: FakeQueue) -> None:
    """未超时的大纲任务仍被 409 保护。"""
    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "大纲保护", "page_count": 5, "layout_mode": "flex"},
        headers=headers,
    )
    project = response.json()
    await client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={"kind": "topic", "content": "验证未超时大纲仍受保护"},
        headers=headers,
    )
    project_id = uuid.UUID(project["id"])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Project).options(selectinload(Project.outline)).where(Project.id == project_id)
        )
        record = result.scalar_one()
        session.add(
            ProjectOutline(
                project_id=record.id,
                status="generating",
                job_id=f"outline-{project_id}-cafef00d",
                started_at=_fresh_since(5),
            )
        )
        await session.commit()

    regenerate = await client.post(
        f"/api/v1/projects/{project['id']}/outline/generate", headers=headers
    )
    assert regenerate.status_code == 409


# ---------- worker 写入 started_at + 错误分类 ----------


@pytest.mark.asyncio
async def test_worker_marks_started_at(client: AsyncClient, queue: FakeQueue) -> None:
    """_mark_generating 进入 generating 时写入 started_at 与清空 error_code。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers, pages=5)
    project_id = uuid.UUID(project["id"])
    await _start_generation(client, headers, project["id"])

    slides = await _slides_by_position(project_id)
    before = datetime.now(UTC)
    assert await _mark_generating(slides[0].id)

    refreshed = await _slides_by_position(project_id)
    assert refreshed[0].status == "generating"
    assert refreshed[0].started_at is not None
    assert refreshed[0].started_at >= before


def test_classify_generation_error() -> None:
    assert classify_generation_error(LLMNotConfiguredError("缺凭证")) == (
        SLIDE_ERROR_LLM_NOT_CONFIGURED
    )
    assert classify_generation_error(InvalidSlideOutputError("结构不合法")) == (
        SLIDE_ERROR_LLM_OUTPUT_INVALID
    )

    class APITimeoutError(Exception):
        pass

    wrapped = InvalidSlideOutputError("模型返回的页面 JSON 不符合约定结构")
    wrapped.__cause__ = APITimeoutError("request timed out")
    assert classify_generation_error(wrapped) == SLIDE_ERROR_LLM_TIMEOUT

    assert classify_generation_error(RuntimeError("其他异常")) == "internal_error"
