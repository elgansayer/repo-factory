"""Minimal Discord command bot for operator controls.

Polls one channel over Discord's REST API (no gateway or public endpoint) and
answers ``!factory`` commands from allowlisted user IDs only:

    !factory status
    !factory pause <project>      !factory resume <project>
    !factory priority <project> high|normal

Configuration (environment, or the dashboard secrets page): DISCORD_BOT_TOKEN,
DISCORD_CHANNEL_ID and DISCORD_OPERATOR_IDS (comma-separated user IDs). The
bot needs the Message Content intent and read/send permission in the channel.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from openhands_factory.web_status import apply_action, collect_status, render_projects_table

LOGGER = logging.getLogger(__name__)
API = "https://discord.com/api/v10"
PREFIX = "!factory"
MAX_REPLY = 1900


def handle_command(
    text: str,
    *,
    action: Callable[[str, str], dict[str, Any]] = apply_action,
    status: Callable[[], dict[str, Any]] = collect_status,
) -> str | None:
    """Return the reply for one message, or None when it isn't a command."""

    words = text.strip().split()
    if not words or words[0].lower() != PREFIX:
        return None
    args = [word.lower() for word in words[1:]]
    usage = (
        "Usage: `!factory status` · `!factory pause|resume <project>` · "
        "`!factory priority <project> high|normal`"
    )
    try:
        if args == ["status"] or not args:
            return f"```\n{render_projects_table(status())}\n```"[:MAX_REPLY]
        if len(args) == 2 and args[0] in {"pause", "resume"}:
            action(args[1], args[0])
            return f"{args[1]}: {args[0]}d."
        if len(args) == 3 and args[0] == "priority" and args[2] in {"high", "normal"}:
            action(args[1], f"priority-{args[2]}")
            return f"{args[1]}: priority set to {args[2]}."
    except ValueError as error:
        return f"Error: {error}"
    return usage


class DiscordBot:
    def __init__(
        self,
        token: str,
        channel_id: str,
        operator_ids: set[str],
        client: httpx.Client | None = None,
    ) -> None:
        if not token or not channel_id.isdigit() or not operator_ids:
            raise ValueError("Discord bot needs a token, numeric channel and operator IDs")
        self.channel_id = channel_id
        self.operator_ids = operator_ids
        self.client = client or httpx.Client(
            base_url=API, headers={"Authorization": f"Bot {token}"}, timeout=20
        )
        self.last_seen: str | None = None

    def _messages(self) -> list[dict[str, Any]]:
        params = {"limit": "20"}
        if self.last_seen is not None:
            params["after"] = self.last_seen
        response = self.client.get(f"/channels/{self.channel_id}/messages", params=params)
        response.raise_for_status()
        messages = response.json()
        return messages if isinstance(messages, list) else []

    def poll_once(self) -> int:
        """Process new messages oldest-first; return how many commands ran."""

        messages = sorted(self._messages(), key=lambda message: int(message["id"]))
        if self.last_seen is None:
            # Start from now: never replay commands sent while the bot was down.
            self.last_seen = messages[-1]["id"] if messages else "0"
            return 0
        handled = 0
        for message in messages:
            self.last_seen = message["id"]
            author = message.get("author") or {}
            if author.get("bot") or str(author.get("id")) not in self.operator_ids:
                continue
            reply = handle_command(str(message.get("content") or ""))
            if reply is None:
                continue
            self.client.post(
                f"/channels/{self.channel_id}/messages",
                json={
                    "content": reply,
                    "allowed_mentions": {"parse": []},
                    "message_reference": {"message_id": message["id"]},
                },
            )
            handled += 1
        return handled

    def run(self, interval_seconds: float = 5.0) -> None:
        while True:
            try:
                self.poll_once()
            except httpx.HTTPError as error:
                # The token is in a header, never in the message; log the type only.
                LOGGER.warning("Discord poll failed: %s", type(error).__name__)
                time.sleep(30)
            time.sleep(interval_seconds)


def bot_from_environment() -> DiscordBot:
    operators = {
        value.strip()
        for value in os.environ.get("DISCORD_OPERATOR_IDS", "").split(",")
        if value.strip().isdigit()
    }
    return DiscordBot(
        os.environ.get("DISCORD_BOT_TOKEN", ""),
        os.environ.get("DISCORD_CHANNEL_ID", ""),
        operators,
    )


def wait_for_bot(recheck_seconds: float = 300.0) -> DiscordBot:
    """Idle until the bot is configured (e.g. via the dashboard secrets page)."""

    from openhands_factory.operator_secrets import load_missing_secrets

    while True:
        load_missing_secrets()
        try:
            return bot_from_environment()
        except ValueError:
            LOGGER.info("Discord bot not configured yet; checking again later")
            time.sleep(recheck_seconds)
