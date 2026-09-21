"""obs#2 集成测试：outline/slide 节点 span、LLM 咽喉点、deck 每页、export 三步。

套路沿用 test_trace_integration.py（真实 PG + FakeQueue + Fake 生成器），
断言换成 spans 表的形状：一次 outline+deck+export 后能还原完整 span 树。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from operator import itemgetter
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableMap, RunnablePassthrough
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import get_queue
from app.core.db import async_session_factory
from app.main import app
from app.models.project import Project
from app.observability.models import Span, Trace
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
        json={"email": f"obs2_{uuid.uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _spans(trace_id: uuid.UUID) -> list[Span]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Span).where(Span.trace_id == trace_id).order_by(Span.id)
        )
        return list(result.scalars())


async def _spans_by_name(trace_id: uuid.UUID) -> dict[str, list[Span]]:
    rows = await _spans(trace_id)
    grouped: dict[str, list[Span]] = {}
    for row in rows:
        grouped.setdefault(row.name, []).append(row)
    return grouped


# --- LLM 咽喉点：Fake 复刻 ChatOpenAI json_mode（对齐 test_llm_usage_recorder） ---


class FakeJsonModeModel(GenericFakeChatModel):
    """行为对齐 ChatOpenAI json_mode：AIMessage（含 usage_metadata）直出 JSON 文本。"""

    model_name: str = "fake-chat"

    def with_structured_output(
        self,
        schema: type[BaseModel],
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Any:
        llm = self.bind(response_format={"type": "json_object"})
        output_parser = PydanticOutputParser(pydantic_object=schema)
        if not include_raw:
            return llm | output_parser
        parser_assign = RunnablePassthrough.assign(
            parsed=itemgetter("raw") | output_parser,
            parsing_error=lambda _: None,
        )
        parser_none = RunnablePassthrough.assign(parsed=lambda _: None)
        return RunnableMap(raw=llm) | parser_assign.with_fallbacks(
            [parser_none], exception_key="parsing_error"
        )


def _ai(payload: str, *, prompt_tokens: int, completion_tokens: int) -> AIMessage:
    return AIMessage(
        content=payload,
        usage_metadata={
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


def _outline_json(pages: int) -> str:
    import json

    return json.dumps(
        {
            "pages": [
                {
                    "title": f"第 {index} 页",
                    "objective": "说明本页的核心目标",
                    "key_points": ["要点一", "要点二"],
                    "source_refs": ["S1:1"],
                    "layout_id": "cover" if index == 1 else "bullets",
                    "page_role": "cover" if index == 1 else "content",
                    "visual": None,
                }
                for index in range(1, pages + 1)
            ]
        },
        ensure_ascii=False,
    )


class FakeLLMOutlineGenerator:
    """走 StructuredChatClient 的 Fake 大纲生成器，让 LLM 咽喉点被真实穿过。"""

    def __init__(self, messages: list[AIMessage]) -> None:
        from app.llm.client import StructuredChatClient

        self._chat = StructuredChatClient(
            model=FakeJsonModeModel(messages=iter(messages)), api_key="test-key"
        )

    async def generate(self, payload):
        from app.domain.outline import OutlineDraft

        draft = await self._chat.complete(
            OutlineDraft, system="s", user="u", purpose="生成大纲"
        )
        return draft


class FakeLLMSlideGenerator:
    """走 StructuredChatClient 的单页生成器；messages 依次消费。"""

    def __init__(self, messages: list[AIMessage]) -> None:
        from app.llm.client import StructuredChatClient

        self._chat = StructuredChatClient(
            model=FakeJsonModeModel(messages=iter(messages)), api_key="test-key"
        )

    async def generate(self, payload):
        from app.domain.slide_draft import FlexSlideDraft

        return await self._chat.complete(
            FlexSlideDraft, system="s", user="u", purpose="生成灵活布局页面"
        )


def _flex_slide_json(*, title: str = "本页核心结论与行动建议") -> str:
    import json

    return json.dumps(
        {
            "blocks": [
                {"id": "title", "type": "text", "text": title},
                {
                    "id": "body",
                    "type": "bullets",
                    "items": ["要点一：具体结论", "要点二：可展开事实"],
                },
            ],
            "layout_tree": {
                "type": "column",
                "id": "root",
                "children": [
                    {"type": "block", "id": "leaf-title", "block_id": "title"},
                    {"type": "block", "id": "leaf-body", "block_id": "body"},
                ],
            },
            "speaker_notes": "讲稿提示",
        },
        ensure_ascii=False,
    )


# --- 用例 ---


async def test_outline_flow_records_node_and_llm_spans(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """outline 全流程：outline.prepare/outline.generate 节点 span + llm 子 span。"""
    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "obs2 outline spans", "page_count": 5},
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
    trace_id = uuid.UUID(body["trace_id"])

    generator = FakeLLMOutlineGenerator(
        [_ai(_outline_json(5), prompt_tokens=120, completion_tokens=80)]
    )
    await generate_outline(
        {"outline_generator": generator, "job_try": 1},
        project["id"],
        body["job_id"],
        body["trace_id"],
    )

    grouped = await _spans_by_name(trace_id)
    assert set(grouped) == {"outline.prepare", "outline.generate", "llm"}
    # 节点 span 成功收口
    for name in ("outline.prepare", "outline.generate"):
        (node,) = grouped[name]
        assert node.span_kind == "node"
        assert node.status == "succeeded"
        assert node.duration_ms is not None
    # llm 是 generate 节点的子 span，token 与 usage_metadata 一致
    (llm,) = grouped["llm"]
    (generate_node,) = grouped["outline.generate"]
    assert llm.span_kind == "llm"
    assert llm.parent_span_id == generate_node.id
    assert llm.status == "succeeded"
    assert llm.purpose == "生成大纲"
    assert llm.prompt_tokens == 120
    assert llm.completion_tokens == 80
    assert llm.model == "fake-chat"
    assert (llm.attributes or {}).get("purpose") == "生成大纲"


async def test_llm_schema_error_marks_span_failed(client: AsyncClient, queue: FakeQueue) -> None:
    """结构化输出解析失败：llm span status=failed 且 error_code=llm_schema_error。"""
    headers = await _sign_up(client)
    response = await client.post(
        "/api/v1/projects",
        json={"title": "obs2 schema error", "page_count": 5},
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

    generator = FakeLLMOutlineGenerator([_ai("不是 JSON", prompt_tokens=10, completion_tokens=5)])
    from app.worker.retry import MAX_TRIES

    await generate_outline(
        {"outline_generator": generator, "job_try": MAX_TRIES},
        project["id"],
        body["job_id"],
        body["trace_id"],
    )

    trace_id = uuid.UUID(body["trace_id"])
    # 终局失败：trace 也应收 failed + llm_error（obs#1 口径）
    async with async_session_factory() as session:
        trace = await session.get(Trace, trace_id)
    assert trace is not None
    assert trace.status == "failed"
    assert trace.error_code == "llm_error"

    llm_spans = (await _spans_by_name(trace_id))["llm"]
    assert llm_spans[-1].status == "failed"
    assert llm_spans[-1].error_code == "llm_schema_error"
    # 失败调用不记 token
    assert llm_spans[-1].prompt_tokens is None


async def _confirmed_project(client: AsyncClient, headers: dict[str, str]) -> dict:
    from app.models.project import ProjectOutline

    response = await client.post(
        "/api/v1/projects",
        json={"title": "obs2 deck spans", "page_count": 5},
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
                pages=[
                    {
                        "id": str(uuid.uuid4()),
                        "title": f"第 {index} 页",
                        "objective": "说明本页的核心目标",
                        "key_points": ["要点一", "要点二"],
                        "source_refs": ["S1:1"],
                        "layout_id": "cover" if index == 1 else "bullets",
                        "page_role": "cover" if index == 1 else "content",
                    }
                    for index in range(1, 6)
                ],
                revision=2,
            )
        )
        record.status = "outline_ready"
        await session.commit()
    return project


async def test_deck_flow_records_per_slide_spans(client: AsyncClient, queue: FakeQueue) -> None:
    """deck 成功路径：每页 slide[N] task span，页内节点 span 挂其下。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]

    # 消息迭代器按页数 ×2 供（初生成 + 可能的修复轮）
    messages = [_ai(_flex_slide_json(), prompt_tokens=90, completion_tokens=60)] * 10
    await generate_deck(
        {"slide_generator": FakeLLMSlideGenerator(messages)},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )

    trace_id = uuid.UUID(body["trace_id"])
    spans = await _spans(trace_id)
    slide_spans = {s.name: s for s in spans if s.name.startswith("slide[")}
    assert set(slide_spans) == {
        f"slide[{position}]" for position in range(1, 6)
    }, "每页各一个 task span"
    for name, row in slide_spans.items():
        assert row.span_kind == "task"
        assert row.status == "succeeded"
        assert row.attributes["position"] == int(name.removeprefix("slide[").removesuffix("]"))
        assert uuid.UUID(row.attributes["slide_id"]) in {
            uuid.UUID(value) for value in slide_ids
        }

    # 页内节点 span 的 parent 是该页的 slide[N] span（repair 视质量检查而定）
    node_spans = [s for s in spans if s.span_kind == "node"]
    assert {"slide.prepare", "slide.generate", "slide.check"} <= {s.name for s in node_spans}
    for node in node_spans:
        assert node.parent_span_id in {row.id for row in slide_spans.values()}
    # LLM 咽喉点在 slide.generate 节点下（修复轮会多出一次 generate；
    # GenericFakeChatModel 消息迭代器耗尽后走解析失败分支 → llm_schema_error）
    llm_spans = [s for s in spans if s.span_kind == "llm"]
    generate_ids = {s.id for s in node_spans if s.name == "slide.generate"}
    succeeded = [s for s in llm_spans if s.status == "succeeded"]
    assert len(succeeded) >= 5
    for llm in llm_spans:
        assert llm.parent_span_id in generate_ids
    for llm in succeeded:
        assert llm.prompt_tokens == 90
        assert llm.completion_tokens == 60


