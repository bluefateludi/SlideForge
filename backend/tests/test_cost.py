"""成本折算纯函数（obs#9）：单价换算与「未配置」语义。

显式传 Settings 构造参数（init 优先级高于 env/dotenv），不依赖环境。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.observability.cost import compute_cost, prices_configured


class TestComputeCost:
    def test_zero_prices_mean_unconfigured(self) -> None:
        settings = Settings(
            llm_price_per_mtok_prompt=0.0,
            llm_price_per_mtok_completion=0.0,
            image_price_per_unit=0.0,
        )
        assert prices_configured(settings) is False
        cost = compute_cost(1_000_000, 500_000, 3, settings=settings)
        assert cost.llm_cost == 0.0
        assert cost.image_cost == 0.0
        assert cost.total_cost == 0.0
        assert cost.configured is False

    def test_token_and_image_math(self) -> None:
        settings = Settings(
            llm_price_per_mtok_prompt=2.0,
            llm_price_per_mtok_completion=8.0,
            image_price_per_unit=0.1,
        )
        cost = compute_cost(1_500_000, 500_000, 3, settings=settings)
        # prompt 1.5M × ¥2/M = 3.0；completion 0.5M × ¥8/M = 4.0；生图 3 × 0.1
        assert cost.llm_cost == 7.0
        assert cost.image_cost == pytest.approx(0.3)
        assert cost.total_cost == pytest.approx(7.3)
        assert cost.configured is True

    def test_partial_prices_still_configured(self) -> None:
        # 只配生图单价：LLM 部分为 0，但整体视为已配置
        settings = Settings(image_price_per_unit=0.1)
        cost = compute_cost(999_999, 1, 5, settings=settings)
        assert cost.llm_cost == 0.0
        assert cost.image_cost == pytest.approx(0.5)
        assert cost.configured is True
