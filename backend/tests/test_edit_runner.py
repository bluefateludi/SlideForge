"""从外部 HTTP 行为验证：固定页评测只调用编辑，不调用生成。"""

import httpx
import pytest

from app.eval.edit_cases import load_edit_cases
from app.eval.edit_runner import EditEvalRunner, FixtureTarget
from app.eval.trace_join import TraceJoinResult


@pytest.mark.asyncio
async def test_runner_only_registers_reads_fixture_and_requests_edit():
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
    assert result.ok
    assert result.trace_id == "trace"
    assert result.prompt_tokens == 100
    assert seen == [
        ("POST", "/auth/register"),
        ("GET", "/projects/project/deck"),
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
