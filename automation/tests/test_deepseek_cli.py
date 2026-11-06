import json
from unittest.mock import patch

from openhands_factory.agents.cli import CLIProvider
from openhands_factory.agents.deepseek_cli import DeepSeekCLIProvider
from openhands_factory.config import AgentsConfig, ProviderConfig


def test_deepseek_uses_supervised_cli_and_official_api():
    provider = DeepSeekCLIProvider(ProviderConfig(enabled=True, model="deepseek_chat"))
    assert isinstance(provider, CLIProvider)
    assert provider.model == "factory-deepseek/deepseek-chat"
    with patch(
        "openhands_factory.agents.deepseek_cli._resolve_deepseek_api_key", return_value="test-only"
    ):
        environment = provider.extra_environment()
    settings = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    assert (
        settings["provider"]["factory-deepseek"]["options"]["baseURL"]
        == "https://api.deepseek.com/v1"
    )
    assert settings["permission"]["task"] == "deny"
    assert environment["DEEPSEEK_API_KEY"] == "test-only"
    assert "test-only" not in environment["OPENCODE_CONFIG_CONTENT"]


def test_experimental_config_accepts_supervised_api_cli():
    data = AgentsConfig.experimental_default().model_dump()
    data["providers"]["deepseek"]["transport"] = "cli"
    data["providers"]["deepseek"]["command"] = "opencode"
    assert AgentsConfig.model_validate(data).providers["deepseek"].auth_mode == "api"
