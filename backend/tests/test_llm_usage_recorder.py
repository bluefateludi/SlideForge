"""LLM 调用埋点：StructuredChatClient 收口处记录 Token 用量与耗时（eval#1）。

FakeJsonModeModel 复刻 ChatOpenAI.with_structured_output(method="json_mode")
的内部结构（bind(response_format) + PydanticOutputParser）：langchain-core 的
BaseChatModel 基类实现会忽略 method 参数走 function_calling，只有 ChatOpenAI
自己重写了 json_mode 分支，fake 必须同样重写才能走到与生产一致的解析路径。
"""

from __future__ import annotations

import time
from operator import itemgetter
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableMap, RunnablePassthrough
from pydantic import BaseModel

from app.llm.client import (
    StructuredChatClient,
    reset_usage_recorder,
    take_usage_records,
)
from app.llm.errors import InvalidModelOutputError


class _Answer(BaseModel):
    answer: str


class FakeJsonModeModel(GenericFakeChatModel):
    """行为对齐 ChatOpenAI json_mode：AIMessage（含 usage_metadata）直出 JSON 文本。"""

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


def _client(messages: list[AIMessage]) -> StructuredChatClient:
    model = FakeJsonModeModel(messages=iter(messages))
    return StructuredChatClient(model=model, api_key="test-key")


@pytest.fixture(autouse=True)
def _clean_recorder() -> None:
    reset_usage_recorder()


@pytest.mark.asyncio
async def test_complete_records_usage_and_latency() -> None:
    client = _client([_ai('{"answer": "甲"}', prompt_tokens=120, completion_tokens=80)])

    started = time.monotonic()
    result = await client.complete(_Answer, system="s", user="u", purpose="生成大纲")
    elapsed = time.monotonic() - started

    assert result.answer == "甲"

    records = take_usage_records()
    assert len(records) == 1
    record = records[0]
    assert record.purpose == "生成大纲"
    assert record.prompt_tokens == 120
    assert record.completion_tokens == 80
    # complete 内部计时的区间包含在测试计时内，容忍时钟粒度带来的微小偏差
    assert 0 <= record.elapsed_seconds <= elapsed + 1.0


@pytest.mark.asyncio
async def test_records_accumulate_across_calls_in_order() -> None:
    client = _client(
        [
            _ai('{"answer": "甲"}', prompt_tokens=10, completion_tokens=5),
            _ai('{"answer": "乙"}', prompt_tokens=20, completion_tokens=15),
        ]
    )

    await client.complete(_Answer, system="s", user="u", purpose="生成大纲")
    await client.complete(_Answer, system="s", user="u", purpose="生成页面内容")

    records = take_usage_records()
    assert [record.purpose for record in records] == ["生成大纲", "生成页面内容"]
    assert [record.prompt_tokens for record in records] == [10, 20]
    assert [record.completion_tokens for record in records] == [5, 15]


@pytest.mark.asyncio
async def test_take_returns_and_resets_for_snapshot_semantics() -> None:
    client = _client([_ai('{"answer": "甲"}', prompt_tokens=7, completion_tokens=3)])

    await client.complete(_Answer, system="s", user="u", purpose="生成大纲")

    first = take_usage_records()
    second = take_usage_records()

    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_missing_usage_records_zero_tokens_not_error() -> None:
    """模型没回报 usage（部分兼容端）时埋点记 0，不影响生成结果。"""
    client = _client([AIMessage(content='{"answer": "甲"}')])

    result = await client.complete(_Answer, system="s", user="u", purpose="生成大纲")

    assert result.answer == "甲"
    records = take_usage_records()
    assert len(records) == 1
    assert records[0].prompt_tokens == 0
    assert records[0].completion_tokens == 0


@pytest.mark.asyncio
async def test_failed_call_does_not_record() -> None:
    """解析失败抛 InvalidModelOutputError 时不记指标，避免污染聚合口径。"""
    client = _client([AIMessage(content="不是 JSON")])

    with pytest.raises(InvalidModelOutputError):
        await client.complete(_Answer, system="s", user="u", purpose="生成大纲")

    assert take_usage_records() == []
