from __future__ import annotations

from typing import Any

import pytest

from openhands_factory.discord_bot import DiscordBot, handle_command


def test_commands_map_to_operator_actions() -> None:
    calls: list[tuple[str, str]] = []

    def action(name: str, act: str) -> dict[str, Any]:
        calls.append((name, act))
        return {}

    assert handle_command("hello") is None
    assert handle_command("!factory pause neptune", action=action) == "neptune: paused."
    assert handle_command("!factory priority Neptune HIGH", action=action) == (
        "neptune: priority set to high."
    )
    assert "Usage" in (handle_command("!factory delete everything", action=action) or "")
    assert calls == [("neptune", "pause"), ("neptune", "priority-high")]


def test_action_errors_are_reported() -> None:
    def action(name: str, act: str) -> dict[str, Any]:
        raise ValueError("unknown project")

    assert handle_command("!factory resume nope", action=action) == "Error: unknown project"


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class FakeClient:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = batches
        self.posts: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, str]) -> FakeResponse:
        return FakeResponse(self.batches.pop(0) if self.batches else [])

    def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        self.posts.append(json)
        return FakeResponse({})


def message(identifier: int, author: str, content: str, bot: bool = False) -> dict[str, Any]:
    return {"id": str(identifier), "author": {"id": author, "bot": bot}, "content": content}


def test_bot_ignores_backlog_strangers_and_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openhands_factory.discord_bot.handle_command", lambda text: f"ran {text}")
    client = FakeClient(
        [
            [message(1, "42", "!factory pause neptune")],  # backlog: skipped
            [
                message(3, "99", "!factory pause neptune"),  # not an operator
                message(4, "42", "!factory status", bot=True),  # a bot
                message(2, "42", "!factory status"),  # operator, out of order
            ],
        ]
    )
    bot = DiscordBot("token", "123", {"42"}, client=client)  # type: ignore[arg-type]
    assert bot.poll_once() == 0
    assert bot.poll_once() == 1
    assert client.posts[0]["content"] == "ran !factory status"
    assert client.posts[0]["allowed_mentions"] == {"parse": []}
    assert bot.last_seen == "4"


def test_bot_requires_configuration() -> None:
    with pytest.raises(ValueError):
        DiscordBot("", "123", {"1"})
    with pytest.raises(ValueError):
        DiscordBot("t", "abc", {"1"})
