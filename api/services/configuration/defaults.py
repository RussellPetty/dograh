from __future__ import annotations

"""Utilities for building default service configurations for a new user.

The defaults follow the same provider choices exposed by `/user/configurations/defaults`.
Values for `api_key` are pulled from environment variables named *{PROVIDER}_API_KEY*.

If an environment variable is missing, that particular provider configuration is
left as ``None``.
"""


import os
from typing import Optional

from api.schemas.user_configuration import UserConfiguration
from api.services.configuration.registry import (
    DeepgramSTTConfiguration,
    ElevenlabsTTSConfiguration,
    OpenAIEmbeddingsConfiguration,
    OpenAILLMService,
    ServiceProviders,
)

# Mapping of service to (provider enum, configuration class)
_DEFAULTS = {
    "llm": (ServiceProviders.OPENAI, OpenAILLMService),
    "tts": (ServiceProviders.ELEVENLABS, ElevenlabsTTSConfiguration),
    "stt": (ServiceProviders.DEEPGRAM, DeepgramSTTConfiguration),
    "embeddings": (ServiceProviders.OPENAI, OpenAIEmbeddingsConfiguration),
}

# Public mapping of service name -> default provider
DEFAULT_SERVICE_PROVIDERS = {
    field: provider for field, (provider, _) in _DEFAULTS.items()
}


def build_clerk_default_configuration() -> Optional[UserConfiguration]:
    """Default service config for a new "Viato Voice" (clerk-mode) organization.

    Seeds providers from Viato-supplied keys in the environment instead of the
    Dograh MPS service:

      - Voice: Grok Realtime (xAI) (``XAI_API_KEY``) — self-contained STT+LLM+TTS,
        set as ``realtime`` with ``is_realtime=True`` (default when the key is set)
      - LLM: OpenRouter   (``OPENROUTER_API_KEY``; model from ``VIATO_VOICE_LLM_MODEL``) — text/non-realtime paths
      - STT: Deepgram      (``DEEPGRAM_API_KEY``) — non-realtime paths

    Each leg is included only when its key is present, and the function returns
    ``None`` when no keys are set (so the user configures providers in the UI
    instead). Non-key fields fall back to each provider configuration's own
    defaults.
    """
    config: dict = {}

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        config["llm"] = {
            "provider": ServiceProviders.OPENROUTER.value,
            "api_key": [openrouter_key],
            "model": os.getenv("VIATO_VOICE_LLM_MODEL", "openai/gpt-4.1-mini"),
        }

    deepgram_key = os.getenv("DEEPGRAM_API_KEY")
    if deepgram_key:
        config["stt"] = {
            "provider": ServiceProviders.DEEPGRAM.value,
            "api_key": [deepgram_key],
        }

    # Voice: Grok Realtime (xAI) — a self-contained realtime model that does
    # STT + LLM + TTS in one turn. When ``XAI_API_KEY`` is present we make it the
    # default voice (``is_realtime=True``); the OpenRouter llm / Deepgram stt above
    # remain for non-realtime/text paths. Voices: Ara/Rex/Sal/Eve/Leo.
    xai_key = os.getenv("XAI_API_KEY")
    if xai_key:
        config["realtime"] = {
            "provider": ServiceProviders.GROK_REALTIME.value,
            "api_key": [xai_key],
            "model": os.getenv(
                "VIATO_VOICE_REALTIME_MODEL", "grok-voice-think-fast-1.0"
            ),
            "voice": os.getenv("VIATO_VOICE_GROK_VOICE", "Ara"),
        }
        config["is_realtime"] = True
    else:
        # Fallback voice when no realtime key is configured (left for self-hosters
        # who supply an ElevenLabs key instead).
        elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
        if elevenlabs_key:
            config["tts"] = {
                "provider": ServiceProviders.ELEVENLABS.value,
                "api_key": [elevenlabs_key],
            }

    if not config:
        return None
    return UserConfiguration(**config)


__all__ = [
    "DEFAULT_SERVICE_PROVIDERS",
    "build_clerk_default_configuration",
]
