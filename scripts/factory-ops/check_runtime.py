"""Validate the deployed package against the selected provider configuration."""
import json
from pathlib import Path
import sys
from openhands_factory.config import AgentsConfig

mode = sys.argv[1]
if mode not in {'experimental', 'legacy'}:
    raise SystemExit('Unknown routing mode')
path = Path('/mnt/HC_Volume_106574422/repo-factory/control/config/factory') / f'agents.{mode}.json'
config = AgentsConfig.model_validate(json.loads(path.read_text()))
service_home = Path.home().resolve()
for name, provider in config.providers.items():
    if not provider.enabled:
        continue
    for relative in [*provider.credential_paths, *provider.runtime_paths]:
        if not (service_home / relative).resolve().is_relative_to(service_home):
            raise SystemExit(f'Unsafe {name} home mount: {relative}; use a bind mount within {service_home}')
print(f'Installed runtime accepts {mode} configuration ({len(config.providers)} providers).')
