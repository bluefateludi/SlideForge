"""obs#6 集成测试：ai_edit 同步管线 trace（一次请求一条，出口即收口）。

套路沿用 test_slide_ai_edit_api.py（真实 PG + Fake 生成器）+
test_span_integration.py 的 span 树断言：
- 成功：traces 有 ai_edit 行已收口 succeeded，spans 含节点（ai_edit.*）+ llm
- 失败注入：failed + error_code（llm_error / llm_timeout）
- 埋点吞异常红线：recorder 落库挂掉时 AI 编辑照常工作
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.db import async_session_factory
from app.domain.slide_patch import TextPatch
from app.llm.errors import InvalidSlideEditOutputError
from app.llm.slide_edit import DeepSeekSlideEditGenerator
from app.main import app
from app.models.project import Project, ProjectOutline
from app.models.slide import Slide as SlideRow
from app.observability.models import Span, Trace


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _sign_up(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"ai_edit_obs_{uuid.uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _bullets_blocks() -> list[dict]:
    return [
        {"id": "t1", "slot_id": "title", "type": "text", "text": "原标题", "locked": False},
        {
            "id": "b1",
            "slot_id": "body",
            "type": "bullets",
            "items": ["要点一", "要点二"],
            "locked": False,
        },
    ]


async def _project_with_slide(
    client: AsyncClient, headers: dict[str, str]
) -> tuple[dict, SlideRow]:
    response = await client.post(
        "/api/v1/projects",
        json={"title": "AI 局部修改 trace", "page_count": 5, "audience": "管理层"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    project = response.json()

    async with async_session_factory() as session:
        result = await session.execute(
            select(Project)
            .options(selectinload(Project.outline))
            .where(Project.id == uuid.UUID(project["id"]))
        )
        record = result.scalar_one()
        page_id = uuid.uuid4()
        row = SlideRow(
            project_id=record.id,
            outline_page_id=page_id,
            position=1,
            layout_id="bullets",
            layout_mode="fixed",
            title="要点页",
            status="ready",
            blocks=_bullets_blocks(),
            issues=[],
            revision=1,
        )
        session.add(row)
        session.add(
            ProjectOutline(
                project_id=record.id,
                status="confirmed",
                pages=[
                    {
                        "id": str(page_id),
                        "title": "要点页",
                        "objective": "目标",
                        "key_points": ["要点"],
                        "source_refs": [],
                        "layout_id": "bullets",
                    }
                ],
                revision=2,
            )
        )
        record.status = "ready"
        await session.commit()
        await session.refresh(row)
        return project, row


async def _project_traces(project_id: uuid.UUID) -> list[Trace]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Trace).where(Trace.project_id == project_id).order_by(Trace.created_at)
        )
        return list(result.scalars())


async def _trace_spans(trace_id: uuid.UUID) -> list[Span]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Span).where(Span.trace_id == trace_id).order_by(Span.id)
        )
        return list(result.scalars())


class FakeToolModel:
    """最小 bind_tools 模型：直出无工具调用的 AIMessage，一轮即结束工具循环。

    让真实 DeepSeekSlideEditGenerator 被完整穿过（含 llm span 埋点）。
    """

    model_name = "fake-edit"

    def __init__(self, message: AIMessage) -> None:
        self._message = message

    def bind_tools(self, tools):  # noqa: ANN001, ANN201
        return self

    async def ainvoke(self, messages, *args, **kwargs):  # noqa: ANN001, ANN202
        return self._message


class ScriptedEditGenerator:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def generate(self, payload):  # noqa: ANN001, ANN201
        if self.error is not None:
            raise self.error
        return [TextPatch(block_id="t1", text="改写后的标题")]


def _patch_generator(monkeypatch: pytest.MonkeyPatch, generator) -> None:
    monkeypatch.setattr(
        "app.api.v1.deck.ai_edit.create_slide_edit_generator",
        lambda: generator,
    )


async def test_ai_edit_success_closes_trace_with_node_and_llm_spans(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功路径：一次 AI 编辑一条 ai_edit trace，出口收口 succeeded。

    llm span 在工具循环内就地产生（bind_tools 不走 StructuredChatClient
    咽喉点），父 span 是 ai_edit.generate 节点 span。
    """
    message = AIMessage(
        content="已完成",
        usage_metadata={"input_tokens": 55, "output_tokens": 25, "total_tokens": 80},
    )
    _patch_generator(
        monkeypatch,
        DeepSeekSlideEditGenerator(model=FakeToolModel(message), api_key="test-key"),
    )
    headers = await _sign_up(client)
    project, slide = await _project_with_slide(client, headers)

    response = await client.post(
        f"/api/v1/projects/{project['id']}/deck/slides/{slide.id}/ai-edit",
        headers=headers,
        json={"instruction": "改写本页标题", "revision": slide.revision},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trace_id"]

    rows = await _project_traces(uuid.UUID(project["id"]))
    assert len(rows) == 1
    trace = rows[0]
    assert str(trace.id) == body["trace_id"]
    assert trace.kind == "ai_edit"
    assert trace.status == "succeeded"
    assert trace.finished_at is not None
    assert trace.error_code is None

    spans = await _trace_spans(trace.id)
    by_name: dict[str, list[Span]] = {}
    for row in spans:
        by_name.setdefault(row.name, []).append(row)
    # 节点 span：generate 一定有；check 在 route 收口前执行；repair 视校验告警而定
    assert {"ai_edit.generate", "ai_edit.check"} <= set(by_name)
    for name in ("ai_edit.generate", "ai_edit.check"):
        (node,) = by_name[name]
        assert node.span_kind == "node"
        assert node.status == "succeeded"
        assert node.duration_ms is not None

    # llm 子 span：挂在 ai_edit.generate 下，token 与 usage_metadata 一致
    llm_spans = by_name.get("llm", [])
    assert llm_spans, "工具循环必须产生 llm span"
    generate_ids = {row.id for row in by_name["ai_edit.generate"]}
    for llm in llm_spans:
        assert llm.span_kind == "llm"
        assert llm.parent_span_id in generate_ids
        assert llm.status == "succeeded"
        assert llm.purpose == "局部修改页面"
        assert llm.model == "fake-edit"
        assert llm.prompt_tokens == 55
        assert llm.completion_tokens == 25
        assert (llm.attributes or {}).get("purpose") == "局部修改页面"

    # trace API 能服务新 kind（kind 已放宽为 str，按 Literal 收紧会让端点整体 500）
    listed = await client.get(
        f"/api/v1/trace?project_id={project['id']}",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["total"] == 1
    assert page["items"][0]["kind"] == "ai_edit"
    detail = await client.get(f"/api/v1/trace/{body['trace_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["trace"]["kind"] == "ai_edit"


async def test_ai_edit_invalid_output_closes_trace_failed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """结构校验失败：trace 收口 failed + llm_error，HTTP 422 口径不变。"""
    _patch_generator(
        monkeypatch,
        ScriptedEditGenerator(error=InvalidSlideEditOutputError("局部修改后页面结构仍不合法")),
    )
    headers = await _sign_up(client)
    project, slide = await _project_with_slide(client, headers)

    response = await client.post(
        f"/api/v1/projects/{project['id']}/deck/slides/{slide.id}/ai-edit",
        headers=headers,
        json={"instruction": "改写本页", "revision": slide.revision},
    )
    assert response.status_code == 422

    rows = await _project_traces(uuid.UUID(project["id"]))
    assert len(rows) == 1
    assert rows[0].kind == "ai_edit"
    assert rows[0].status == "failed"
    assert rows[0].error_code == "llm_error"
    assert rows[0].finished_at is not None
    # 异常透传路径上的节点 span 收 failed
    spans = await _trace_spans(rows[0].id)
    failed_nodes = [row for row in spans if row.status == "failed"]
    assert [row.name for row in failed_nodes] == ["ai_edit.generate"]


async def test_ai_edit_timeout_closes_trace_llm_timeout(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具循环超时类异常：trace 收口 failed + llm_timeout（codes 既有值）。"""
    _patch_generator(monkeypatch, ScriptedEditGenerator(error=TimeoutError("上游超时")))
    headers = await _sign_up(client)
    project, slide = await _project_with_slide(client, headers)

    with pytest.raises(TimeoutError):
        await client.post(
            f"/api/v1/projects/{project['id']}/deck/slides/{slide.id}/ai-edit",
            headers=headers,
            json={"instruction": "改写本页", "revision": slide.revision},
        )

    rows = await _project_traces(uuid.UUID(project["id"]))
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "llm_timeout"
    assert rows[0].finished_at is not None


async def test_ai_edit_works_when_recorder_db_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """埋点吞异常红线：recorder 落库挂掉时 AI 编辑照常，trace_id 为 null。"""
    from app.observability import recorder

    class _Raising:
        def __aenter__(self):
            raise RuntimeError("db down")

        def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(recorder, "async_session_factory", lambda *a, **k: _Raising())

    _patch_generator(monkeypatch, ScriptedEditGenerator())
    headers = await _sign_up(client)
    project, slide = await _project_with_slide(client, headers)

    response = await client.post(
        f"/api/v1/projects/{project['id']}/deck/slides/{slide.id}/ai-edit",
        headers=headers,
        json={"instruction": "改写本页标题", "revision": slide.revision},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trace_id"] is None
    assert len(body["operations"]) == 1
    assert body["operations"][0]["after"]["text"] == "改写后的标题"

    rows = await _project_traces(uuid.UUID(project["id"]))
    assert rows == [], "trace 建失败不应留下任何行"
