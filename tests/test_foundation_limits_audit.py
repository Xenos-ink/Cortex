"""Wave 1 foundation tests: limits.py, redaction.py, and audit.py."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from computer_use_mcp.audit import AuditEvent, AuditLogger, Metrics
from computer_use_mcp.limits import LimitEnforcer, LimitExceeded, Limits
from computer_use_mcp.redaction import contains_secret, redact_image, redact_text, safe_repr

# --- Limits ------------------------------------------------------------------------------

def test_limits_defaults_match_mission_contract() -> None:
    limits = Limits()
    assert limits.max_task_seconds == 900.0
    assert limits.max_actions == 100
    assert limits.max_retries_per_action == 5
    assert limits.max_recovery_per_action == 2
    assert limits.max_recovery_per_task == 6
    assert limits.max_model_calls == 60
    assert limits.min_screenshot_interval_ms == 250
    assert limits.max_context_items == 50
    assert limits.max_sessions == 4


def test_limits_validate_clamps_high_values() -> None:
    clamped = Limits(
        max_task_seconds=100_000.0,
        max_actions=10_000,
        max_retries_per_action=50,
        max_recovery_per_action=99,
        max_recovery_per_task=999,
        max_model_calls=500_000,
        min_screenshot_interval_ms=600_000,
        max_context_items=10_000,
        max_sessions=1_000,
    ).validate()
    assert clamped.max_task_seconds == 3600.0
    assert clamped.max_actions == 500
    assert clamped.max_retries_per_action == 5
    assert clamped.max_recovery_per_action == 10
    assert clamped.max_recovery_per_task == 50
    assert clamped.max_model_calls == 1000
    assert clamped.min_screenshot_interval_ms == 60_000
    assert clamped.max_context_items == 1000
    assert clamped.max_sessions == 64


def test_limits_validate_clamps_low_values() -> None:
    clamped = Limits(
        max_task_seconds=-5.0,
        max_actions=0,
        max_retries_per_action=-3,
        max_recovery_per_action=-1,
        max_recovery_per_task=-10,
        max_model_calls=0,
        max_context_items=0,
        max_sessions=0,
    ).validate()
    assert clamped.max_task_seconds == 1.0
    assert clamped.max_actions == 1
    assert clamped.max_retries_per_action == 0
    assert clamped.max_recovery_per_action == 0
    assert clamped.max_recovery_per_task == 0
    assert clamped.max_model_calls == 1
    assert clamped.max_context_items == 1
    assert clamped.max_sessions == 1


def test_limits_validate_does_not_mutate_original() -> None:
    original = Limits(max_actions=10_000)
    clamped = original.validate()
    assert original.max_actions == 10_000
    assert clamped.max_actions == 500
    assert clamped is not original


# --- LimitEnforcer -----------------------------------------------------------------------

def test_enforcer_validates_limits_on_construction() -> None:
    enforcer = LimitEnforcer(Limits(max_actions=10_000))
    assert enforcer.limits.max_actions == 500


def test_enforcer_trips_max_actions() -> None:
    enforcer = LimitEnforcer(Limits(max_actions=2))
    enforcer.check_action()
    enforcer.record_action()
    enforcer.check_action()
    enforcer.record_action()
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_action()
    assert excinfo.value.limit_name == "max_actions"


def test_enforcer_trips_max_retries_per_action() -> None:
    enforcer = LimitEnforcer(Limits(max_retries_per_action=2))
    enforcer.check_retry()
    enforcer.record_retry()
    enforcer.check_retry()
    enforcer.record_retry()
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_retry()
    assert excinfo.value.limit_name == "max_retries_per_action"


def test_enforcer_trips_max_recovery_per_action() -> None:
    enforcer = LimitEnforcer(Limits(max_recovery_per_action=1, max_recovery_per_task=10))
    enforcer.record_recovery()
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_recovery()
    assert excinfo.value.limit_name == "max_recovery_per_action"


def test_enforcer_trips_max_recovery_per_task_and_survives_action_reset() -> None:
    enforcer = LimitEnforcer(Limits(max_recovery_per_action=5, max_recovery_per_task=2))
    enforcer.record_recovery()
    enforcer.begin_action()  # new action: per-action scope resets
    enforcer.record_recovery()
    assert enforcer.snapshot()["recovery_current_action"] == 1
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_recovery()
    assert excinfo.value.limit_name == "max_recovery_per_task"


def test_enforcer_begin_action_resets_retry_scope() -> None:
    enforcer = LimitEnforcer(Limits(max_retries_per_action=1))
    enforcer.record_retry()
    enforcer.begin_action()
    enforcer.check_retry()  # must not raise


def test_enforcer_trips_max_model_calls() -> None:
    enforcer = LimitEnforcer(Limits(max_model_calls=1))
    enforcer.check_model_call()
    enforcer.record_model_call()
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_model_call()
    assert excinfo.value.limit_name == "max_model_calls"


def test_enforcer_trips_task_duration() -> None:
    enforcer = LimitEnforcer(Limits(max_task_seconds=900.0))
    enforcer.check_task_duration()
    enforcer._started_monotonic -= 901.0  # white-box: simulate elapsed time
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_task_duration()
    assert excinfo.value.limit_name == "max_task_seconds"


def test_enforcer_trips_context_items() -> None:
    enforcer = LimitEnforcer(Limits(max_context_items=10))
    enforcer.check_context_items(10)
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_context_items(11)
    assert excinfo.value.limit_name == "max_context_items"


def test_enforcer_trips_session_count() -> None:
    enforcer = LimitEnforcer(Limits(max_sessions=2))
    enforcer.check_session_count(1)
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_session_count(2)
    assert excinfo.value.limit_name == "max_sessions"


def test_enforcer_screenshot_rate_gate() -> None:
    enforcer = LimitEnforcer(Limits(min_screenshot_interval_ms=250))
    assert enforcer.can_screenshot() is True
    enforcer.record_screenshot()
    assert enforcer.can_screenshot() is False
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_screenshot()
    assert excinfo.value.limit_name == "min_screenshot_interval_ms"
    time.sleep(0.26)
    assert enforcer.can_screenshot() is True
    enforcer.check_screenshot()


def test_enforcer_snapshot_reports_counters() -> None:
    enforcer = LimitEnforcer(Limits())
    enforcer.record_action()
    enforcer.record_model_call()
    snapshot = enforcer.snapshot()
    assert snapshot["actions"] == 1
    assert snapshot["model_calls"] == 1
    assert "elapsed_seconds" in snapshot


# --- redaction ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("password", "password=hunter2secret"),
        ("token", "token=abc123def456"),
        ("api_key", "api_key=sk-abcdef123456"),
        ("client_secret", "client_secret=shhh-123456"),
        ("bearer", "Authorization: Bearer abcdef1234567890abcdef123456"),
        ("aws", "AKIAIOSFODNN7EXAMPLE"),
        ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
        ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowAAAAA\n-----END RSA PRIVATE KEY-----"),
        ("private_key_header", "-----BEGIN PRIVATE KEY-----"),
        ("basic_auth_url", "https://alice:s3cretpw@example.com/api"),
        ("credit_card", "4111111111111111"),
        ("credit_card_spaced", "4111 1111 1111 1111"),
    ],
)
def test_redaction_catches_each_secret_class(name: str, secret: str) -> None:
    redacted, count = redact_text(f"prefix {secret} suffix")
    assert count >= 1, f"pattern {name} not detected"
    assert secret not in redacted
    assert "[REDACTED:" in redacted
    assert contains_secret(secret) is True


@pytest.mark.parametrize(
    "benign",
    [
        "The password policy requires at least 12 characters.",
        "Bearer authentication is required for the API.",
        "Authorization header was missing from the request.",
        "Visit https://example.com/docs for API details.",
        "Local service reachable at https://example.com:8443/help.",
        "The secret sauce of great teams is trust.",
        "AWS access keys rotate every 90 days.",
        "The JWT algorithm HS256 is widely used.",
        "Invoice total is 123.45 EUR.",
        "Support hotline: 555-0100 extension 4.",
        "Set TOKEN_BUCKET_CAPACITY to 50 in config.",
    ],
)
def test_redaction_has_no_false_positives_on_benign_strings(benign: str) -> None:
    redacted, count = redact_text(benign)
    assert count == 0, f"false positive on: {benign} -> {redacted}"
    assert redacted == benign
    assert contains_secret(benign) is False


def test_luhn_filter_rejects_non_pan_digit_strings() -> None:
    redacted, count = redact_text("order id 1234567812345678")  # fails Luhn
    assert count == 0
    assert redacted == "order id 1234567812345678"


def test_redact_text_counts_replacements() -> None:
    text = "api_key=sk-abcdef123456 and password=hunter2secret"
    redacted, count = redact_text(text)
    assert count == 2
    assert "sk-abcdef123456" not in redacted
    assert "hunter2secret" not in redacted


def test_safe_repr_never_leaks_secrets() -> None:
    payload = {"api_key": "sk-abcdef123456", "note": "password=hunter2secret"}
    rendered = safe_repr(payload)
    assert "sk-abcdef123456" not in rendered
    assert "hunter2secret" not in rendered
    assert "[REDACTED:" in rendered


def test_redact_image_blurs_given_regions_and_keeps_original() -> None:
    image = Image.new("RGB", (100, 100), "white")
    for x in range(12, 36):  # non-uniform content inside the region so blur has an effect
        for y in range(12, 28):
            image.putpixel((x, y), (0, 0, 0))
    original = image.copy()
    redacted, count = redact_image(image, regions=[(10, 10, 30, 20)])
    assert count == 1
    assert redacted is not image
    assert redacted.getpixel((20, 20)) != original.getpixel((20, 20))
    assert image.getpixel((20, 20)) == original.getpixel((20, 20))


def test_redact_image_without_regions_uses_noop_scan_hook() -> None:
    image = Image.new("RGB", (50, 50), "white")
    redacted, count = redact_image(image)
    assert count == 0
    assert redacted.tobytes() == image.tobytes()


def test_redact_image_ignores_out_of_bounds_regions() -> None:
    image = Image.new("RGB", (40, 40), "white")
    _, count = redact_image(image, regions=[(500, 500, 10, 10)])
    assert count == 0


# --- audit -------------------------------------------------------------------------------

def test_audit_event_defaults_and_literal_event_types() -> None:
    event = AuditEvent(session_id="s1", event_type="execution")
    assert event.timestamp.tzinfo is not None
    assert event.task_id is None
    assert event.metadata == {}
    with pytest.raises(ValidationError):
        AuditEvent(session_id="s1", event_type="not_a_real_event")


def test_audit_jsonl_round_trip_with_redaction_enforced(tmp_path: Path) -> None:
    logger = AuditLogger(log_dir=tmp_path)
    logger.emit(
        "execution",
        "session one/slash",  # exercises filename sanitization
        task_id="task-1",
        observation_id="obs-1",
        action_id="action-1",
        active_app="notepad.exe",
        risk="high",
        result="token=abc123def456",
        duration_ms=12.5,
        metadata={"note": "api_key=sk-abcdef123456", "attempt": 2, "ok": True},
    )
    path = tmp_path / "audit_session_one_slash.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    raw = lines[0]
    assert "sk-abcdef123456" not in raw
    assert "abc123def456" not in raw
    assert "[REDACTED:" in raw
    event = json.loads(raw)
    for field in ("timestamp", "session_id", "event_type", "active_app", "risk", "result", "duration_ms"):
        assert field in event
    assert event["event_type"] == "execution"
    assert event["metadata"]["attempt"] == 2
    assert event["metadata"]["ok"] is True
    assert event["metadata"]["note"].startswith("[REDACTED:")


def test_audit_redacts_sensitive_metadata_keys(tmp_path: Path) -> None:
    logger = AuditLogger(log_dir=tmp_path)
    logger.emit("observation", "s", metadata={"api_key": "sk-abcdef123456", "author": "runtime"})
    raw = (tmp_path / "audit_s.jsonl").read_text(encoding="utf-8")
    assert "sk-abcdef123456" not in raw
    event = json.loads(raw)
    assert event["metadata"]["api_key"] == "[REDACTED:api_key]"
    assert event["metadata"]["author"] == "runtime"  # no "author" false positive


def test_audit_logger_is_thread_safe(tmp_path: Path) -> None:
    logger = AuditLogger(log_dir=tmp_path)
    total_threads, per_thread = 5, 20

    def worker(thread_index: int) -> None:
        for step in range(per_thread):
            logger.emit("observation", "shared", metadata={"i": thread_index, "j": step})

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(total_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    lines = (tmp_path / "audit_shared.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == total_threads * per_thread
    assert all(json.loads(line)["session_id"] == "shared" for line in lines)


# --- metrics -----------------------------------------------------------------------------

def test_metrics_snapshot_covers_all_counters_and_latencies() -> None:
    metrics = Metrics()
    expected_counters = {
        "task_started",
        "task_completed",
        "task_failed",
        "action_total",
        "action_success",
        "action_failure",
        "verification_verified",
        "verification_failed",
        "verification_uncertain",
        "grounding_failure",
        "safety_block",
        "approval_requested",
        "approval_granted",
        "approval_denied",
        "retry_total",
        "recovery_total",
        "recovery_success",
        "model_calls",
        "screenshot_count",
    }
    expected_latencies = {"observation_ms", "model_ms", "execution_ms", "verification_ms", "task_ms"}
    snapshot = metrics.snapshot()
    assert set(snapshot["counters"]) == expected_counters
    assert set(snapshot["latencies"]) == expected_latencies
    assert all(value == 0 for value in snapshot["counters"].values())
    assert snapshot["latencies"]["model_ms"]["count"] == 0


def test_metrics_counters_and_latency_stats() -> None:
    metrics = Metrics()
    metrics.incr("task_started")
    metrics.incr("action_total", 3)
    metrics.incr("safety_block")
    for value in (10.0, 20.0, 30.0, 40.0):
        metrics.record_latency("execution_ms", value)
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["action_total"] == 3
    assert snapshot["counters"]["task_started"] == 1
    assert snapshot["counters"]["safety_block"] == 1
    latency = snapshot["latencies"]["execution_ms"]
    assert latency["count"] == 4
    assert latency["avg_ms"] == 25.0
    assert latency["p50_ms"] == 25.0
    assert latency["max_ms"] == 40.0
    # linear interpolation at 0.95 over [10,20,30,40]: rank 2.85 -> 30*0.15 + 40*0.85
    assert latency["p95_ms"] == pytest.approx(38.5)


def test_metrics_is_thread_safe() -> None:
    metrics = Metrics()
    threads, per_thread = 8, 250

    def worker() -> None:
        for _ in range(per_thread):
            metrics.incr("action_total")
            metrics.record_latency("model_ms", 1.0)

    threads_list = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in threads_list:
        thread.start()
    for thread in threads_list:
        thread.join(timeout=10.0)
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["action_total"] == threads * per_thread
    # latency history is deliberately bounded; it keeps the most recent 1024 samples
    assert snapshot["latencies"]["model_ms"]["count"] == min(threads * per_thread, 1024)
