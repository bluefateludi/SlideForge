"""编辑内部工具的统一调用入口；Trace 仅保存定位信息，不保存内容参数。"""

from dataclasses import dataclass

from app.observability import codes
from app.observability.recorder import finish_span, start_span


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    message: str


async def execute_edit_tool(
    tool_map, call: dict, *, round_index: int, call_index: int
) -> ToolResult:
    name = str(call.get("name") or "")
    args = call.get("args") or {}
    call_id = str(call.get("id") or "")
    tool = tool_map.get(name)
    attributes = {"round": round_index, "call_index": call_index, "tool_call_id": call_id[:128]}
    for key in ("block_id", "after_block_id"):
        value = args.get(key) if isinstance(args, dict) else None
        if isinstance(value, str):
            attributes[key] = value[:128]
    # 未知名称可能来自模型任意输出，避免把正文当成名称持久化。
    handle = await start_span(
        f"tool.{name}" if tool is not None else "tool.unknown",
        "tool",
        attributes=attributes,
    )
    if tool is None:
        await finish_span(
            handle, "failed", error_code=codes.TOOL_UNKNOWN, error_message="Unknown edit tool"
        )
        return ToolResult(call_id, "错误：未知工具，请使用已提供的编辑工具")
    try:
        message = str(await tool.ainvoke(args))
    except Exception as error:
        # Pydantic 错误会包含 input_value；错误正文不能写入数据库。
        await finish_span(
            handle, "failed", error_code=codes.TOOL_ERROR, error_message=error.__class__.__name__
        )
        raise RuntimeError("Edit tool invocation failed") from None
    rejected = message.startswith("错误")
    await finish_span(
        handle,
        "failed" if rejected else "succeeded",
        error_code=codes.TOOL_REJECTED if rejected else None,
        error_message="Edit tool rejected the operation" if rejected else None,
    )
    return ToolResult(call_id, message)
