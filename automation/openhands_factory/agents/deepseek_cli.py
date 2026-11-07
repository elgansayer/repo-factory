"""Official DeepSeek API through the existing supervised OpenCode CLI harness."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from openhands_factory.agents.base import ProviderHealth, ProviderStatus
from openhands_factory.agents.deepseek import MODEL_MAP, _resolve_deepseek_api_key
from openhands_factory.agents.opencode import OpenCodeProvider
from openhands_factory.config import ProviderConfig


class DeepSeekCLIProvider(OpenCodeProvider):
    name = "deepseek"

    def __init__(self, config: ProviderConfig) -> None:
        def model(value: str) -> str:
            return "factory-deepseek/" + MODEL_MAP.get(value, value.split("/")[-1])

        super().__init__(
            enabled=config.enabled,
            command="opencode",
            model=model(config.model or "deepseek_chat"),
            phase_models={key: model(value) for key, value in config.phase_models.items()},
            max_turns=config.max_turns,
            credential_paths=(".config/opencode", ".local/share/opencode"),
            runtime_paths=tuple(
                dict.fromkeys([*config.runtime_paths, ".opencode", ".local/lib", ".agents"])
            ),
        )

    def health(self) -> ProviderHealth:
        status = ProviderStatus.HEALTHY
        detail = "Official DeepSeek API key configured; supervised OpenCode transport"
        if not self.enabled:
            status, detail = ProviderStatus.DISABLED, "Disabled"
        elif not _resolve_deepseek_api_key():
            status, detail = ProviderStatus.AUTH_REQUIRED, "DEEPSEEK_API_KEY missing"
        elif self._available():
            status, detail = ProviderStatus.UNAVAILABLE, self._available() or "CLI unavailable"
        return ProviderHealth(self.name, status, datetime.now(UTC), detail=detail)

    def preflight_health(self, cwd: Path) -> ProviderHealth | None:
        health = self.health()
        return None if health.status is ProviderStatus.HEALTHY else health

    def extra_environment(self) -> dict[str, str]:
        settings = json.loads(super().extra_environment()["OPENCODE_CONFIG_CONTENT"])
        settings["provider"] = {
            "factory-deepseek": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Official DeepSeek API",
                "options": {
                    "baseURL": "https://api.deepseek.com/v1",
                    "apiKey": "{env:DEEPSEEK_API_KEY}",
                },
                "models": {
                    "deepseek-chat": {"name": "DeepSeek Chat"},
                    "deepseek-reasoner": {"name": "DeepSeek Reasoner"},
                },
            }
        }
        return {
            "OPENCODE_CONFIG_CONTENT": json.dumps(settings),
            "DEEPSEEK_API_KEY": _resolve_deepseek_api_key() or "",
        }
