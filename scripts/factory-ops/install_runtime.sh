#!/usr/bin/env bash
# Deploy the reviewed local changes without launching factory tasks.
set -euo pipefail
source_root=/mnt/HC_Volume_106574422/repo-factory/control/automation
runtime=/opt/hellotalk-factory/venv/lib/python3.13/site-packages/openhands_factory
backup=/home/dev/factory-ops/backups/20261106T225259Z/runtime-before-install.tar.gz
if [[ ! -e "$backup" ]]; then
    tar -czf "$backup" -C "$runtime" .
    chmod 600 "$backup"
fi
# Install the complete package, including entry points and configuration models.
# Partial file copies can leave a runtime that passes source tests but cannot start.
/opt/hellotalk-factory/venv/bin/python -m pip install --no-deps "$source_root"
test -x /opt/hellotalk-factory/venv/bin/repo-factory
install -m 644 /home/dev/factory-tools/mcp/*.py /opt/hellotalk-factory/venv/share/factory-mcp/
chmod +x /opt/hellotalk-factory/venv/share/factory-mcp/{treesitter_mcp,ghidra_mcp,rea_mcp}.py
install -m 755 /home/dev/.local/bin/concise-build /opt/hellotalk-factory/venv/bin/concise-build
echo 'Runtime installed. No factory services started.'
