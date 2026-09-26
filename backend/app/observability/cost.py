"""成本折算（obs#9）：token / AI 生图量 × env 单价，纯函数无 IO。

口径：
- 文本 LLM：prompt / completion 分别的每百万 token 单价；
- AI 生图：每张单价，计费张数 = span_kind='image' 且 name='image.ai'
  且 status='succeeded'（失败的生图调用不保证实际扣费，v1 只数交付张数）；
- Unsplash 与占位图免费，不参与计算。

单价未配置（全 0）时 configured=False，各项成本为 0——展示侧据此
区分「免费」与「未配置」，而不是显示误导性的 ¥0。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """一次聚合的成本结果（人民币元，不四舍五入，展示层再格式化）。"""

    llm_cost: float
    image_cost: float
    configured: bool

    @property
    def total_cost(self) -> float:
        return self.llm_cost + self.image_cost


def prices_configured(settings: Settings) -> bool:
    return any(
        (
            settings.llm_price_per_mtok_prompt,
            settings.llm_price_per_mtok_completion,
            settings.image_price_per_unit,
        )
    )


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    ai_image_count: int,
    *,
    settings: Settings | None = None,
) -> CostBreakdown:
    """token 与 AI 生图张数 → 成本。settings 缺省读全局配置。"""
    cfg = settings or get_settings()
    llm_cost = (
        prompt_tokens / 1_000_000 * cfg.llm_price_per_mtok_prompt
        + completion_tokens / 1_000_000 * cfg.llm_price_per_mtok_completion
    )
    image_cost = ai_image_count * cfg.image_price_per_unit
    return CostBreakdown(
        llm_cost=llm_cost,
        image_cost=image_cost,
        configured=prices_configured(cfg),
    )


__all__ = ["CostBreakdown", "compute_cost", "prices_configured"]
