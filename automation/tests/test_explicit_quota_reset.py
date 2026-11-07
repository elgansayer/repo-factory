from datetime import UTC, datetime, timedelta

from openhands_factory.agents.base import AgentFailure, AgentFailureKind, AgentPhase, AgentResult
from openhands_factory.agents.cli import extract_retry_after_seconds
from openhands_factory.agents.health import AgentCircuitBreaker, AgentHealthStore
from openhands_factory.agents.router import AgentRouter


def test_observed_claude_reset_and_midnight_rollover():
    now = datetime(2026, 11, 6, 18, 24, 57, tzinfo=UTC)
    message = "You've hit your 5-hour limit · resets 10:20pm (UTC)"
    assert extract_retry_after_seconds(message, now) == 14103
    assert extract_retry_after_seconds(message, now.replace(microsecond=500000)) == 14103
    assert extract_retry_after_seconds(message, now.replace(hour=22, minute=20, second=5)) == 0
    near_midnight = now.replace(hour=23, minute=55, second=0)
    assert (
        extract_retry_after_seconds("session limit · resets 12:20am (UTC)", near_midnight) == 1500
    )
    assert extract_retry_after_seconds("session limit · resets 12pm (unknown)", now) is None
    assert extract_retry_after_seconds("session limit · resets 13:90pm (UTC)", now) is None
    # A weekly reset needs a date; a bare clock is insufficient.
    assert extract_retry_after_seconds("weekly limit · resets 10:20pm (UTC)", now) != 14103


def test_explicit_retry_replaces_old_floor_and_survives_restart(tmp_path):
    now = datetime.now(UTC)
    breaker = AgentCircuitBreaker("claude", failure_threshold=1, cooldown_seconds=300)
    breaker.record_failure(AgentFailureKind.PROVIDER_QUOTA, now, retry_after_seconds=21600)
    breaker.record_failure(
        AgentFailureKind.PROVIDER_QUOTA,
        now,
        retry_after_seconds=900,
        retry_after_is_explicit=True,
    )
    store = AgentHealthStore(tmp_path / "health.json", coordination_root=tmp_path)
    store.save({"claude": breaker})
    restored = store.load()["claude"]
    assert restored.consecutive_failures == 2
    assert restored.effective_cooldown_seconds() == 900
    assert restored.get_health().retry_after == now + timedelta(seconds=900)
    assert not restored.permits_call(now + timedelta(seconds=899))
    assert restored.permits_call(now + timedelta(seconds=900))
    # Recovery still leases only one real attempt.
    assert not restored.permits_call(now + timedelta(seconds=900))
    restored.record_success()
    assert not restored.retry_after_is_explicit


def test_router_preserves_explicit_zero_instead_of_default_floor():
    class Provider:
        name = "claude"

    router = AgentRouter(
        [Provider()],
        failure_threshold=1,
        cooldown_seconds=300,
        failure_cooldowns={AgentFailureKind.PROVIDER_QUOTA: 21600},
    )
    now = datetime.now(UTC)
    router._record_breaker(
        AgentResult(
            provider="claude",
            phase=AgentPhase.IMPLEMENTATION,
            success=False,
            started_at=now,
            finished_at=now,
            exit_code=1,
            summary=None,
            output_path=None,
            failure=AgentFailure(
                AgentFailureKind.PROVIDER_QUOTA, "reset now", retry_after_seconds=0
            ),
            transport="fake",
            model="fake",
        )
    )
    breaker = router._breakers()["claude"]
    assert breaker.retry_after_seconds == 0
    assert breaker.retry_after_is_explicit
    assert breaker.effective_cooldown_seconds() == 300
