"""工具异常路径仅记录结构化定位与错误分类，不持久化参数正文。"""

import pytest

from app.llm.tool_execution import execute_edit_tool


@pytest.mark.asyncio
async def test_unknown_tool_is_traced_without_arbitrary_name(monkeypatch):
    recorded = []

    async def start(name, kind, *, attributes):
        recorded.append((name, attributes))
        return None

    async def finish(handle, status, **kwargs):
        recorded.append((status, kwargs))

    monkeypatch.setattr("app.llm.tool_execution.start_span", start)
    monkeypatch.setattr("app.llm.tool_execution.finish_span", finish)
    result = await execute_edit_tool(
        {},
        {
            "name": "正文不要落库",
            "id": "call_2",
            "args": {"text": "秘密正文"},
        },
        round_index=2,
        call_index=3,
    )
    assert result.message.startswith("错误")
    assert recorded[0][0] == "tool.unknown"
    assert recorded[0][1] == {"round": 2, "call_index": 3, "tool_call_id": "call_2"}
    assert recorded[1][1]["error_code"] == "tool_unknown"
    assert "正文" not in str(recorded)


@pytest.mark.asyncio
async def test_tool_exception_is_sanitized_before_outer_trace_records_it(monkeypatch):
    recorded = []

    async def finish(handle, status, **kwargs):
        recorded.append(kwargs)

    class InvalidTool:
        async def ainvoke(self, args):
            raise ValueError("invalid input_value=秘密正文")

    monkeypatch.setattr("app.llm.tool_execution.finish_span", finish)
    with pytest.raises(RuntimeError, match="Edit tool invocation failed") as failure:
        await execute_edit_tool(
            {"replace_text": InvalidTool()},
            {
                "name": "replace_text",
                "id": "call_1",
                "args": {"text": "秘密正文"},
            },
            round_index=1,
            call_index=1,
        )
    assert recorded[0]["error_message"] == "ValueError"
    assert "秘密正文" not in str(failure.value)
