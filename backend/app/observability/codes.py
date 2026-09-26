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

# --- obs#5 新增（图片降级链） ---
IMAGE_GEN_ERROR = "image_gen_error"  # AI 生图级未产出图（异常或空响应），降级到下一级

# --- obs/10 新增（编辑环工具调用） ---
TOOL_UNKNOWN = "tool_unknown"  # 模型调用了未注册的工具名
# 域规则拒绝（locked 块/块不存在/类型不符）：工具正常工作，改动未生效
TOOL_REJECTED = "tool_rejected"
TOOL_ERROR = "tool_error"  # 工具执行抛异常
