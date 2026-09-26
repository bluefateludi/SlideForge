import logging

import httpx

from app.core.config import get_settings
from app.images.bailian import BailianImageProvider
from app.images.base import ImageAsset, ImageProvider, ImageRequest
from app.images.generated import GeneratedImageProvider
from app.images.unsplash import UnsplashImageProvider
from app.observability import codes
from app.observability.recorder import finish_span, start_span

logger = logging.getLogger(__name__)

# 图片降级链层级 → 子 span 名（obs#5）。span_kind 统一 'image'，
# 靠名字区分层级；未登记的 source 不埋点，未来新图源接入时在此补充。
_LEVEL_SPANS: dict[str, str] = {
    "generated": "image.ai",
    "stock": "image.unsplash",
}


class ImagePipeline:
    """按优先级依次尝试图源，全部失败则返回 None 交给占位图。

    顺序是「生图 → 图库」：生图能精确贴合页面语义，图库胜在稳定与真实，
    因此把它放在后面兜底。两级都不可用时不报错，占位图本身就是设计的一部分。
    """

    def __init__(self, providers: list[ImageProvider]) -> None:
        self._providers = providers

    @property
    def enabled(self) -> bool:
        return any(provider.available() for provider in self._providers)

    async def fetch(self, request: ImageRequest) -> ImageAsset | None:
        for provider in self._providers:
            if not provider.available():
                # 未配置凭证：没有发生真实调用，不产生 span
                continue
            asset = await self._fetch_level(provider, request)
            if asset is not None:
                return asset
        return None

    async def _fetch_level(
        self, provider: ImageProvider, request: ImageRequest
    ) -> ImageAsset | None:
        """尝试单级图源，并把该级结果记成 image.* 子 span（obs#5）。

        降级语义：该级没接住（返回 None 或抛异常）记 failed——失败的是
        这一级图源，不是业务；整体由下一级或占位图兜底。AI 生图级未
        产出统一记 image_gen_error（provider 内部已把常见异常吞成 None
        并留有 warning 日志，span 侧不再区分细分原因）。provider 意外
        抛异常同样收口后继续走降级，业务行为与直接返回 None 一致。
        """
        name = _LEVEL_SPANS.get(provider.source)
        if name is None:
            return await provider.fetch(request)
        handle = await start_span(
            name,
            "image",
            attributes={
                "provider": type(provider).__name__,
                # provider 实例上的模型名（可得时）；图库类图源没有该属性记 None
                "model": getattr(provider, "_model", None),
            },
        )
        try:
            asset = await provider.fetch(request)
        except Exception as error:
            await finish_span(
                handle,
                "failed",
                error_code=codes.IMAGE_GEN_ERROR if name == "image.ai" else None,
                error_message=str(error) or error.__class__.__name__,
            )
            logger.warning("%s 图源异常，降级到下一级：%s", name, error)
            return None
        if asset is None:
            await finish_span(
                handle, "failed", error_code=codes.IMAGE_GEN_ERROR if name == "image.ai" else None
            )
            return None
        await finish_span(handle, "succeeded", model=getattr(provider, "_model", None))
        return asset


def create_image_pipeline(client: httpx.AsyncClient) -> ImagePipeline:
    settings = get_settings()
    fallback = UnsplashImageProvider(
        client=client,
        access_key=settings.unsplash_access_key,
        timeout_seconds=settings.image_timeout_seconds,
    )
    if settings.image_provider == "unsplash":
        # 省钱档（#32 成本约束）：不配付费生图主源，直接走免费图库；
        # 图库未配 key 时落到占位图。布局/导出类验收只看几何尺寸，
        # 不依赖真实图片内容。
        return ImagePipeline([fallback])
    if settings.image_provider == "bailian":
        primary: ImageProvider = BailianImageProvider(
            client=client,
            api_key=settings.image_api_key,
            model=settings.image_model,
            base_url=settings.image_base_url,
            workspace_id=settings.image_workspace_id,
            timeout_seconds=settings.image_timeout_seconds,
        )
    else:
        primary = GeneratedImageProvider(
            client=client,
            base_url=settings.image_base_url,
            api_key=settings.image_api_key,
            model=settings.image_model,
            timeout_seconds=settings.image_timeout_seconds,
        )

    return ImagePipeline([primary, fallback])
