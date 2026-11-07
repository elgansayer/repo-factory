# Factory deployment sources

This directory documents the November 2026 host configuration. Scripts retain the existing
/home/dev, /opt/hellotalk-factory and /mnt/HC_Volume_106574422 paths; this is a host deployment
snapshot, not a portable installer. See OPERATIONS.md for routing, rollback and limitations.

| Repository source | Deployed destination |
| --- | --- |
| scripts/factory-ops/ | /home/dev/factory-ops/ |
| scripts/factory-ops/switch_mode.sh | /home/dev/switch_mode.sh |
| config/litellm/ | /home/dev/factory-ops/litellm/ |
| config/factory/agents.experimental.json | Selected by switch_mode.sh |
| config/factory/agents.legacy.json | Selected by switch_mode.sh |
| tools/factory-mcp/*.py | /home/dev/factory-tools/mcp/ and runtime share/factory-mcp/ |
| tools/factory-wrappers/ | /home/dev/factory-tools/bin/ |
| tools/factory-wrappers/concise-build | ~/.local/bin/ and runtime bin/ |
| config/mcp/factory.json | Merge its server into each supported client's MCP configuration |
| config/mcp/factory-slim.context.yml | /home/dev/factory-tools/serena/factory-slim.context.yml |
| automation/skills/ | /home/dev/.agents/skills/ |
| config/systemd/factory-litellm.service | ~/.config/systemd/user/factory-litellm.service |
| config/systemd/factory-routing.conf | /etc/systemd/system/repo-factory@.service.d/90-routing.conf |
| config/systemd/storage-units/ | /etc/systemd/system/ |

The Python runtime and optional MCP/proxy dependencies are separate. The recorded optional
versions are in tools/factory-mcp/requirements-host.txt. Native dependencies include the provider
CLIs, Caveman and its companions, RTK, ast-grep, Serena, Ghidra 12.2, and Java 25. They are installed
on the host but their downloads and caches are not vendored here. Ghidra's downloaded archive was
verified against SHA-256 2c2cd776db3c1f6a097ba418922378ac3649cadd5e844a6f5f088ae628a7be67.

The migration script lists only the five data-directory moves performed during this repair.
UV and Playwright cache mounts are also recorded, but preparing a new machine requires reviewing
its existing mounts and data before enabling these host-specific units. Do not blindly replay a
storage migration on a different host. install_runtime.sh installs the complete Python package
and copies prepared tools; it does not install every external dependency or enable system units.

Credentials, .env files, subscription state, STATE_BACKUP.md, backup archives, logs, Ghidra
projects and downloaded binaries stay on the host. TOKEN_BENCHMARK.md contains the non-secret
fixture results. Validation scripts generate fresh local artifacts; they do not prove current
remote quota, subscription entitlement or billing balance.
