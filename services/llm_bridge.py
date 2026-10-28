"""LLM provider bridge: the single place that routes chat to the right adapter.

Everything provider-specific that pipelines used to hardcode (which config
section, which API key, which adapter module, which error message) lives here.
Callers do::

    selection = select_llm(config)
    answer, tool_names = await complete(
        selection, messages=..., tools=..., user_request=...
    )

Adding a provider = add its adapter module + one entry in each map below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import (
    get_anthropic_api_key,
    get_gemini_api_key,
    get_inworld_api_key,
    get_openai_api_key,
)

LLM_PROVIDERS = ("inworld", "openai", "anthropic", "local", "gemini")
DEFAULT_MAX_TOKENS = 120

_LABELS = {
    "inworld": "inworld-router",
    "openai": "openai",
    "anthropic": "anthropic",
    "local": "local",
    "gemini": "gemini-live",
}
_KEY_NAMES = {
    "inworld": "INWORLD_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
_DEFAULT_MODELS = {
    "inworld": "deepseek-v4-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-opus-4-8",
    "local": "local-model",
    "gemini": "gemini-3.8-live",
}


@dataclass(frozen=True)
class LlmSelection:
    """The resolved choice for one conversation turn."""

    provider: str
    label: str
    model: str
    max_tokens: int
    api_key: str = ""  # cloud providers
    key_name: str = ""
    base_url: str = ""  # local provider
    temperature: float = 0.7


def active_provider(config: dict[str, Any]) -> str:
    provider = str(
        config.get("llm", {}).get("provider") or config.get("provider") or "inworld"
    ).lower()
    return provider if provider in LLM_PROVIDERS else "inworld"


def _get_api_key(provider: str) -> str:
    if provider == "gemini":
        return get_gemini_api_key()
    if provider == "openai":
        return get_openai_api_key()
    if provider == "anthropic":
        return get_anthropic_api_key()
    return get_inworld_api_key()


def select_llm(
    config: dict[str, Any], default_max_tokens: int = DEFAULT_MAX_TOKENS
) -> LlmSelection:
    """Resolve provider, model, token budget and credentials from the config."""
    provider = active_provider(config)
    section = config.get(provider) if isinstance(config.get(provider), dict) else {}
    configured_model = (
        section.get("live_model") if provider == "gemini" else section.get("model")
    )
    model = str(configured_model or _DEFAULT_MODELS[provider])
    max_tokens = int(section.get("max_tokens") or default_max_tokens)
    if provider == "local":
        return LlmSelection(
            provider=provider,
            label=_LABELS[provider],
            model=model,
            max_tokens=max_tokens,
            base_url=str(section.get("base_url") or "http://127.0.0.1:1234/v1").rstrip(
                "/"
            ),
            temperature=float(section.get("temperature") or 0.7),
        )
    return LlmSelection(
        provider=provider,
        label=_LABELS[provider],
        model=model,
        max_tokens=max_tokens,
        api_key=_get_api_key(provider),
        key_name=_KEY_NAMES[provider],
    )


async def complete(
    selection: LlmSelection,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    user_request: str,
) -> tuple[str, list[str]]:
    """Run one conversation turn on the selected provider's adapter."""
    if selection.provider == "local":
        from services import local_chat

        return await local_chat.complete(
            selection.base_url,
            model=selection.model,
            messages=messages,
            tools=tools,
            user_request=user_request,
            max_tokens=selection.max_tokens,
            temperature=selection.temperature,
        )
    if not selection.api_key:
        raise RuntimeError(
            f"{selection.key_name} is not configured. "
            "Save the key in the configuration UI."
        )
    if selection.provider == "gemini":
        raise RuntimeError(
            "Gemini uses the native /session transport, not text completion."
        )
    if selection.provider == "anthropic":
        from services import anthropic_chat as adapter
    elif selection.provider == "openai":
        from services import openai_chat as adapter
    else:
        from services import inworld_chat as adapter
    return await adapter.complete(
        selection.api_key,
        model=selection.model,
        messages=messages,
        tools=tools,
        user_request=user_request,
        max_tokens=selection.max_tokens,
    )


def friendly_error(selection: LlmSelection, exc: Exception) -> str:
    if selection.provider == "anthropic":
        from services import anthropic_chat

        return anthropic_chat.friendly_anthropic_error(exc)
    if selection.provider == "openai":
        from services import openai_chat

        return openai_chat.friendly_openai_error(exc)
    if selection.provider == "inworld":
        from services import inworld_chat

        return inworld_chat.friendly_inworld_error(exc, selection.api_key)
    return str(exc) or exc.__class__.__name__
