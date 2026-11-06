# Factory routing and token controls

Configuration is installed locally. After the requested startup repair, all three factory services
were relaunched in experimental mode on 2026-11-06; their storage gates were clear.
This confirms daemon health, not completed model work. Factory watchdog/update/path units and relevant
cron entries remain disabled. Product containers and interactive agent sessions were left running.

## Manual launch

These commands switch routing, start the loopback proxy/cache, unpause the three instances,
and start their existing systemd services. They require the operator's normal sudo access.

```bash
/home/dev/factory-ops/start_factory.sh experimental
/home/dev/factory-ops/start_factory.sh legacy
```

Run only the desired command. To change configuration without launching workers:

```bash
/home/dev/switch_mode.sh experimental
/home/dev/switch_mode.sh legacy
/home/dev/switch_mode.sh experimental --no-restart
/home/dev/switch_mode.sh stop-proxy
```

The helper uses a user systemd service for the proxy, port 4000. It never starts factory workers.
Cron and watchdog/update automation stays disabled even after manual launch. Re-enable those only
when their previous scheduling is desired; the original crontab and enabled-unit evidence are backed up.

## Routing

Experimental native factory workers use Codex (OpenAI subscription), then Claude Max, Antigravity,
OpenCode Go, and official DeepSeek API through a supervised OpenCode CLI adapter. The same priority
applies to repairs and reviews. The last adapter uses an isolated custom provider with the official
https://api.deepseek.com/v1 endpoint, preserving the requested deepseek-chat/deepseek-reasoner aliases.
OpenAI expires at 2026-11-17 00:00:00 UTC; Claude expires at 2026-12-02 00:00:00 UTC, including each
requested final day. Quota failures and timeouts advance the proxy chain without retries.
Proxy quota cooldowns are persisted; absent a provider reset timestamp, rolling quotas use five hours
and weekly quotas conservatively use seven days. Native factory breakers have separate persisted
backoff when no provider hint is available. Explicit retry hints take precedence over exponential
backoff, retaining the short base cooldown and single recovery lease. Availability, account plan,
remaining allowance and model
entitlement are not proven by the local tests. DeepSeek uses API billing, not a subscription allowance.

Important capability boundary: the local Codex/Claude/Antigravity LiteLLM bridges accept text only.
Native factory CLI workers retain their own full tools. OpenHands sends tool requests to LiteLLM;
the text bridges explicitly decline them, so those requests continue to OpenCode or DeepSeek.
They cannot consume the first three subscriptions through the proxy with native tool calls yet.
The OpenCode bridge preserves API tool schemas, tool call IDs, responses, and usage through LiteLLM.
Gemini API billing is disabled unless FACTORY_ALLOW_GEMINI_API=1 is explicitly set.

Legacy provider files were not rewritten. Experimental files are separate. Systemd reads
/home/dev/factory-ops/routing.env last; the active provider JSON is /home/dev/factory-ops/agents.active.json.
The existing factory start/concurrency budgets remain in place.

## Startup repair and storage relocation

The failed systemd preflight was caused by an incomplete runtime installation: the repo-factory
entry point and current configuration schema were missing. The installer now installs the complete
Python package. The launcher validates its configuration and stops/pauses all instances if startup
fails. The new DeepSeek CLI adapter also satisfies supervised-session provider requirements.
Preflight now rejects provider home symlinks that escape the sandbox. The relocated UV and
Playwright caches use persistent bind mounts, avoiding the unsafe-home-mount error seen on the
first worker attempt. Both OpenCode and DeepSeek passed isolated model-discovery checks afterward.
The focused startup/configuration suite passed 63 tests; metadata-only OpenCode discovery accepted
both custom DeepSeek models. This does not prove remote DeepSeek authentication or billing balance.

Root free space increased from 4.1 GiB before the main moves to 9.7 GiB, clearing the 5 GiB reserve.
The existing data volume still had 141 GiB free. These directories now live under
/mnt/HC_Volume_106574422/repo-factory/host-data/:

| Original path (preserved) | Data-volume directory |
| --- | --- |
| /home/dev/neptune-evidence | neptune-evidence |
| /home/dev/Sega-Saturn-BIOS | saturn-reference-assets |
| /home/dev/neptune-asset-source | neptune-asset-source |
| /home/dev/factory-tools | factory-tools |
| /home/dev/.local/share/godot | godot-tools |

Each move used rsync with ownership, hard links, ACLs and xattrs preserved, followed by a checksum
comparison before removing the duplicate on root. Enabled systemd bind mounts preserve existing
absolute paths, including inside worker sandboxes. Factory services require these mounts before
starting. Mount unit sources are in factory-ops/storage-units; migrate_host_data.py records the
exact migration procedure. UV/Playwright caches and the Ghidra download archive also moved earlier
to repo-factory/shared-tools on the same volume. The caches have their own enabled mount units.

## Capacity decision

Defer a compute upgrade for factory throughput. While lanes were idle, this four-CPU host had
roughly 4.6 GiB of available RAM and one-minute load 0.68. Those idle readings do not measure busy
build performance, but they do not establish a compute bottleneck. Existing storage now clears
the admission gate. A larger box may improve disk headroom and local grep/git/build latency;
it cannot increase a provider subscription allowance.

