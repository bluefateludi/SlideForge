"""固定页面 → 真实编辑 API → 真实 Trace / 报告数据库；模型使用脚本替身。"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.db import async_session_factory
from app.eval.edit_cases import load_edit_cases
from app.eval.edit_persistence import build_edit_run
from app.eval.edit_runner import EditEvalRunner
from app.llm.slide_edit import DeepSeekSlideEditGenerator
from app.main import app
from app.models.project import ProjectOutline
from app.models.slide import Slide
from app.observability.models import Span


class ScriptedModel:
    model_name = "offline-script"

    def __init__(self):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        self.messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "replace_text",
                        "args": {"block_id": "title", "text": "营收翻倍之路"},
                        "id": "fixed_call",
                    }
                ],
                usage_metadata=usage,
            ),
            AIMessage(content="完成", usage_metadata=usage),
        ]

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return self.messages.pop(0)


@pytest.mark.asyncio
async def test_fixed_fixture_real_api_trace_and_report(monkeypatch):
    case = next(case for case in load_edit_cases() if case.id == "replace/retitle-page")
    generator = DeepSeekSlideEditGenerator(model=ScriptedModel(), api_key="offline")
    monkeypatch.setattr("app.api.v1.deck.ai_edit.create_slide_edit_generator", lambda: generator)
    paths = []

    async def capture(request):
        paths.append(request.url.path)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test/api/v1/",
        event_hooks={"request": [capture]},
    ) as client:
        result = (await EditEvalRunner(client).run([case]))[0]
        assert result.ok, result
        assert result.trace_id is not None
        assert result.prompt_tokens == 20
        assert result.completion_tokens == 10
        assert not any(path.endswith("/generate") for path in paths)
        async with async_session_factory() as session:
            spans = (
                await session.scalars(
                    select(Span).where(Span.trace_id == uuid.UUID(result.trace_id))
                )
            ).all()
            model_calls = [span for span in spans if span.span_kind == "llm"]
            assert len(model_calls) == 2
            assert all(
                span.model == "offline-script" and span.duration_ms is not None
                for span in model_calls
            )
            tool = next(span for span in spans if span.span_kind == "tool")
            assert tool.duration_ms is not None
            outline = (
                await session.scalars(
                    select(ProjectOutline).where(
                        ProjectOutline.project_id == uuid.UUID(result.project_id)
                    )
                )
            ).one()
            assert outline.pages[0]["id"] == str(
                (await session.get(Slide, uuid.UUID(result.slide_id))).outline_page_id
            )
            assert outline.pages[0]["objective"] == case.initial_outline.objective
            assert tool.parent_span_id is not None
            assert tool.attributes["tool_call_id"] == "fixed_call"
            assert tool.attributes["call_index"] == 1
            assert "营收翻倍之路" not in str(tool.attributes)
            page = await session.get(Slide, uuid.UUID(result.slide_id))
            assert page.blocks == [
                block.model_dump(mode="json") for block in case.initial_slide.blocks
            ]
            run = build_edit_run([case], [result])
            session.add(run)
            await session.commit()
            run_id = run.id
        auth = await client.post(
            "auth/register",
            json={"email": f"report-{uuid.uuid4().hex}@example.com", "password": "password123"},
        )
        headers = {"Authorization": f"Bearer {auth.json()['access_token']}"}
        response = await client.get(f"eval/runs/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["cases_version"].startswith("edit-v2-")
        assert body["rows"][0]["category"] == "edit"
        assert body["rows"][0]["trace_id"] == result.trace_id
        assert body["rows"][0]["checks"]
        assert body["rows"][0]["edit_sample"]["initial_outline"]["objective"] == (
            case.initial_outline.objective
        )
        assert body["rows"][0]["operations"][0]["after"]["text"] == "营收翻倍之路"
        assert body["rows"][0]["result_slide"]["blocks"][0]["text"] == "营收翻倍之路"
        assert body["rows"][0]["edit_elapsed_seconds"] is not None
