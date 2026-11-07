"""Official DeepSeek API overflow provider for deepseek_chat and deepseek_reasoner."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from openhands_factory.agents.base import (
    AgentFailure,
    AgentFailureKind,
    AgentPhase,
    AgentProvider,
    AgentRequest,
    AgentResult,
    ProviderHealth,
    ProviderStatus,
)
from openhands_factory.config import ProviderConfig
from openhands_factory.exceptions import FactoryError, VerificationFailed

DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
MODEL_MAP: dict[str, str] = {
    "deepseek_chat": "deepseek-chat",
    "deepseek-chat": "deepseek-chat",
    "deepseek/deepseek-chat": "deepseek-chat",
    "deepseek_reasoner": "deepseek-reasoner",
    "deepseek-reasoner": "deepseek-reasoner",
    "deepseek/deepseek-reasoner": "deepseek-reasoner",
}


def _resolve_deepseek_api_key(explicit_key: str | None = None) -> str | None:
    if explicit_key:
        return explicit_key
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key
    secrets_file = Path("/home/dev/.zsh_secrets.env")
    if secrets_file.is_file():
        try:
            for raw in secrets_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("export DEEPSEEK_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except OSError:
            pass
    return None


class DeepSeekProvider(AgentProvider):
    """Official DeepSeek API Tier 3 provider (`deepseek_chat` and `deepseek_reasoner`)."""

    name = "deepseek"

    def __init__(
        self,
        config: ProviderConfig,
        api_key: str | None = None,
        api_base: str | None = None,
        runner: Callable[[AgentRequest], str | None] | None = None,
    ) -> None:
        self.config = config
        self.api_key = _resolve_deepseek_api_key(api_key)
        self.api_base = (
            api_base or os.environ.get("DEEPSEEK_API_BASE") or DEEPSEEK_API_BASE
        ).rstrip("/")
        self.runner = runner

    def _model_for_phase(self, phase: AgentPhase) -> tuple[str, str]:
        configured = (
            self.config.model_for_phase(phase.value) or self.config.model or "deepseek_chat"
        )
        api_model = MODEL_MAP.get(configured, configured.split("/", 1)[-1])
        return configured, api_model

    def health(self) -> ProviderHealth:
        now = datetime.now(UTC)
        if not self.config.enabled:
            return ProviderHealth(
                provider=self.name,
                status=ProviderStatus.DISABLED,
                checked_at=now,
                detail="provider disabled in configuration",
            )
        key = _resolve_deepseek_api_key(self.api_key)
        if not key:
            return ProviderHealth(
                provider=self.name,
                status=ProviderStatus.AUTH_REQUIRED,
                checked_at=now,
                detail="DEEPSEEK_API_KEY is missing",
            )
        return ProviderHealth(
            provider=self.name,
            status=ProviderStatus.HEALTHY,
            checked_at=now,
            detail="DeepSeek API Tier 3 fallback configured",
        )

    def supports(self, phase: AgentPhase) -> bool:
        return bool(self.config.enabled)

    def run(self, request: AgentRequest) -> AgentResult:
        started_at = datetime.now(UTC)
        configured_model, api_model = self._model_for_phase(request.phase)
        health = self.health()
        if health.status is not ProviderStatus.HEALTHY:
            kind = (
                AgentFailureKind.PROVIDER_AUTH
                if health.status is ProviderStatus.AUTH_REQUIRED
                else AgentFailureKind.PROVIDER_UNAVAILABLE
            )
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=1,
                summary=None,
                output_path=None,
                failure=AgentFailure(kind=kind, message=health.detail or "DeepSeek unavailable"),
                transport="deepseek-api",
                model=configured_model,
            )

        if self.runner is not None:
            try:
                if request.prepare_attempt:
                    request.prepare_attempt()
                summary = self.runner(request)
                if request.validate_output:
                    request.validate_output()
                return AgentResult(
                    provider=self.name,
                    phase=request.phase,
                    success=True,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    exit_code=0,
                    summary=summary,
                    output_path=None,
                    failure=None,
                    transport="deepseek-api",
                    model=configured_model,
                    captured_output_chars=len(summary or ""),
                    output_truncated=False,
                )
            except VerificationFailed as error:
                return AgentResult(
                    provider=self.name,
                    phase=request.phase,
                    success=False,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    exit_code=1,
                    summary=None,
                    output_path=None,
                    failure=AgentFailure(
                        kind=AgentFailureKind.INVALID_AGENT_OUTPUT, message=str(error)
                    ),
                    transport="deepseek-api",
                    model=configured_model,
                )
            except FactoryError as error:
                return AgentResult(
                    provider=self.name,
                    phase=request.phase,
                    success=False,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    exit_code=1,
                    summary=None,
                    output_path=None,
                    failure=AgentFailure(
                        kind=AgentFailureKind.PROVIDER_TRANSPORT, message=str(error)
                    ),
                    transport="deepseek-api",
                    model=configured_model,
                )

        if request.prepare_attempt:
            request.prepare_attempt()

        key = _resolve_deepseek_api_key(self.api_key)
        assert key is not None
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": request.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        messages.append({"role": "user", "content": request.prompt})

        timeout_secs = float(request.timeout_seconds or 120)
        url = f"{self.api_base}/chat/completions"
        payload: dict[str, Any] = {
            "model": api_model,
            "messages": messages,
        }
        if api_model != "deepseek-reasoner":
            payload["temperature"] = 0.1

        try:
            with httpx.Client(timeout=timeout_secs) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=124,
                summary=None,
                output_path=None,
                failure=AgentFailure(
                    kind=AgentFailureKind.PROVIDER_TIMEOUT,
                    message=f"DeepSeek API timed out after {timeout_secs}s: {exc}",
                    exit_code=124,
                ),
                transport="deepseek-api",
                model=configured_model,
            )
        except httpx.HTTPError as exc:
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=1,
                summary=None,
                output_path=None,
                failure=AgentFailure(
                    kind=AgentFailureKind.PROVIDER_TRANSPORT,
                    message=f"DeepSeek API transport error: {exc}",
                    exit_code=1,
                ),
                transport="deepseek-api",
                model=configured_model,
            )

        if response.status_code == 401 or response.status_code == 403:
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=response.status_code,
                summary=None,
                output_path=None,
                failure=AgentFailure(
                    kind=AgentFailureKind.PROVIDER_AUTH,
                    message=(
                        f"DeepSeek API auth failed ({response.status_code}): {response.text[:300]}"
                    ),
                    exit_code=response.status_code,
                ),
                transport="deepseek-api",
                model=configured_model,
            )
        if response.status_code == 402:
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=402,
                summary=None,
                output_path=None,
                failure=AgentFailure(
                    kind=AgentFailureKind.PROVIDER_QUOTA,
                    message=f"DeepSeek API insufficient balance (402): {response.text[:300]}",
                    exit_code=402,
                ),
                transport="deepseek-api",
                model=configured_model,
            )
        if response.status_code == 429:
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=429,
                summary=None,
                output_path=None,
                failure=AgentFailure(
                    kind=AgentFailureKind.PROVIDER_RATE_LIMIT,
                    message=f"DeepSeek API rate limited (429): {response.text[:300]}",
                    exit_code=429,
                ),
                transport="deepseek-api",
                model=configured_model,
            )
        if response.status_code >= 400:
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=response.status_code,
                summary=None,
                output_path=None,
                failure=AgentFailure(
                    kind=AgentFailureKind.PROVIDER_TRANSPORT,
                    message=f"DeepSeek API HTTP {response.status_code}: {response.text[:300]}",
                    exit_code=response.status_code,
                ),
                transport="deepseek-api",
                model=configured_model,
            )

        try:
            body = response.json()
            choices = body.get("choices", [])
            msg = choices[0].get("message", {}) if choices else {}
            content = msg.get("content") or msg.get("reasoning_content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return AgentResult(
                provider=self.name,
                phase=request.phase,
                success=False,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=1,
                summary=None,
                output_path=None,
                failure=AgentFailure(
                    kind=AgentFailureKind.INVALID_AGENT_OUTPUT,
                    message=f"Invalid DeepSeek JSON response: {exc}",
                ),
                transport="deepseek-api",
                model=configured_model,
            )

        if request.validate_output:
            try:
                request.validate_output()
            except VerificationFailed as error:
                return AgentResult(
                    provider=self.name,
                    phase=request.phase,
                    success=False,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    exit_code=1,
                    summary=content[:500] if content else None,
                    output_path=None,
                    failure=AgentFailure(
                        kind=AgentFailureKind.INVALID_AGENT_OUTPUT,
                        message=str(error),
                    ),
                    transport="deepseek-api",
                    model=configured_model,
                )

        return AgentResult(
            provider=self.name,
            phase=request.phase,
            success=True,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            exit_code=0,
            summary=content,
            output_path=None,
            failure=None,
            transport="deepseek-api",
            model=configured_model,
            captured_output_chars=len(content),
            output_truncated=False,
        )
