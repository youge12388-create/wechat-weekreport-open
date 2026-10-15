from __future__ import annotations

from dataclasses import dataclass


class TranslationError(Exception):
    retryable = False


class TranslationAuthError(TranslationError):
    retryable = False


class TranslationRateLimitError(TranslationError):
    retryable = True


@dataclass
class TranslationResult:
    content: str
    source_language: str | None
    chunks: int
