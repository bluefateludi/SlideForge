"""从外部 HTTP 行为验证：固定页评测只调用编辑，不调用生成。"""

import httpx
import pytest

from app.eval.edit_cases import load_edit_cases
from app.eval.edit_runner import EditEvalRunner, FixtureTarget
from app.eval.trace_join import TraceJoinResult


@pytest.mark.asyncio
@pytest.mark.parametrize("outline_matches", [True, False])
async def test_runner_only_registers_reads_fixture_and_requests_edit(outline_matches):
    case = next(c for c in load_edit_cases() if c.id == "replace/retitle-page")
    seen = []

    async def prepare(owner_id, prepared_case):
        assert owner_id == "owner"
        assert prepared_case.initial_slide == case.initial_slide
        return FixtureTarget("project", "slide")

    async def measure(trace_id):
        assert trace_id == "trace"
        return TraceJoinResult(
            tokens_source="trace",
            prompt_tokens=100,
            completion_tokens=20,
            total_cost=0.12,
            measurement_complete=True,
            cost_configured=True,
        )

    def http(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/auth/register":
            return httpx.Response(201, json={"access_token": "token", "user": {"id": "owner"}})
        if request.url.path == "/projects/project/deck":
            return httpx.Response(
                200,
                json={
                    "slides": [
                        {
                            "id": "slide",
                            "revision": 1,
                            "blocks": [
                                block.model_dump(mode="json") for block in case.initial_slide.blocks
                            ],
                            "layout_tree": case.initial_slide.layout_tree.model_dump(mode="json"),
                        }
                    ]
                },
            )
        if request.url.path == "/projects/project/outline":
            page = case.initial_outline.model_dump(mode="json")
            if not outline_matches:
                page["objective"] = "不同的页面目标"
            return httpx.Response(
                200,
                json={
                    "status": "confirmed",
                    "pages": [page],
                },
            )
        if request.url.path.endswith("/ai-edit"):
            return httpx.Response(
                200,
                json={
                    "trace_id": "trace",
                    "operations": [
                        {
                            "op": "replace",
                            "block_id": "title",
                            "type": "text",
                            "after": {"type": "text", "text": "营收翻倍之路"},
                        }
                    ],
                },
            )
        raise AssertionError(f"不应调用 {request.url.path}")

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(http)
    ) as client:
        result = (await EditEvalRunner(client, prepare=prepare, measure=measure).run([case]))[0]
    if not outline_matches:
        assert not result.ok
        assert "大纲" in result.error
        assert not any(path.endswith("ai-edit") for _, path in seen)
        return
    assert result.ok
    assert result.trace_id == "trace"
    assert result.prompt_tokens == 100
    assert result.operations == [
        {
            "op": "replace",
            "block_id": "title",
            "type": "text",
            "after": {"type": "text", "text": "营收翻倍之路"},
        }
    ]
    assert result.result_slide["blocks"][0]["text"] == "营收翻倍之路"
    assert result.edit_elapsed_seconds is not None
    assert seen == [
        ("POST", "/auth/register"),
        ("GET", "/projects/project/deck"),
        ("GET", "/projects/project/outline"),
        ("POST", "/projects/project/deck/slides/slide/ai-edit"),
    ]


@pytest.mark.asyncio
async def test_mismatched_fixture_stops_before_model_call():
    case = load_edit_cases()[0]
    seen = []

    async def prepare(owner_id, prepared_case):
        return FixtureTarget("project", "slide")

    def http(request):
        seen.append(request.url.path)
        if request.url.path == "/auth/register":
            return httpx.Response(201, json={"access_token": "token", "user": {"id": "owner"}})
        return httpx.Response(200, json={"slides": []})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(http)
    ) as client:
        result = (await EditEvalRunner(client, prepare=prepare).run([case]))[0]
    assert not result.ok
    assert "夹具" in result.error
    assert not any(path.endswith("ai-edit") for path in seen)


@pytest.mark.asyncio
async def test_invalid_fixture_is_rejected_before_any_http_or_model_call():
    case = load_edit_cases()[0].model_copy(update={"theme_id": "missing-theme"})
    seen = []

    def http(request):
        seen.append(request.url.path)
        raise AssertionError("无效样本不应发起 HTTP 请求")

    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(http),
    ) as client:
        result = (await EditEvalRunner(client).run([case]))[0]
    assert not result.ok
    assert result.stage == "validate_fixture"
    assert result.edit_elapsed_seconds is None
    assert seen == []