async def test_deck_failed_slide_span_has_error_code(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """失败注入：第 2 页生成器抛错 → slide[2] span failed 带 error_code。"""
    from app.llm.errors import InvalidSlideOutputError

    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]

    class _FailingGenerator:
        async def generate(self, payload):
            if payload.position == 2:
                raise InvalidSlideOutputError("模拟生成失败")
            return _fake_flex_draft()

    await generate_deck(
        {"slide_generator": _FailingGenerator()},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )

    trace_id = uuid.UUID(body["trace_id"])
    spans = await _spans(trace_id)
    failed = [s for s in spans if s.status == "failed"]
    # 失败被 _generate_one 转公开错误继续收口，页内 generate 节点 span
    # 在异常透传路径上被 span 上下文管理器记为 failed + error_code
    failed_nodes = [s for s in failed if s.span_kind == "node"]
    assert [s.name for s in failed_nodes] == ["slide.generate"]
    assert all(s.error_code == "InvalidSlideOutputError" for s in failed_nodes)
    # 失败页的 task span 存在并归属该页（异常已在页内被转公开错误，
    # task span 以 succeeded 收口；失败信息在 trace 与 slide.error 上）
    failed_tasks = [s for s in spans if s.name == "slide[2]"]
    assert len(failed_tasks) == 1
    assert failed_tasks[0].attributes["position"] == 2
    assert all(node.parent_span_id == failed_tasks[0].id for node in failed_nodes)


