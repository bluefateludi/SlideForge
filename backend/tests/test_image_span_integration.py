"""obs#5 集成测试：图片降级链 image.* 子 span。

套路沿用 test_span_integration.py（真实 PG + FakeQueue + Fake 生成器），
图片链路用可控 Fake provider 通过 ctx["image_pipeline"] 注入，
覆盖三条路径：全降级到占位图 / AI 异常被图库接住 / AI 直接命中。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_queue
from app.core.db import async_session_factory
from app.images.base import ImageAsset, ImageRequest
from app.images.pipeline import ImagePipeline
from app.main import app
from app.models.project import Project, ProjectOutline
from app.models.slide import Slide as SlideRow
from app.observability.models import Span
from app.worker.deck_tasks import generate_deck


def _minimal_png() -> bytes:
    # 直接复用 test_images 的最小 PNG 构造，避免跨文件 import 测试模块
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + tag + data + crc

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    idat = chunk(b"IDAT", zlib.compress(raw))
    return signature + ihdr + idat + chunk(b"IEND", b"")


class _FakeProvider:
    """可控图源：可设定模型名、返回资产或抛异常。"""

    def __init__(
        self,
        *,
        source: str,
        model: str | None = None,
        asset: ImageAsset | None = None,
        error: Exception | None = None,
    ) -> None:
        self.source = source  # type: ignore[assignment]
        self._model = model
        self._asset = asset
        self._error = error

    def available(self) -> bool:
        return True

    async def fetch(self, request: ImageRequest) -> ImageAsset | None:
        if self._error is not None:
            raise self._error
        return self._asset


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def enqueue_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return object()


@pytest.fixture
async def queue() -> AsyncGenerator[FakeQueue, None]:
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
        json={"email": f"obs5_{uuid.uuid4().hex}@example.com", "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _confirmed_one_page_project(client: AsyncClient, headers: dict[str, str]) -> dict:
    """单页项目：大纲页含 image 块的 flex 草稿由 Fake 生成器产出。"""
    response = await client.post(
        "/api/v1/projects",
        json={"title": "obs5 image spans", "page_count": 5},
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
                        "title": "示意图页",
                        "objective": "展示配图",
                        "key_points": ["要点一", "要点二"],
                        "source_refs": ["S1:1"],
                        "layout_id": "bullets",
                        "page_role": "content",
                    }
                ],
                revision=2,
            )
        )
        record.status = "outline_ready"
        await session.commit()
    return project


class _FakeFlexGenerator:
    """机械填充带图片块的 flex 草稿（复用 test_span_integration 套路）。"""

    async def generate(self, payload):
        from app.domain.flex_layout import FlexContainer, FlexLeaf
        from app.domain.slide_draft import (
            FlexBulletsContent,
            FlexImageContent,
            FlexSlideDraft,
            FlexTextContent,
        )

        blocks = [
            FlexTextContent(id="title", text="本页核心结论与行动建议"),
            FlexBulletsContent(id="body", items=["要点一", "要点二"]),
            FlexImageContent(id="visual", alt="示意图"),
        ]
        tree = FlexContainer(
            type="column",
            id="root",
            children=[FlexLeaf(id=f"leaf-{b.id}", block_id=b.id, grow=1.0) for b in blocks],
        )
        return FlexSlideDraft(blocks=blocks, layout_tree=tree, speaker_notes="讲稿提示")


async def _run_deck_with_pipeline(
    client: AsyncClient, queue: FakeQueue, headers: dict[str, str], project: dict, pipeline
) -> uuid.UUID:
    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/deck/generate", json={}, headers=headers
    )
    body = accepted.json()
    slide_ids = queue.calls[-1][0][2]
    await generate_deck(
        {"slide_generator": _FakeFlexGenerator(), "image_pipeline": pipeline},
        project["id"],
        slide_ids,
        body["trace_id"],
        body["job_id"],
    )
    return uuid.UUID(body["trace_id"])


async def _spans(trace_id: uuid.UUID) -> list[Span]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Span).where(Span.trace_id == trace_id).order_by(Span.id)
        )
        return list(result.scalars())


async def _slide_image_block(project_id: str) -> dict:
    async with async_session_factory() as session:
        result = await session.execute(
            select(SlideRow).where(SlideRow.project_id == uuid.UUID(project_id))
        )
        row = result.scalar_one()
    return next(block for block in row.blocks if block["type"] == "image")


async def test_image_spans_full_fallback_to_placeholder(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """两级图源均未命中：ai failed→unsplash failed→placeholder succeeded，父级均为 slide[1]。"""
    headers = await _sign_up(client)
    project = await _confirmed_one_page_project(client, headers)
    pipeline = ImagePipeline(
        [
            _FakeProvider(source="generated", model="fake-image-model", asset=None),
            _FakeProvider(source="stock", asset=None),
        ]  # type: ignore[list-item]
    )
    trace_id = await _run_deck_with_pipeline(client, queue, headers, project, pipeline)

    spans = await _spans(trace_id)
    (task_span,) = [s for s in spans if s.name == "slide[1]"]
    assert task_span.span_kind == "task"
    assert task_span.status == "succeeded", "降级是兜底成功，页 span 不因此 failed"

    by_name = {s.name: s for s in spans if s.span_kind == "image"}
    assert set(by_name) == {"image.ai", "image.unsplash", "image.placeholder"}

    ai = by_name["image.ai"]
    stock = by_name["image.unsplash"]
    placeholder = by_name["image.placeholder"]
    for span in (ai, stock, placeholder):
        assert span.parent_span_id == task_span.id, "image.* 必须挂在 slide[N] 页 span 下"
        assert span.duration_ms is not None

    assert ai.status == "failed"
    assert ai.error_code == "image_gen_error"
    assert (ai.attributes or {})["provider"] == "_FakeProvider"
    assert (ai.attributes or {})["model"] == "fake-image-model"
    assert stock.status == "failed"
    assert stock.error_code is None, "图库未命中不算生图错误"
    assert placeholder.status == "succeeded"
    assert (placeholder.attributes or {})["reason"] == "图源未命中"
    # 草稿规范化会给块 id 加 slide 前缀，只断言可定位到图片块
    assert str((placeholder.attributes or {})["block_id"]).endswith("visual")

    # 业务结果：块保留占位图
    image = await _slide_image_block(project["id"])
    assert image["url"] is None
    assert image["source"] == "placeholder"


async def test_image_spans_ai_error_degrades_to_unsplash(
    client: AsyncClient, queue: FakeQueue
) -> None:
    """AI 生图抛异常：image.ai failed(image_gen_error) 后 Unsplash 接住，页面仍成功。"""
    headers = await _sign_up(client)
    project = await _confirmed_one_page_project(client, headers)
    pipeline = ImagePipeline(
        [
            _FakeProvider(source="generated", error=RuntimeError("生成服务不可用")),
            _FakeProvider(
                source="stock",
                asset=ImageAsset(
                    data=_minimal_png(),
                    content_type="image/png",
                    source="stock",
                    credit="Photo by Ada on Unsplash",
                ),
            ),
        ]  # type: ignore[list-item]
    )
    trace_id = await _run_deck_with_pipeline(client, queue, headers, project, pipeline)

    spans = await _spans(trace_id)
    (task_span,) = [s for s in spans if s.name == "slide[1]"]
    assert task_span.status == "succeeded"

    by_name = {s.name: s for s in spans if s.span_kind == "image"}
    assert set(by_name) == {"image.ai", "image.unsplash"}, "图库接住后不应出现占位图 span"

    ai = by_name["image.ai"]
    assert ai.status == "failed"
    assert ai.error_code == "image_gen_error"
    assert ai.error_message == "生成服务不可用"
    assert ai.parent_span_id == task_span.id

    stock = by_name["image.unsplash"]
    assert stock.status == "succeeded"
    assert stock.parent_span_id == task_span.id

    # 业务结果：块落到图库图片
    image = await _slide_image_block(project["id"])
    assert image["url"] is not None
    assert image["source"] == "stock"
    assert image["credit"] == "Photo by Ada on Unsplash"


async def test_image_spans_ai_direct_hit(client: AsyncClient, queue: FakeQueue) -> None:
    """AI 直接命中：只有 image.ai succeeded（model 列带出），无后续降级 span。"""
    headers = await _sign_up(client)
    project = await _confirmed_one_page_project(client, headers)
    pipeline = ImagePipeline(
        [
            _FakeProvider(
                source="generated",
                model="fake-image-model",
                asset=ImageAsset(data=_minimal_png(), content_type="image/png", source="generated"),
            ),
            _FakeProvider(source="stock", asset=None),
        ]  # type: ignore[list-item]
    )
    trace_id = await _run_deck_with_pipeline(client, queue, headers, project, pipeline)

    spans = await _spans(trace_id)
    (task_span,) = [s for s in spans if s.name == "slide[1]"]
    image_spans = [s for s in spans if s.span_kind == "image"]
    assert [s.name for s in image_spans] == ["image.ai"]
    (ai,) = image_spans
    assert ai.status == "succeeded"
    assert ai.error_code is None
    assert ai.model == "fake-image-model"
    assert ai.parent_span_id == task_span.id

    image = await _slide_image_block(project["id"])
    assert image["url"] is not None
    assert image["source"] == "generated"
