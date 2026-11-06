#!/usr/bin/env bash
# Changes routing and proxy only. Start factory daemons separately.
set -euo pipefail
mode="${1:-status}"
root=/home/dev/factory-ops/litellm
control=/mnt/HC_Volume_106574422/repo-factory/control
state=/var/lib/repo-factory/shared/routing-mode
pidfile="$root/litellm.pid"
port=4000
stop_proxy() {
    systemctl --user stop factory-litellm.service 2>/dev/null || true
    [[ -f "$pidfile" ]] || return 0
    read -r pid < "$pidfile"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        if ! tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- 'litellm --config /home/dev/litellm_config.yaml'; then
            echo 'PID belongs to another process; refusing to signal it.' >&2
            return 1
        fi
        kill -TERM "$pid"
        for _ in {1..50}; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo 'Proxy did not stop; inspect before retrying.' >&2
            return 1
        fi
    fi
    unlink "$pidfile"
}
case "$mode" in
    status)
        if [[ -f "$state" ]]; then read -r current < "$state"; else current=legacy; fi
        printf 'FACTORY_ROUTING_MODE=%s\n' "$current"
        [[ ! -f "$pidfile" ]] || printf 'Proxy PID file: %s\n' "$pidfile"
        exit 0;;
    stop-proxy) stop_proxy; exit 0;;
    legacy|experimental) ;;
    *) echo "Usage: $0 {experimental|legacy|status|stop-proxy} [--no-restart]" >&2; exit 2;;
esac
[[ "${2:-}" == '' || "${2:-}" == --no-restart ]] || exit 2
test -r "$root/litellm.$mode.yaml"
test -r "$control/config/factory/agents.$mode.json"
stop_proxy
install -m 600 "$root/litellm.$mode.yaml" /home/dev/litellm_config.yaml
install -m 600 "$control/config/factory/agents.$mode.json" /home/dev/factory-ops/agents.active.json
printf '%s\n' "$mode" > "$state"
printf 'FACTORY_ROUTING_MODE=%s\nFACTORY_AGENTS_CONFIG=/home/dev/factory-ops/agents.active.json\n' "$mode" > /home/dev/factory-ops/routing.env
printf 'export FACTORY_ROUTING_MODE=%q\nexport FACTORY_AGENTS_CONFIG=%q\nexport LITELLM_PROXY_URL=%q\n' \
    "$mode" /home/dev/factory-ops/agents.active.json "http://127.0.0.1:$port" > /home/dev/.factory_routing_mode.env
if [[ "${2:-}" == --no-restart ]]; then
    echo "Mode=$mode; proxy stopped."
    exit 0
fi
if [[ "$mode" == experimental ]]; then
    docker compose -f "$root/docker-compose.yml" up -d --wait
fi
if [[ -f /home/dev/.zsh_secrets.env ]]; then source /home/dev/.zsh_secrets.env; fi
export PYTHONPATH="$root:${PYTHONPATH:-}"
export FACTORY_ROUTING_MODE="$mode"
systemctl --user daemon-reload
systemctl --user restart factory-litellm.service
pid=$(systemctl --user show factory-litellm.service -p MainPID --value)
printf '%s\n' "$pid" > "$pidfile"
for _ in {1..50}; do
    if curl -fsS "http://127.0.0.1:$port/health/liveliness" >/dev/null 2>&1; then
        echo "Mode=$mode; proxy ready on 127.0.0.1:$port. Factory daemons not started."
        exit 0
    fi
    kill -0 "$pid" 2>/dev/null || { echo "Proxy failed; see $root/litellm-proxy.log" >&2; exit 1; }
    sleep 1
done
echo "Proxy health timed out; see $root/litellm-proxy.log" >&2
exit 1
