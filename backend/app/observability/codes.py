"""trace / span 的 error_code 规范枚举（obs#2）。

trace 与 span 共用这一套字符串口径，落库列是自由 String，
集中成常量只为让 catch 点不会各拼各的拼写。可随实施补充。
"""

from __future__ import annotations

# --- obs#1 已有（trace 级收口沿用） ---
LLM_ERROR = "llm_error"  # 模型输出不符合契约（worker 级收口）
LLM_NOT_CONFIGURED = "llm_not_configured"
INTERNAL_ERROR = "internal_error"
STALE_JOB = "stale_job"
SLIDE_FAILURES = "slide_failures"
CANCELLED = "cancelled"

# --- obs#2 新增（span 级细化） ---
LLM_SCHEMA_ERROR = "llm_schema_error"  # with_structured_output 解析/校验失败
LLM_TIMEOUT = "llm_timeout"  # 供应商/SDK 超时（APITimeoutError 等）
RENDER_ERROR = "render_error"  # PPTX 渲染异常
VERIFY_ERROR = "verify_error"  # 导出回读验证异常
