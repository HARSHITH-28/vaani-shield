"""Environment-based LLM provider selection."""

import os

from services.llm_adapters import GeminiLLMProvider, GroqLLMProvider
from services.llm_interfaces import LLMProviderError


def get_provider_settings():
    """Return safe provider metadata, never credential contents."""
    raw_provider = os.getenv("VAANISHIELD_LLM_PROVIDER")
    if raw_provider is None:
        provider = "groq"
    else:
        provider = raw_provider.strip().casefold()
        if provider == "":
            provider = "groq"
        elif provider in {"legacy", "rule_based", "none", "mock", "legacy_keyword_router"}:
            provider = "legacy_keyword_router"
        elif provider not in {"gemini", "groq"}:
            provider = "unsupported"

    credential_name = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}.get(provider)
    credential = os.getenv(credential_name, "").strip() if credential_name else ""
    credential_configured = (
        bool(credential) and not credential.casefold().startswith("replace_with")
        if credential_name else True
    )

    model_defaults = {
        "gemini": "gemini-2.5-flash",
        "groq": "openai/gpt-oss-20b",
    }

    if provider == "gemini":
        gemini_env = os.getenv("GEMINI_MODEL", "").strip()
        general_env = os.getenv("VAANISHIELD_LLM_MODEL", "").strip()
        if gemini_env:
            model = gemini_env
        elif general_env and general_env.startswith("gemini-"):
            model = general_env
        elif general_env and general_env not in {"openai/gpt-oss-20b", "keyword-router"}:
            model = general_env
        else:
            model = model_defaults["gemini"]

        if not (
            model.startswith("gemini-") and len(model) <= 100
            and all(character.isalnum() or character in "-._" for character in model)
        ):
            model = "invalid-model-identifier"
    elif provider == "groq":
        model = os.getenv("VAANISHIELD_LLM_MODEL", model_defaults["groq"]).strip()
        if not (
            len(model) <= 100 and all(character.isalnum() or character in "-._/" for character in model)
        ):
            model = "invalid-model-identifier"
    else:
        model = "keyword-router"

    return {
        "provider": provider,
        "model": model,
        "credential_configured": credential_configured if credential_name else True,
    }


def create_llm_provider(settings=None):
    """Return configured provider; no configuration explicitly selects rules."""
    settings = settings or get_provider_settings()
    provider = settings["provider"]
    if provider == "legacy_keyword_router":
        return None
    if provider not in {"gemini", "groq"}:
        raise LLMProviderError("The configured assistant language provider is unsupported.")
    credential_name = "GEMINI_API_KEY" if provider == "gemini" else "GROQ_API_KEY"
    api_key = os.getenv(credential_name, "").strip()
    model = settings["model"]
    if not settings["credential_configured"]:
        raise LLMProviderError(f"{provider.title()} is selected but its API key is not configured.")
    if model == "invalid-model-identifier":
        raise LLMProviderError("VAANISHIELD_LLM_MODEL must be valid for the selected provider.")
    return GeminiLLMProvider(api_key, model) if provider == "gemini" else GroqLLMProvider(api_key, model)