The audit found an avoidable quota delay: Claude's last recorded failure at 18:24:57 UTC said its
five-hour limit reset at 22:20 UTC, but the native breaker scheduled its next probe for 06:24:57 UTC
the following day. The parser now reads explicit UTC session reset clocks, including midnight
rollover. Provider hints survive persistence and bypass exponential multiplication; unspecified
quota failures retain the existing backoff policy. Ninety-one focused tests passed, followed by
eight parser/boundary checks after a rounding fix.

The original shared health record is backed up as agent-health-before-reset-fix.json in the audit
backup directory. Only Claude's matching historical retry hint was corrected to its recorded
22:20 UTC reset; its open circuit and failure count were preserved for a single recovery attempt.
This does not establish current account allowance, since other sessions can consume it. Codex and
Google quota records were left intact. First assess actual allowance, fallback success and completed
jobs per quota window; consider compute only with busy-host evidence.
Claude's published limits include five-hour and weekly windows:
https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-max-or-ultra-plan.

## Token controls

- Code Mode registers two tools (schema, execute), with Tree-sitter, Ghidra, REA and Serena behind them.
  REA is a compatibility name sharing the headless Ghidra implementation, not a separate upstream engine.
- Ghidra 12.2 and Java 25 perform real function decompilation and persistent project symbol renames.
  The archive SHA-256 was checked against the official release. Binary bytes are not changed.
- All five requested languages use Tree-sitter grammars, including GDScript. AST and symbol queries page
  at 15; projection is forwarded to backends; oversized text spills to workspace files.
- The worker skills enforce bounded reads, targeted binary inspection, terse output and minimal changes.
  OpenHands file-editor reads over 150 lines are rejected unless an explicit bounded range is supplied.
  Shell read prohibitions are prompt rules, not a complete shell security sandbox.
- OpenHands receives the Code Mode MCP configuration directly, plus a condenser at 20 events.
  That is an event threshold, not an exact ten-turn clock. LiteLLM prunes at ten estimated turns,
  preserving tool-call/result pairs and the original goal. Native CLI internal histories remain
  governed by their clients and Caveman; exact ten-turn pruning is not enforced inside those clients.
- Compiler PATH shims and RTK are available to native workers and mounted into rootless build containers.
  Exit codes survive compression. Existing repository verification receipts remain visible.
- Caveman's CLI, engine, proxy, recovery MCP and companion binaries are installed. Experimental
  Codex/Claude/OpenCode use its wrapper and mount its state. Local plain-text fixtures showed 0% extra
  Caveman reduction; no live model-output savings are claimed.
- Redis uses exact requests, default_off, no semantic matching. Opt-in requests must carry
  metadata.factory_cache_kind = ast, check or embedding, and cache.use-cache = true.
  Tool and multi-turn requests are explicitly denied response caching by the proxy callback.
- Provider prompt caching is distinct from Redis. Anthropic-compatible requests get bounded ephemeral
  cache markers; OpenAI/DeepSeek discounts depend on provider automatic caching. Markers cannot force a
  subscription discount. Context filters exclude build/dependency/binary/log outputs in the host and
  factory control files and in the AST walker; .agentignore is not universally honored by every CLI.

## Validation and benchmark

```bash
/opt/hellotalk-factory/venv/bin/python /home/dev/factory-ops/litellm/validate_offline.py
docker compose -f /home/dev/factory-ops/litellm/docker-compose.yml up -d --wait
/opt/hellotalk-factory/venv/bin/python /home/dev/factory-ops/benchmark_tokens.py
```

The first command uses real LiteLLM routing with local mock responses. It checks all five tiers,
429s, timeout, expiry boundaries, history integrity, cache opt-out, and tool-call preservation.
It does not validate remote authentication or subscription eligibility. Both proxy modes passed
local HTTP health checks. The focused factory suite passed 99 tests.
The benchmark uses real compilation, Ghidra decompile/rename, Serena editing, MCP catalogs and Redis
with a local HTTP backend. Results: /home/dev/TOKEN_BENCHMARK.md. Logs: factory-ops/validation/.

## Recovery

STATE_BACKUP.md records the audit. Originals and the crontab are under
/home/dev/factory-ops/backups/20261106T225259Z/, permissions restricted. configuration.tar.gz
preserves the previous proxy/MCP/provider configuration; runtime-before-install.tar.gz preserves
the deployed Python package. Archives can contain credentials: do not publish them.
Switching to legacy restores provider selection; token-tool repairs remain installed. Full runtime
rollback requires reviewing and restoring the relevant archived files while services are stopped.
No factory tasks, branches, queues, containers or volumes were deleted.

Sources checked: [LiteLLM cache controls](https://docs.litellm.ai/docs/proxy/caching_controls),
[Ghidra 12.2 release](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.2_build),
[Ghidra decompiler API](https://ghidra.re/ghidra_docs/api/ghidra/app/decompiler/DecompInterface.html).
