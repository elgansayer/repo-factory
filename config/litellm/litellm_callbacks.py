"""LiteLLM Custom Providers, Quota/Rate-Limit Failover, Prompt Caching, and 10-Turn History Compression.

Supports:
- Phase 2: Toggleable Experimental Model Routing
  - Tier 1: Google Antigravity (`agy` / Gemini Pro) with 5-hour rolling window & weekly quota detection
  - Tier 2: OpenCode Go Plus (`deepseek-v4.1-flash`, `deepseek-v4-pro`, `qwen3.8-max`, `qwen3.8-flash`, `qwen3.7-plus`)
  - Tier 3: Official DeepSeek API (`deepseek_chat` -> `deepseek/deepseek-chat`, `deepseek_reasoner` -> `deepseek/deepseek-reasoner`)
- Phase 4.3: Prompt Caching (`cache_control` headers on system prompts, repository maps, and MCP tool definitions)
- Phase 4.4: History Truncation and Context Compression every 10 turns
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import litellm
from litellm import CustomLLM, ModelResponse
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.utils import Choices, Message, Usage

LOGGER = logging.getLogger("factory.litellm")

# Quota / Rate-limit markers including 5-hour rolling window and weekly quota limits
TIER1_QUOTA_MARKERS: tuple[str, ...] = (
    "5-hour",
    "5 hour",
    "five-hour",
    "five hour",
    "rolling window",
    "5h window",
    "weekly quota",
    "weekly limit",
    "weekly allowance",
    "weekly usage limit",
    "quota exceeded",
    "quota exhausted",
    "usage limit",
    "rate limit",
    "ratelimit",
    "resource_exhausted",
    "too many requests",
    "http 429",
    "status code 429",
)

_MODIFIED_FILE_RE = re.compile(
    r"(?:[\w./-]+\.(?:py|ts|tsx|js|jsx|go|cpp|cc|c|h|hpp|gd|tscn|tres|rs|json|yaml|yml|md|sh))\b"
)
_COMPILER_OUTPUT_RE = re.compile(
    r"(?:error:|fatal:|failed|FAIL|panic:|assertion|undefined reference|exception|Traceback)",
    re.IGNORECASE,
)


def _bootstrap_credentials() -> None:
    """Load OpenCode Go Plus and zsh secret credentials into process environment if absent."""
    auth_path = Path("/home/dev/.local/share/opencode/auth.json")
    if auth_path.is_file() and not os.environ.get("OPENCODE_GO_API_KEY"):
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
            opencode_go = data.get("opencode-go", {})
            if isinstance(opencode_go, dict) and opencode_go.get("key"):
                os.environ["OPENCODE_GO_API_KEY"] = str(opencode_go["key"])
        except Exception:
            pass

    secrets_path = Path("/home/dev/.zsh_secrets.env")
    if secrets_path.is_file():
        try:
            for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or not line.startswith("export "):
                    continue
                assignment = line[len("export ") :].strip()
                if "=" not in assignment:
                    continue
                key, _, value = assignment.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and not os.environ.get(key):
                    os.environ[key] = value
        except Exception:
            pass


_bootstrap_credentials()


def dryrun_echo(provider: str, model: str, prompt: str):
    """Zero-cost routing validation: FACTORY_DRYRUN_ECHO=1 answers locally instead of spending quota."""
    if os.environ.get("FACTORY_DRYRUN_ECHO") == "1":
        return AntigravityCustomLLM._build_response(model=f"{provider}/{model.split('/', 1)[-1]}", content=f"DRYRUN_OK {provider}", prompt=prompt)
    return None


def is_tier1_quota_or_rate_limit(text: str) -> tuple[bool, str | None]:
    """Detect Antigravity 5-hour rolling window, weekly quota, or HTTP 429 rate limits."""
    lowered = text.lower()
    for marker in TIER1_QUOTA_MARKERS:
        if marker in lowered:
            return True, marker
    return False, None


def compress_conversation_history(
    messages: list[dict[str, Any]],
    *,
    turn_threshold: int = 10,
    keep_recent_messages: int = 4,
) -> list[dict[str, Any]]:
    """Summarize and prune conversation history every `turn_threshold` turns.

    Retains:
    1. All system messages (with prompt caching preserved)
    2. The architectural goal (initial user request)
    3. Deduplicated modified file paths referenced across pruned turns
    4. Latest compiler/test error output from pruned turns
    5. The most recent `keep_recent_messages` messages for active turn continuity
    """
    if not isinstance(messages, list) or len(messages) <= keep_recent_messages + 1:
        return messages

    system_messages: list[dict[str, Any]] = []
    non_system_messages: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_messages.append(msg)
        elif isinstance(msg, dict):
            non_system_messages.append(msg)

    # Count user/assistant turns; 1 turn = 1 assistant response (or 2 messages)
    assistant_turns = sum(1 for m in non_system_messages if m.get("role") == "assistant")
    estimated_turns = max(assistant_turns, len(non_system_messages) // 2)
    if estimated_turns < turn_threshold:
        return messages

    # Split into prunable prefix and recent tail
    split = len(non_system_messages) - keep_recent_messages
    # Never separate a tool result from its assistant tool_call (including parallel calls).
    while split > 0 and non_system_messages[split].get("role") == "tool":
        split -= 1
    prunable = non_system_messages[:split]
    recent = non_system_messages[split:]
    if not prunable:
        return messages

    architectural_goal = ""
    modified_files: list[str] = []
    seen_files: set[str] = set()
    latest_compiler_output = ""

    for idx, msg in enumerate(prunable):
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            text = "\n".join(text_parts)
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)

        if idx == 0 and msg.get("role") == "user":
            previous_goal = re.search(r"^Architectural Goal: (.*)$", text, re.MULTILINE)
            architectural_goal = previous_goal.group(1) if previous_goal else text[:600].strip()

        for match in _MODIFIED_FILE_RE.findall(text):
            if match not in seen_files and len(modified_files) < 40:
                seen_files.add(match)
                modified_files.append(match)

        if _COMPILER_OUTPUT_RE.search(text):
            matching_lines = [
                line.strip()
                for line in text.splitlines()
                if _COMPILER_OUTPUT_RE.search(line)
            ]
            if matching_lines:
                latest_compiler_output = "\n".join(matching_lines[-10:])[:500]

    summary_lines = [
        f"[COMPRESSED_HISTORY_SUMMARY: Pruned {len(prunable)} messages after {estimated_turns} turns]",
        f"Architectural Goal: {architectural_goal or 'See system prompt / task instructions.'}",
        f"Modified/Referenced Files: {', '.join(modified_files) if modified_files else 'None recorded'}",
        f"Latest Compiler/Test Output: {latest_compiler_output or 'No compiler/test failures in pruned turns.'}",
    ]
    summary_message: dict[str, Any] = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "\n".join(summary_lines),
            }
        ],
    }
    return [*system_messages, summary_message, *recent]


def inject_prompt_cache_headers(
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """Attach `cache_control: {"type": "ephemeral"}` to system prompts, repo maps, and MCP tool definitions."""
    updated_messages = messages
    breakpoints = 0
    if isinstance(messages, list):
        updated_messages = []
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                updated_messages.append(msg)
                continue
            role = msg.get("role")
            content = msg.get("content")
            should_cache = role == "system" or (
                idx == 0
                and role == "user"
                and isinstance(content, str)
                and ("<repository_map>" in content or "code_map" in content or len(content) > 1024)
            )
            if should_cache and breakpoints < 2:
                breakpoints += 1
                msg_copy = copy.deepcopy(msg)
                if isinstance(content, str):
                    msg_copy["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                elif isinstance(content, list) and content:
                    last_block = msg_copy["content"][-1]
                    if isinstance(last_block, dict):
                        last_block["cache_control"] = {"type": "ephemeral"}
                updated_messages.append(msg_copy)
            else:
                updated_messages.append(msg)

    updated_tools = tools
    if isinstance(tools, list) and tools:
        updated_tools = copy.deepcopy(tools)
        last_tool = updated_tools[-1]
        if isinstance(last_tool, dict):
            last_tool["cache_control"] = {"type": "ephemeral"}

    return updated_messages, updated_tools


class TokenEfficiencyCallback(CustomLogger):
    """LiteLLM callback enforcing Prompt Caching (4.3) and 10-Turn History Compression (4.4)."""

    def log_pre_api_call(self, model: str, messages: list[Any], kwargs: dict[str, Any]) -> None:
        self._apply_optimizations(messages, kwargs, model)

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return data
        # Cache only explicit deterministic checks; tool conversations must never replay.
        metadata = data.get("metadata") or {}
        eligible = metadata.get("factory_cache_kind") in {"ast", "check", "embedding"}
        messages = data.get("messages") or []
        multiturn = any(m.get("role") in {"assistant", "tool"} for m in messages)
        if not eligible or multiturn or data.get("tools"):
            data["cache"] = {"no-cache": True, "no-store": True}
        messages = data.get("messages")
        if isinstance(messages, list):
            compressed = compress_conversation_history(messages, turn_threshold=10)
            cached_msgs, cached_tools = compressed, data.get("tools")
            if str(data.get("model", "")).startswith("anthropic/"):
                cached_msgs, cached_tools = inject_prompt_cache_headers(compressed, cached_tools)
            data["messages"] = cached_msgs
            if cached_tools is not None:
                data["tools"] = cached_tools
        return data

    def _apply_optimizations(self, messages: list[Any], kwargs: dict[str, Any], model: str = "") -> None:
        if not isinstance(messages, list):
            return
        compressed = compress_conversation_history(messages, turn_threshold=10)
        tools = kwargs.get("tools") or kwargs.get("optional_params", {}).get("tools")
        cached_msgs, cached_tools = compressed, tools
        if model.startswith("anthropic/"):
            cached_msgs, cached_tools = inject_prompt_cache_headers(compressed, tools)
        if cached_msgs is not None:
            messages[:] = cached_msgs
        if cached_tools is not None:
            if "tools" in kwargs:
                kwargs["tools"] = cached_tools
            if isinstance(kwargs.get("optional_params"), dict) and "tools" in kwargs["optional_params"]:
                kwargs["optional_params"]["tools"] = cached_tools


class AntigravityCustomLLM(CustomLLM):
    """Tier 1 CustomLLM bridging LiteLLM to Google Antigravity (`agy`) / Gemini Pro.

    Detects 5-hour rolling window and weekly quota exhaustion and raises HTTP 429
    RateLimitError so LiteLLM Router immediately fails over to Tier 2 (OpenCode Go Plus).
    """

    def completion(
        self,
        model: str,
        messages: list[Any],
        api_base: str | None = None,
        custom_prompt_dict: dict[str, Any] | None = None,
        model_response: ModelResponse | None = None,
        print_verbose: Any = None,
        encoding: Any = None,
        api_key: str | None = None,
        logging_obj: Any = None,
        optional_params: dict[str, Any] | None = None,
        acompletion: Any = None,
        litellm_params: dict[str, Any] | None = None,
        logger_fn: Any = None,
        headers: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
        client: Any = None,
    ) -> ModelResponse:
        optional_params = optional_params or {}
        headers = headers or {}
        prompt_text = self._flatten_messages(messages)
        if optional_params.get("tools"):
            raise litellm.ServiceUnavailableError(message="agy proxy bridge is text-only; native factory CLI supports tools", model=model, llm_provider="agy")
        tier_gate("agy", model)

        # Support deterministic dry-run testing of Tier 1 5-hour/weekly quota failover
        if (
            os.environ.get("FACTORY_SIMULATE_TIER1_QUOTA") == "1"
            or headers.get("x-simulate-tier1-quota") == "1"
            or "[SIMULATE_TIER1_429]" in prompt_text
            or "[SIMULATE_5H_QUOTA]" in prompt_text
            or "[SIMULATE_WEEKLY_QUOTA]" in prompt_text
        ):
            raise litellm.RateLimitError(
                message=(
                    "Antigravity (agy) quota exhausted: 5-hour rolling window or weekly quota "
                    "limit reached (HTTP 429). Failing over immediately to Tier 2."
                ),
                llm_provider="agy",
                model=model,
            )

        if (echo := dryrun_echo("agy", model, prompt_text)) is not None:
            return echo

        # If GEMINI_API_KEY is available and direct Gemini API is preferred or fast-path requested
        gemini_key = (api_key or os.environ.get("GEMINI_API_KEY")) if os.environ.get("FACTORY_ALLOW_GEMINI_API") == "1" else None
        target_model = model.split("/", 1)[-1] if "/" in model else model

        # Try invoking `agy` CLI or Gemini API if configured
        agy_bin = os.environ.get("AGY_BIN", "/home/dev/.local/bin/agy")
        timeout_secs = float(timeout) if isinstance(timeout, (int, float)) else 60.0

        if Path(agy_bin).is_file() and os.environ.get("AGY_USE_CLI", "1") == "1":
            cli_model = (
                target_model
                if target_model in {"gemini-4-argon-high", "gemini-3.8-flash-high", "gemini-3.8-flash-medium"}
                else "gemini-4-argon-high"
            )
            try:
                proc = subprocess.run(
                    [agy_bin, "--sandbox", "--mode", "plan", "--print", "--model", cli_model, prompt_text],
                    capture_output=True,
                    text=True,
                    timeout=min(timeout_secs, 90.0),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise litellm.Timeout(
                    message=f"Antigravity CLI timed out after {timeout_secs}s",
                    model=model,
                    llm_provider="agy",
                ) from exc

            combined = f"{proc.stdout}\n{proc.stderr}".strip()
            hit_quota, marker = is_tier1_quota_or_rate_limit(combined)
            if hit_quota:
                mark_tier_exhausted("agy", combined)
                raise litellm.RateLimitError(
                    message=f"Antigravity (agy) Tier 1 quota limit ({marker}): {combined[:300]}",
                    llm_provider="agy",
                    model=model,
                )
            if proc.returncode == 0 and proc.stdout.strip():
                return self._build_response(model=model, content=proc.stdout.strip(), prompt=prompt_text)

        # Fallback/bridge via Gemini API if GEMINI_API_KEY is present
        if gemini_key:
            gemini_model = "gemini-2.5-pro" if "pro" in target_model or "argon" in target_model else "gemini-2.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            }
            try:
                with httpx.Client(timeout=min(timeout_secs, 45.0)) as http_client:
                    resp = http_client.post(url, json=payload)
            except httpx.TimeoutException as exc:
                raise litellm.Timeout(
                    message=f"Gemini Pro request timed out: {exc}",
                    model=model,
                    llm_provider="agy",
                ) from exc
            except Exception as exc:
                raise litellm.APIConnectionError(
                    message=f"Gemini Pro connection error: {exc}",
                    model=model,
                    llm_provider="agy",
                ) from exc

            if resp.status_code == 429 or is_tier1_quota_or_rate_limit(resp.text)[0]:
                mark_tier_exhausted("agy", resp.text)
                raise litellm.RateLimitError(
                    message=f"Antigravity/Gemini Pro Tier 1 quota or rate limit (HTTP {resp.status_code}): {resp.text[:300]}",
                    llm_provider="agy",
                    model=model,
                )
            if resp.status_code >= 400:
                raise litellm.ServiceUnavailableError(
                    message=f"Antigravity/Gemini Pro HTTP {resp.status_code}: {resp.text[:300]}",
                    llm_provider="agy",
                    model=model,
                )
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                text_out = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                if text_out:
                    return self._build_response(model=model, content=text_out, prompt=prompt_text)

        raise litellm.RateLimitError(
            message="Antigravity (agy) Tier 1 interactive session unavailable or quota reached; failing over to Tier 2",
            llm_provider="agy",
            model=model,
        )

    async def acompletion(
        self,
        model: str,
        messages: list[Any],
        api_base: str | None = None,
        custom_prompt_dict: dict[str, Any] | None = None,
        model_response: ModelResponse | None = None,
        print_verbose: Any = None,
        encoding: Any = None,
        api_key: str | None = None,
        logging_obj: Any = None,
        optional_params: dict[str, Any] | None = None,
        acompletion: Any = None,
        litellm_params: dict[str, Any] | None = None,
        logger_fn: Any = None,
        headers: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
        client: Any = None,
    ) -> ModelResponse:
        import asyncio

        return await asyncio.to_thread(
            self.completion,
            model=model,
            messages=messages,
            api_base=api_base,
            custom_prompt_dict=custom_prompt_dict,
            model_response=model_response,
            print_verbose=print_verbose,
            encoding=encoding,
            api_key=api_key,
            logging_obj=logging_obj,
            optional_params=optional_params,
            acompletion=acompletion,
            litellm_params=litellm_params,
            logger_fn=logger_fn,
            headers=headers,
            timeout=timeout,
            client=client,
        )

    @staticmethod
    def _flatten_messages(messages: list[Any]) -> str:
        chunks: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                text = "\n".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and "text" in p
                )
            else:
                text = str(content)
            chunks.append(f"[{role.upper()}]\n{text}")
        return "\n\n".join(chunks)

    @staticmethod
    def _build_response(*, model: str, content: str, prompt: str) -> ModelResponse:
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(content) // 4)
        resp = ModelResponse()
        resp.model = model if "/" in model else f"agy/{model}"
        resp.choices = [
            Choices(
                finish_reason="stop",
                index=0,
                message=Message(content=content, role="assistant"),
            )
        ]
        resp.usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
        return resp


class OpenCodeGoCustomLLM(CustomLLM):
    """Tier 2 CustomLLM routing to OpenCode Go Plus subscription endpoints (DeepSeek V4.1 Flash / V4 Pro & Qwen 3.8).

    Supports both OpenAI-compatible (`/inference/go/openai/v1`) for DeepSeek models
    and Anthropic-compatible (`/inference/go/anthropic/v1`) for Qwen coding models.
    Raises HTTP 429 `litellm.RateLimitError` or `litellm.Timeout` on quota/timeout so
    LiteLLM seamlessly switches to Tier 3 (`deepseek_chat` / `deepseek_reasoner`).
    """

    OPENAI_COMPAT_URL = "https://opencode.ai/inference/go/openai/v1/chat/completions"
    ANTHROPIC_COMPAT_URL = "https://opencode.ai/inference/go/anthropic/v1/messages"

    def completion(self, model, messages, api_base=None, custom_prompt_dict=None,
                   model_response=None, print_verbose=None, encoding=None, api_key=None,
                   logging_obj=None, optional_params=None, acompletion=None,
                   litellm_params=None, logger_fn=None, headers=None, timeout=None, client=None):
        """Delegate protocol translation to LiteLLM so tool calls and usage survive."""
        optional_params = optional_params or {}
        prompt = AntigravityCustomLLM._flatten_messages(messages)
        tier_gate("opencode-go", model)
        if os.environ.get("FACTORY_SIMULATE_TIER2_QUOTA") == "1" or "[SIMULATE_TIER2_429]" in prompt:
            raise litellm.RateLimitError(message="simulated 429", llm_provider="opencode-go", model=model)
        if os.environ.get("FACTORY_SIMULATE_TIER2_TIMEOUT") == "1" or "[SIMULATE_TIER2_TIMEOUT]" in prompt:
            raise litellm.Timeout(message="simulated timeout", llm_provider="opencode-go", model=model)
        if (echo := dryrun_echo("opencode-go", model, prompt)) is not None:
            return echo
        key = api_key or os.environ.get("OPENCODE_GO_API_KEY")
        if not key:
            raise litellm.AuthenticationError(message="OpenCode Go key missing", llm_provider="opencode-go", model=model)
        model_id = model.split("/", 1)[-1]
        anthropic = model_id.startswith(("qwen", "minimax"))
        base = self.ANTHROPIC_COMPAT_URL.removesuffix("/messages") if anthropic else self.OPENAI_COMPAT_URL.removesuffix("/chat/completions")
        params = {k: v for k, v in optional_params.items()
                  if k in {"tools", "tool_choice", "temperature", "max_tokens", "top_p", "stop", "response_format"}}
        try:
            result = litellm.completion(
                model=("anthropic/" if anthropic else "openai/") + model_id,
                api_base=base, api_key=key, messages=messages,
                timeout=timeout or 60, num_retries=0, caching=False, **params)
            result.model = "opencode-go/" + model_id
            return result
        except litellm.RateLimitError as exc:
            mark_tier_exhausted("opencode-go", str(exc))
            raise

    async def acompletion(
        self,
        model: str,
        messages: list[Any],
        api_base: str | None = None,
        custom_prompt_dict: dict[str, Any] | None = None,
        model_response: ModelResponse | None = None,
        print_verbose: Any = None,
        encoding: Any = None,
        api_key: str | None = None,
        logging_obj: Any = None,
        optional_params: dict[str, Any] | None = None,
        acompletion: Any = None,
        litellm_params: dict[str, Any] | None = None,
        logger_fn: Any = None,
        headers: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
        client: Any = None,
    ) -> ModelResponse:
        import asyncio

        return await asyncio.to_thread(
            self.completion,
            model=model,
            messages=messages,
            api_base=api_base,
            custom_prompt_dict=custom_prompt_dict,
            model_response=model_response,
            print_verbose=print_verbose,
            encoding=encoding,
            api_key=api_key,
            logging_obj=logging_obj,
            optional_params=optional_params,
            acompletion=acompletion,
            litellm_params=litellm_params,
            logger_fn=logger_fn,
            headers=headers,
            timeout=timeout,
            client=client,
        )


# ---------------------------------------------------------------------------
# Time-boxed subscription tiers (Tier 0A OpenAI Pro, Tier 0B Claude Max)
# ---------------------------------------------------------------------------
import tempfile
from datetime import datetime, timedelta, timezone

TIER_STATE_PATH = Path(os.environ.get("FACTORY_TIER_STATE", "/home/dev/factory-ops/litellm/tier_state.json"))
TIER_WINDOWS: dict[str, datetime] = {
    "codex-cli": datetime(2026, 11, 17, tzinfo=timezone.utc),  # inclusive November 16 UTC
    "claude-cli": datetime(2026, 12, 2, tzinfo=timezone.utc),  # inclusive December 1 UTC
}
# Antigravity: 5h rolling window / weekly ceiling cooldowns once detected.
QUOTA_COOLDOWN_SECONDS = {"agy": 5 * 3600, "codex-cli": 5 * 3600, "claude-cli": 5 * 3600}
WEEKLY_MARKERS = ("weekly", "week")


def _now() -> datetime:
    override = os.environ.get("FACTORY_NOW")  # ISO timestamp, used for dry-run date tests
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def _load_state() -> dict[str, float]:
    try:
        return {k: float(v) for k, v in json.loads(TIER_STATE_PATH.read_text()).items()}
    except Exception:
        return {}


def mark_tier_exhausted(provider: str, detail: str) -> None:
    """Persist a quota cooldown so an exhausted tier is not re-probed every request."""
    seconds = QUOTA_COOLDOWN_SECONDS.get(provider, 3600)
    if any(m in detail.lower() for m in WEEKLY_MARKERS):
        seconds = 7 * 24 * 3600
    state = _load_state()
    state[provider] = _now().timestamp() + seconds
    try:
        TIER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TIER_STATE_PATH.write_text(json.dumps(state))
    except OSError:
        pass


def tier_gate(provider: str, model: str) -> None:
    """Raise RateLimitError (=> immediate failover) if the tier is expired or cooling down."""
    end = TIER_WINDOWS.get(provider)
    if end is not None and _now() >= end:
        raise litellm.RateLimitError(
            message=f"{provider} tier expired at {end.isoformat()}; failing over",
            llm_provider=provider,
            model=model,
        )
    until = _load_state().get(provider)
    if until is not None and _now().timestamp() < until:
        raise litellm.RateLimitError(
            message=f"{provider} quota cooldown active until {datetime.fromtimestamp(until, timezone.utc).isoformat()}",
            llm_provider=provider,
            model=model,
        )


class _SubscriptionCLILLM(CustomLLM):
    provider = ""
    sim_env = ""
    sim_marker = ""

    def build_command(self, model_id: str, out_file: str) -> tuple[list[str], str | None]:
        raise NotImplementedError

    def completion(self, model, messages, api_base=None, custom_prompt_dict=None, model_response=None,
                   print_verbose=None, encoding=None, api_key=None, logging_obj=None, optional_params=None,
                   acompletion=None, litellm_params=None, logger_fn=None, headers=None, timeout=None,
                   client=None):
        headers = headers or {}
        prompt_text = AntigravityCustomLLM._flatten_messages(messages)
        if (optional_params or {}).get("tools"):
            raise litellm.ServiceUnavailableError(message="Subscription CLI proxy is text-only; use native factory CLI for tools", model=model, llm_provider=self.provider)
        tier_gate(self.provider, model)
        if (
            os.environ.get(self.sim_env) == "1"
            or headers.get(f"x-simulate-{self.provider}-quota") == "1"
            or self.sim_marker in prompt_text
        ):
            mark_tier_exhausted(self.provider, "simulated")
            raise litellm.RateLimitError(
                message=f"{self.provider} quota exhausted (simulated HTTP 429)",
                llm_provider=self.provider,
                model=model,
            )
        if (echo := dryrun_echo(self.provider, model, prompt_text)) is not None:
            return echo
        model_id = model.split("/", 1)[-1]
        timeout_secs = float(timeout) if isinstance(timeout, (int, float)) else 300.0
        with tempfile.TemporaryDirectory(prefix=f"{self.provider}-") as workdir:
            out_file = str(Path(workdir) / "last.txt")
            cmd, _ = self.build_command(model_id, out_file)
            try:
                proc = subprocess.run(cmd, input=prompt_text, capture_output=True, text=True,
                                      timeout=timeout_secs, check=False, cwd=workdir)
            except subprocess.TimeoutExpired as exc:
                raise litellm.Timeout(message=f"{self.provider} timed out", model=model,
                                      llm_provider=self.provider) from exc
            except FileNotFoundError as exc:
                raise litellm.ServiceUnavailableError(message=f"{self.provider} CLI missing", llm_provider=self.provider,
                                                      model=model) from exc
            combined = f"{proc.stdout}\n{proc.stderr}"
            hit, marker = is_tier1_quota_or_rate_limit(combined)
            if proc.returncode != 0 and hit:
                mark_tier_exhausted(self.provider, combined)
                raise litellm.RateLimitError(message=f"{self.provider} quota/rate limit ({marker})",
                                             llm_provider=self.provider, model=model)
            if proc.returncode != 0:
                raise litellm.ServiceUnavailableError(message=f"{self.provider} exit {proc.returncode}: {combined[-300:]}",
                                                      llm_provider=self.provider, model=model)
            out_path = Path(out_file)
            content = out_path.read_text().strip() if out_path.is_file() else proc.stdout.strip()
        return AntigravityCustomLLM._build_response(model=f"{self.provider}/{model_id}", content=content, prompt=prompt_text)

    async def acompletion(self, *args, **kwargs):
        import asyncio
        return await asyncio.to_thread(self.completion, *args, **kwargs)


class CodexCLICustomLLM(_SubscriptionCLILLM):
    """Tier 0A: OpenAI Pro via the logged-in `codex` CLI (subscription, not API key). Active until 2026-11-16."""

    provider = "codex-cli"
    sim_env = "FACTORY_SIMULATE_TIER0A_QUOTA"
    sim_marker = "[SIMULATE_TIER0A_429]"

    def build_command(self, model_id, out_file):
        return ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                "-m", model_id, "-o", out_file, "-"], None


class ClaudeCLICustomLLM(_SubscriptionCLILLM):
    """Tier 0B: Claude Max via the logged-in `claude` CLI. Active until 2026-12-01."""

    provider = "claude-cli"
    sim_env = "FACTORY_SIMULATE_TIER0B_QUOTA"
    sim_marker = "[SIMULATE_TIER0B_429]"

    def build_command(self, model_id, out_file):
        return ["claude", "-p", "--model", model_id, "--output-format", "text"], None


codex_handler = CodexCLICustomLLM()
claude_handler = ClaudeCLICustomLLM()

agy_handler = AntigravityCustomLLM()
opencode_go_handler = OpenCodeGoCustomLLM()
token_efficiency_callback = TokenEfficiencyCallback()

litellm.custom_provider_map = [
    {"provider": "agy", "custom_handler": agy_handler},
    {"provider": "opencode-go", "custom_handler": opencode_go_handler},
    {"provider": "codex-cli", "custom_handler": codex_handler},
    {"provider": "claude-cli", "custom_handler": claude_handler},
]
if token_efficiency_callback not in litellm.callbacks:
    litellm.callbacks.append(token_efficiency_callback)

from litellm.utils import custom_llm_setup
custom_llm_setup()
