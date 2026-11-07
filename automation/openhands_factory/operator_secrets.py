"""Operator-supplied secrets used only when the environment lacks them.

The web status page can store values for a fixed allowlist of keys in
``FACTORY_OPERATOR_CONTROL_DIR/secrets.env`` (mode 0600). At startup the
Factory copies a stored value into its environment only when that key is
missing, so instance env files always win. Values are never displayed: the
page reports only whether each key is stored.
"""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping
from pathlib import Path

from openhands_factory.operator_controls import operator_control_dir

SECRET_KEYS: dict[str, str] = {
    "DEEPSEEK_API_KEY": "DeepSeek API key (overflow provider)",
    "OPENCODE_GO_API_KEY": "OpenCode Go API key",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token for alerts",
    "TELEGRAM_CHAT_ID": "Telegram chat ID for alerts",
    "FACTORY_DISCORD_WEBHOOK_URL": "Discord webhook URL for alerts",
    "DISCORD_BOT_TOKEN": "Discord bot token (commands)",
    "DISCORD_CHANNEL_ID": "Discord channel ID the bot listens in",
    "DISCORD_OPERATOR_IDS": "Discord user IDs allowed to command (comma-separated)",
    "CLOUDFLARE_API_TOKEN": "Cloudflare API token (tunnel hostnames)",
}
_VALUE = re.compile(r"^[\x21-\x7e]{1,4096}$")


def secrets_path(directory: Path | None = None) -> Path:
    return (directory or operator_control_dir()) / "secrets.env"


def read_secrets(directory: Path | None = None) -> dict[str, str]:
    try:
        lines = secrets_path(directory).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key in SECRET_KEYS and _VALUE.fullmatch(value):
            values[key] = value
    return values


def stored_keys(directory: Path | None = None) -> set[str]:
    return set(read_secrets(directory))


def set_secret(key: str, value: str | None, directory: Path | None = None) -> None:
    """Store (or with ``None``/empty, remove) one allowlisted secret atomically."""

    if key not in SECRET_KEYS:
        raise ValueError("unsupported secret")
    values = read_secrets(directory)
    if value:
        if not _VALUE.fullmatch(value):
            raise ValueError("secret must be printable without spaces")
        values[key] = value
    else:
        values.pop(key, None)
    path = secrets_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("".join(f"{name}={values[name]}\n" for name in sorted(values)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_missing_secrets(
    environment: MutableMapping[str, str] | None = None, directory: Path | None = None
) -> list[str]:
    """Fill only absent allowlisted keys; return the names that were loaded."""

    env = os.environ if environment is None else environment
    loaded = []
    for key, value in read_secrets(directory).items():
        if not env.get(key):
            env[key] = value
            loaded.append(key)
    return loaded
