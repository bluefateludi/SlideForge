"""Trace 缺失不等于零用量；聚合的完整性通过公开结果传递。"""

import uuid

import pytest

from app.core.config import Settings
from app.eval.trace_join import join_trace_metrics

OUTLINE = uuid.UUID("00000000-0000-0000-0000-000000000001")
DECK = uuid.UUID("00000000-0000-0000-0000-000000000002")


class SpanStore:
    """数据库边界替身：不调用数据库或模型。"""

    def __init__(self, rows=(), error=False):
        self.rows = rows
        self.error = error

    async def execute(self, statement):
        if self.error:
            raise ConnectionError("数据库不可用")
        return self

    def all(self):
        return self.rows


def usage(trace_id, prompt=0, completion=0):
    return (trace_id, "llm", "llm", "succeeded", 10, prompt, completion, "test-model", 1, None, {})


async def measure(store):
    return await join_trace_metrics(
        store,
        outline_trace_id=OUTLINE,
        deck_trace_id=DECK,
        settings=Settings(llm_price_per_mtok_prompt=1, llm_price_per_mtok_completion=1),
    )


@pytest.mark.asyncio
async def test_real_zero_usage_is_complete():
    result = await measure(SpanStore([usage(OUTLINE), usage(DECK)]))
    assert result.measurement_complete is True
    assert result.prompt_tokens == 0
    assert result.total_cost == 0


@pytest.mark.asyncio
async def test_one_stage_missing_is_incomplete_even_if_other_has_tokens():
    result = await measure(SpanStore([usage(OUTLINE, 100, 20)]))
    assert result.measurement_complete is False
    assert "partial_trace_usage" in result.measurement_issues


@pytest.mark.asyncio
async def test_one_llm_span_missing_usage_invalidates_aggregate():
    result = await measure(
        SpanStore(
            [
                usage(OUTLINE, 100, 20),
                usage(DECK, 200, 30),
                usage(DECK, None, None),
            ]
        )
    )
    assert result.measurement_complete is False
    assert "incomplete_llm_usage" in result.measurement_issues


@pytest.mark.asyncio
async def test_query_failure_preserves_missing_reason():
    result = await measure(SpanStore(error=True))
    assert result.measurement_complete is False
    assert result.measurement_issues == ["query_error"]


@pytest.mark.asyncio
async def test_negative_usage_is_not_valid_measurement():
    result = await measure(SpanStore([usage(OUTLINE, -1, 0), usage(DECK)]))
    assert result.measurement_complete is False
    assert "invalid_usage" in result.measurement_issues
