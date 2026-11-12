"""Provider selection from environment settings (cached)."""

from __future__ import annotations

import logging
from functools import lru_cache

from support_agent.core.config import get_settings
from support_agent.providers.anthropic import AnthropicLLMProvider
from support_agent.providers.base import EmbeddingProvider, LLMProvider
from support_agent.providers.fake import FakeEmbeddingProvider, FakeLLMProvider
from support_agent.providers.openai import OpenAIEmbeddingProvider, OpenAILLMProvider

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    settings = get_settings()
    match settings.llm_provider:
        case "fake":
            return FakeLLMProvider()
        case "openai":
            return OpenAILLMProvider(settings)
        case "anthropic":
            return AnthropicLLMProvider(settings)
        case unknown:  # pragma: no cover - Literal is validated by pydantic
            raise ValueError(f"unsupported LLM_PROVIDER: {unknown}")


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    match settings.embedding_provider:
        case "fake":
            return FakeEmbeddingProvider()
        case "openai":
            return OpenAIEmbeddingProvider(settings)
        case unknown:  # pragma: no cover
            raise ValueError(f"unsupported EMBEDDING_PROVIDER: {unknown}")


def reset_providers() -> None:
    """Test hook."""
    get_llm_provider.cache_clear()
    get_embedding_provider.cache_clear()
