from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from openhands_factory.operator_secrets import (
    load_missing_secrets,
    read_secrets,
    secrets_path,
    set_secret,
)


def test_secrets_fill_only_missing_allowlisted_keys(tmp_path: Path) -> None:
    set_secret("DEEPSEEK_API_KEY", "stored-key", tmp_path)
    set_secret("TELEGRAM_CHAT_ID", "123", tmp_path)
    secrets_path(tmp_path).write_text(
        secrets_path(tmp_path).read_text() + "PATH=/evil\nLD_PRELOAD=x\n"
    )
    env = {"TELEGRAM_CHAT_ID": "from-env"}
    assert load_missing_secrets(env, tmp_path) == ["DEEPSEEK_API_KEY"]
    assert env == {"TELEGRAM_CHAT_ID": "from-env", "DEEPSEEK_API_KEY": "stored-key"}
    assert stat.S_IMODE(os.stat(secrets_path(tmp_path)).st_mode) == 0o600


def test_unknown_keys_and_bad_values_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_secret("PATH", "/x", tmp_path)
    with pytest.raises(ValueError):
        set_secret("DEEPSEEK_API_KEY", "has space", tmp_path)
    with pytest.raises(ValueError):
        set_secret("DEEPSEEK_API_KEY", "line\nINJECT=1", tmp_path)
    set_secret("DEEPSEEK_API_KEY", "k", tmp_path)
    set_secret("DEEPSEEK_API_KEY", None, tmp_path)
    assert read_secrets(tmp_path) == {}


def test_secrets_page_never_renders_values(tmp_path: Path, monkeypatch) -> None:
    from openhands_factory.web_status import render_secrets_html

    monkeypatch.setenv("FACTORY_OPERATOR_CONTROL_DIR", str(tmp_path))
    set_secret("DEEPSEEK_API_KEY", "super-secret-value")
    page = render_secrets_html()
    assert "super-secret-value" not in page
    assert "stored" in page
