"""Credential-safe operator alerts."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from openhands_factory.config import FactoryConfig
from openhands_factory.generation import FACTORY_RUNTIME_VERSION
from openhands_factory.redaction import redact_text
from openhands_factory.state import atomic_write_json, read_json

LOGGER = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 1800
MAX_ALERT_ISSUES = 8
MAX_TELEGRAM_TEXT = 4000
MAX_DISCORD_TEXT = 2000


def _alert_category(message: str) -> str:
    """Derive a stable cooldown key without collapsing all factory alerts together."""
    parts = [part.strip() for part in message.split(":", 2)]
    if len(parts) >= 2 and parts[0] == "OpenHands factory alert":
        return f"{parts[0]}:{parts[1]}"
    return parts[0]


def _compact_issue_list(message: str) -> str:
    """Keep large health alerts actionable and safely inside Telegram's message limit."""
    match = re.search(r"issues=([0-9,]+)", message)
    if match is None:
        return message
    identifiers = [identifier for identifier in match.group(1).split(",") if identifier]
    if len(identifiers) <= MAX_ALERT_ISSUES:
        return message
    shown = ",".join(identifiers[:MAX_ALERT_ISSUES])
    remaining = len(identifiers) - MAX_ALERT_ISSUES
    replacement = f"issues={shown},... (+{remaining} more; total={len(identifiers)})"
    return f"{message[: match.start()]}{replacement}{message[match.end() :]}"


class AlertService:
    def __init__(
        self, config: FactoryConfig, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    ) -> None:
        self.config = config
        self.cooldown_seconds = cooldown_seconds
        self.state_path = config.state_dir / "alert_cooldowns.json"

    def send(self, message: str, *, category: str | None = None) -> bool:
        telegram = (
            self.config.telegram_bot_token is not None and self.config.telegram_chat_id is not None
        )
        discord = self.config.discord_webhook_url is not None
        if not telegram and not discord:
            return False

        resolved_category = category or _alert_category(message)
        now = datetime.now(UTC)
        state = read_json(self.state_path, {})
        entry = state.get(resolved_category)

        if entry is not None:
            last_sent = datetime.fromisoformat(entry["last_sent"])
            if (now - last_sent).total_seconds() < self.cooldown_seconds:
                entry["suppressed"] = entry.get("suppressed", 0) + 1
                state[resolved_category] = entry
                atomic_write_json(self.state_path, state)
                return False

        suppressed = entry.get("suppressed", 0) if entry is not None else 0
        text = _compact_issue_list(message)
        generation = self.config.factory_generation
        text = (
            f"{text}\n\nFactory generation: {generation}\n"
            f"Factory runtime: {FACTORY_RUNTIME_VERSION}"
        )
        if suppressed:
            minutes = max(1, self.cooldown_seconds // 60)
            text = (
                f"{text}\n\n({suppressed} similar alert"
                f"{'s' if suppressed != 1 else ''} suppressed in the last {minutes} minutes)"
            )

        text = redact_text(text)
        sent = False
        if telegram:
            sent = self._send_telegram(_truncate(text, MAX_TELEGRAM_TEXT)) or sent
        if discord:
            sent = self._send_discord(_truncate(text, MAX_DISCORD_TEXT)) or sent

        if sent:
            state[resolved_category] = {"last_sent": now.isoformat(), "suppressed": 0}
            atomic_write_json(self.state_path, state)
        return sent

    def _send_telegram(self, text: str) -> bool:
        token = self.config.telegram_bot_token
        chat = self.config.telegram_chat_id
        if token is None or chat is None:
            return False
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
                json={
                    "chat_id": chat.get_secret_value(),
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
            return response.is_success
        except Exception as error:
            LOGGER.warning("Telegram alert delivery failed: %s", error)
            return False

    def _send_discord(self, text: str) -> bool:
        webhook = self.config.discord_webhook_url
        if webhook is None:
            return False
        try:
            response = httpx.post(
                webhook.get_secret_value(),
                json={"content": text, "allowed_mentions": {"parse": []}},
                timeout=20,
            )
            return response.is_success
        except Exception as error:
            # httpx errors can include the URL, which embeds the webhook secret.
            LOGGER.warning("Discord alert delivery failed: %s", type(error).__name__)
            return False


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n...[alert truncated]"
    return f"{text[: limit - len(suffix)]}{suffix}"
