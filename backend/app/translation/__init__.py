"""翻译 Provider 入口；与业务 LLM Provider 分离。"""

from app.translation.baidu import BaiduTranslationProvider
from app.translation.base import (
    TranslationAuthError,
    TranslationError,
    TranslationRateLimitError,
    TranslationResult,
)
from app.core.config import Settings
from app.services.runtime_config_service import runtime_settings

__all__ = [
    "BaiduTranslationProvider",
    "TranslationAuthError",
    "TranslationError",
    "TranslationRateLimitError",
    "TranslationResult",
    "get_translation_provider",
]


def get_translation_provider(settings: Settings | None = None) -> BaiduTranslationProvider:
    return BaiduTranslationProvider(settings or runtime_settings())
