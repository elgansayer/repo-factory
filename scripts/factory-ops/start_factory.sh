#!/usr/bin/env bash
# Explicit manual launch; never invoked by installation or validation.
set -euo pipefail
mode="${1:?Usage: start_factory.sh experimental|legacy}"
[[ "$mode" == experimental || "$mode" == legacy ]] || exit 2
sudo -v
test -x /opt/hellotalk-factory/venv/bin/repo-factory || {
    echo 'Factory executable missing. Run bash /home/dev/factory-ops/install_runtime.sh first.' >&2
    exit 1
}
/opt/hellotalk-factory/venv/bin/python /home/dev/factory-ops/check_runtime.py "$mode"
/home/dev/switch_mode.sh "$mode"
rollback_start() {
    sudo systemctl stop repo-factory@neptune.service repo-factory@hellotalk.service repo-factory@workout-agent.service || true
    /opt/hellotalk-factory/venv/bin/python /home/dev/factory-ops/resume_instances.py --pause
    echo 'Startup failed; all three instances stopped and paused. Inspect journalctl for the failed service.' >&2
}
trap rollback_start ERR
/opt/hellotalk-factory/venv/bin/python /home/dev/factory-ops/resume_instances.py
sudo systemctl start repo-factory@neptune.service repo-factory@hellotalk.service repo-factory@workout-agent.service
for _ in {1..5}; do
    sleep 1
    for instance in neptune hellotalk workout-agent; do
        systemctl is-active --quiet "repo-factory@$instance.service"
    done
done
trap - ERR
echo 'Three factory instances started. Watchdogs, updates and cron remain disabled.'