def _fake_flex_draft():
    from app.domain.flex_layout import FlexContainer, FlexLeaf
    from app.domain.slide_draft import (
        FlexBulletsContent,
        FlexImageContent,
        FlexKpiContent,
        FlexSlideDraft,
        FlexTextContent,
    )

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


async def _ready_project_with_deck_trace(
    client: AsyncClient, queue: FakeQueue
) -> tuple[dict, uuid.UUID, dict[str, str]]:
    """建项目 → 确认大纲 → 生成全部页面，返回 (project, deck_trace_id, headers)。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]
    await generate_deck(
        {"slide_generator": _FakeFlexGenerator()},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )
    return project, uuid.UUID(body["trace_id"]), headers


class _FakeFlexGenerator:
    """机械填充 flex 草稿（复用 test_trace_integration 套路）。"""

    async def generate(self, payload):
        return _fake_flex_draft()


async def test_export_spans_attach_to_last_deck_trace(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """export 三步 span 挂到 last_deck_trace_id 指向的 deck trace。"""
    project, deck_trace_id, headers = await _ready_project_with_deck_trace(client, queue)

    before = await _spans(deck_trace_id)
    assert not [s for s in before if s.span_kind == "export"]

    response = await client.get(
        f"/api/v1/projects/{project['id']}/deck/export", headers=headers
    )
    assert response.status_code == 200

    after = await _spans(deck_trace_id)
    export_spans = [s for s in after if s.span_kind == "export"]
    assert [s.name for s in export_spans] == [
        "export.quality_check",
        "export.render",
        "export.verify",
    ]
    for row in export_spans:
        assert row.status == "succeeded"
        assert row.duration_ms is not None
    # 未产生新的 trace：export 挂旧 trace 而不是另起
    async with async_session_factory() as session:
        result = await session.execute(
            select(Trace).where(Trace.project_id == uuid.UUID(project["id"]))
        )
        assert len(list(result.scalars())) == 1


async def test_export_without_deck_trace_skips_spans(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """无 last_deck_trace_id（或 trace 不存在）时导出照常、不写任何 span。"""
    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]
    # 直接跑 worker（不经过 API 的 last_deck_trace_id 锚点写入路径也行，但 API
    # 已在 enqueue 前写入锚点；这里显式清掉锚点模拟旧数据）
    await generate_deck(
        {"slide_generator": _FakeFlexGenerator()},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )
    async with async_session_factory() as session:
        row = await session.get(Project, uuid.UUID(project["id"]))
        assert row is not None and row.last_deck_trace_id is not None
        row.last_deck_trace_id = None
        await session.commit()

    response = await client.get(
        f"/api/v1/projects/{project['id']}/deck/export", headers=headers
    )
    assert response.status_code == 200
    spans = await _spans(uuid.UUID(body["trace_id"]))
    assert not [s for s in spans if s.span_kind == "export"], "无锚点时跳过 export 埋点"


async def test_instrumentation_failure_does_not_break_generation(
    client: AsyncClient, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """recorder 落库挂掉时业务照常：页面全部 ready，任务正常收口。"""
    from app.observability import recorder

    class _Raising:
        def __aenter__(self):
            raise RuntimeError("db down")

        def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(recorder, "async_session_factory", lambda *a, **k: _Raising())

    headers = await _sign_up(client)
    project = await _confirmed_project(client, headers)
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]
    await generate_deck(
        {"slide_generator": _FakeFlexGenerator()},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )

    deck = await client.get(f"/api/v1/projects/{project['id']}/deck", headers=headers)
    assert deck.json()["status"] == "ready"
    # export 路径同样不因埋点失败阻塞（trace_exists 查询也挂 → 按无 trace 处理）
    response = await client.get(
        f"/api/v1/projects/{project['id']}/deck/export", headers=headers
    )
    assert response.status_code == 200
