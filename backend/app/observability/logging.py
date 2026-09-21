"""统一日志配置（obs#1）：所有日志行携带当前 trace_id。

API 进程（create_app）与 worker 进程（startup 钩子）各调用一次
setup_logging；不配置时保持 uvicorn / arq 默认行为。
"""

from __future__ import annotations

import logging
import logging.config

from app.observability import context

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [trace_id=%(trace_id)s] %(message)s"


class TraceIdFilter(logging.Filter):
    """给每条 record 注入 trace_id（无上下文时为 "-"），供格式串消费。"""

    def filter(self, record: logging.LogRecord) -> bool:
        trace_id = context.current_trace_id()
        record.trace_id = str(trace_id) if trace_id is not None else "-"
        return True


def setup_logging() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "trace_id": {"()": TraceIdFilter},
            },
            "formatters": {
                "standard": {"format": _FORMAT},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "filters": ["trace_id"],
                },
            },
            "root": {
                "level": "INFO",
                "handlers": ["console"],
            },
        }
    )
