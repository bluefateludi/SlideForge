from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.llm.errors import InvalidModelOutputError, LLMNotConfiguredError
from app.observability import codes
from app.observability.recorder import finish_span, start_span

T = TypeVar("T", bound=BaseModel)

# LCEL 负责一次结构化调用；有状态的校验/修复放在 LangGraph。
_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", "{system}"),
        ("human", "{user}"),
    ]
)

# openai SDK 的自动重试上限；SDK 不暴露实际重试次数，span 只记此配置值
LLM_MAX_RETRIES = 2


@dataclass(frozen=True, slots=True)
class LLMUsageRecord:
    """一次文本 LLM 调用的工程指标；缺 usage 时 token 记 0。"""

    purpose: str
    prompt_tokens: int
    completion_tokens: int
    elapsed_seconds: float


class LLMUsageRecorder:
    """进程内轻量聚合：评测采集器 / 日志 take 一次即取走并重置。

    线程安全即可（ARQ worker 是单进程事件循环，无跨进程消费方），
    不做持久化；记录量级是一次生成的调用数，无需淘汰策略。
    """

    def __init__(self) -> None:
        self._records: list[LLMUsageRecord] = []
        self._lock = threading.Lock()

    def add(self, record: LLMUsageRecord) -> None:
        with self._lock:
            self._records.append(record)

    def take(self) -> list[LLMUsageRecord]:
        """取走自上次 take 以来的调用记录并重置（快照语义）。"""
        with self._lock:
            records = list(self._records)
            self._records.clear()
            return records


_recorder = LLMUsageRecorder()


def take_usage_records() -> list[LLMUsageRecord]:
    """消费方入口：读取并重置进程内已累积的 LLM 调用记录。"""
    return _recorder.take()


def reset_usage_recorder() -> None:
    """清空已累积记录（测试隔离用；take 本身也会重置）。"""
    _recorder.take()


def _extract_usage(message: BaseMessage | None) -> tuple[int, int]:
    """从 AIMessage 提取 (prompt_tokens, completion_tokens)，缺失记 0。"""
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return 0, 0
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def create_chat_model(settings: Settings | None = None) -> ChatOpenAI:
    """用 ChatOpenAI 对接 DeepSeek 兼容接口，业务层不再持有 OpenAI SDK。"""
    cfg = settings or get_settings()
    kwargs: dict = {
        "model": cfg.llm_model,
        "api_key": cfg.llm_api_key or "not-configured",
        "base_url": cfg.llm_base_url,
        "timeout": cfg.llm_timeout_seconds,
        "max_retries": LLM_MAX_RETRIES,
    }
    # 思考模式默认关闭；关闭时不要传 thinking，避免无谓地拉长延迟
    if cfg.llm_thinking_enabled:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    return ChatOpenAI(**kwargs)


def _model_name(model: BaseChatModel) -> str | None:
    """尽力取模型标识（ChatOpenAI 有 model_name / model 属性），失败返回 None。"""
    for attr in ("model_name", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _is_timeout(error: BaseException) -> bool:
    """openai.APITimeoutError 与通用超时类，判不出就按非超时。"""
    if error.__class__.__name__ in {"APITimeoutError", "TimeoutError"}:
        return True
    try:
        from openai import APITimeoutError

        return isinstance(error, APITimeoutError)
    except Exception:
        return False


class StructuredChatClient:
    """prompt | ChatOpenAI.with_structured_output(json_mode) 的薄封装。"""

    def __init__(self, *, model: BaseChatModel, api_key: str) -> None:
        self._model = model
        self._api_key = api_key

    async def complete(
        self,
        schema: type[T],
        *,
        system: str,
        user: str,
        purpose: str,
    ) -> T:
        if not self._api_key.strip():
            raise LLMNotConfiguredError(f"未配置 LLM API Key，无法{purpose}")

        # include_raw 只为拿到 AIMessage（usage_metadata 在消息上）；解析行为不变
        chain = _PROMPT | self._model.with_structured_output(
            schema, method="json_mode", include_raw=True
        )
        # 咽喉点 span（obs#2）：无 trace 上下文时 start_span 返回 None，全程零开销
        llm_span = await start_span(
            "llm",
            "llm",
            attributes={
                "purpose": purpose,
                # 口径说明：openai SDK 不暴露实际重试次数，这里只记配置上限
                "max_retries": LLM_MAX_RETRIES,
            },
        )
        started = time.monotonic()
        try:
            result = await chain.ainvoke({"system": system, "user": user})
        except Exception as error:
            await finish_span(
                llm_span,
                "failed",
                error_code=codes.LLM_TIMEOUT if _is_timeout(error) else codes.LLM_ERROR,
                error_message=str(error) or error.__class__.__name__,
            )
            raise InvalidModelOutputError("模型返回内容不符合约定结构") from error

        raw_message = result.get("raw") if isinstance(result, dict) else None
        parsed = result.get("parsed") if isinstance(result, dict) else result
        parsing_error = result.get("parsing_error") if isinstance(result, dict) else None
        if parsing_error is not None or parsed is None:
            # 解析失败不记指标，避免污染聚合口径；span 侧记 llm_schema_error
            await finish_span(
                llm_span,
                "failed",
                error_code=codes.LLM_SCHEMA_ERROR,
                error_message=str(parsing_error) or "结构化输出解析失败",
            )
            raise InvalidModelOutputError("模型返回内容不符合约定结构")

        prompt_tokens, completion_tokens = _extract_usage(raw_message)
        elapsed = time.monotonic() - started
        _recorder.add(
            LLMUsageRecord(
                purpose=purpose,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                elapsed_seconds=elapsed,
            )
        )
        # 观测日志（obs#1）：trace_id 由 TraceIdFilter 自动携带，这里只补调用细节
        logging.getLogger(__name__).info(
            "LLM 调用完成 purpose=%s elapsed=%.1fs prompt_tokens=%d completion_tokens=%d",
            purpose,
            elapsed,
            prompt_tokens,
            completion_tokens,
        )

        try:
            if isinstance(parsed, schema):
                final = parsed
            elif isinstance(parsed, BaseModel):
                final = schema.model_validate(parsed.model_dump())
            elif isinstance(parsed, dict):
                final = schema.model_validate(parsed)
            else:
                final = None
        except ValidationError as error:
            await finish_span(llm_span, "failed", error_code=codes.LLM_SCHEMA_ERROR)
            raise InvalidModelOutputError("模型返回内容不符合约定结构") from error
        if final is None:
            await finish_span(llm_span, "failed", error_code=codes.LLM_SCHEMA_ERROR)
            raise InvalidModelOutputError("模型返回内容不符合约定结构")

        await finish_span(
            llm_span,
            "succeeded",
            model=_model_name(self._model),
            purpose=purpose,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return final
