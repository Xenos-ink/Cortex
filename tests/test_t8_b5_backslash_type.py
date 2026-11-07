"""T8 anomaly-B5 regression: typing a raw single-backslash Windows path must complete.

The incident (evidence/perf-004/p3/runs/r1-run0-type-stall): a 52-char path typed into
the Run dialog never returned (>10 min, 0 CPU) while the same tree typed backslash
paths successfully in the very next run (r1-run1, precision 1.0) — an activation-race
wedge, not a text-content defect. Root-cause findings pinned here:

- the SendInput unicode batching handles every backslash path character (VK_PACKET
  units for the whole string, including '\\', ':' and '.') — unit-level;
- REAL dispatch of the exact incident payload completes well under 1 s (live test);
- a follow_ups chain containing the path-typing type action runs to completion;
- the B8 focus-transition settle paces keyboard input sent immediately after a window
  activation (win+r -> type with no settle was the wedge topology).

Real-input tests close every window they open through the shared D6-safe helpers
(marker-scoped launch and teardown; see tests/e2e/helpers_win32.py) and never park
the mouse in a failsafe corner.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from desktop_tripwire import LIVE_DESKTOP

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.desktop.input_engine as input_engine_module
from computer_use_mcp.desktop.backend import LocalComputerBackend
from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
from computer_use_mcp.desktop.input_engine import _INPUT, _key_event, _text_to_key_units
from computer_use_mcp.core.models import GroundedAction

#: The exact incident payload shape: a raw single-backslash Windows path after a verb.
BS = chr(92)  # immune to source-encoding/shell mangling
INCIDENT_TEXT = (
    "notepad C:" + BS + "Users" + BS + "localadmin" + BS + "Desktop" + BS
    + "cortex-bench" + BS + "r1_note.txt"
)


def test_backslash_path_text_produces_complete_unicode_units() -> None:
    """Unit pin: every character (including backslashes) becomes a down+up VK_PACKET pair."""
    units = _text_to_key_units(INCIDENT_TEXT)
    assert len(units) == 2 * len(INCIDENT_TEXT)  # down+up per code unit, nothing dropped
    backslash_events = [u for u in units if u[1] == ord(BS)]
    assert len(backslash_events) == 10  # down+up per backslash (5 backslashes in the payload)


def test_follow_ups_chain_with_backslash_path_type_runs_to_completion() -> None:
    """The driver pattern that stalled pre-fix: [type path, keypress enter] as a batch."""
    import asyncio
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image as PILImage

    from computer_use_mcp.pipeline.agent import ComputerUseAgent
    from computer_use_mcp.core.limits import Limits
    from computer_use_mcp.core.models import ActionSpec
    from computer_use_mcp.vision.observation import ObservationEngine
    from computer_use_mcp.security.safety import SafetyPolicy
    from computer_use_mcp.core.state import StopToken, TaskState

    backend = FakeComputerBackend()

    def _observe_flipped(monitor_index: object = None) -> object:
        observation = FakeComputerBackend.observe(backend, monitor_index)  # type: ignore[arg-type]
        color = "white" if len(backend.executed) % 2 == 0 else "black"
        image = PILImage.new("RGB", (backend.width, backend.height), color)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        import base64

        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation

    backend.observe = _observe_flipped  # type: ignore[method-assign]
    agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    outcome = asyncio.run(
        agent.run_single(
            state,
            GroundedAction(action="type", text=INCIDENT_TEXT, confidence=1.0),
            approved=True,
            follow_ups=[ActionSpec(action="keypress", keys=["enter"])],
        )
    )
    # The chain runs to completion (the incident shape can never hang or half-run).
    assert outcome.kind == "executed"
    assert len(outcome.follow_up_results or []) == 2
    assert [item.action.value for item in backend.executed] == ["type", "keypress"]


@pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="Real Win32 dispatch requires Windows.")
@LIVE_DESKTOP
def test_real_dispatch_of_backslash_path_completes_under_a_second(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REAL dispatch: the incident payload typed into Notepad completes < 1 s.

    R-04 note: integrity verification is disabled for THIS test (monkeypatched) — it
    pins the B5 STALL verdict (the action must RETURN promptly), not landed-text
    integrity, which has its own suite (tests/test_r04_type_integrity.py). On a
    reference machine whose input stack drops keystrokes, an integrity-raising type
    would fail here for reasons unrelated to B5.

    WP12d (VAL-WP12-03): this test tests a STALL, not pacing — the engine runs
    UNPACED for this test only (interval restored by monkeypatch), keeping the
    < 1.0 s bound meaningful against the WP12a default pace. And it is D6-scoped:
    the Notepad instance is launched via the shared marker-scoped helpers
    (``launch_marked_notepad`` + ``release_notepad``) — it can never attach to the
    user's own untitled Notepad (the old unmarked-title attach) and teardown
    closes only our marker window.
    """
    monkeypatch.setattr(backend_module, "TYPE_INTEGRITY_ENABLED", False)
    monkeypatch.setattr(real_backend._engine, "type_interval", 0.0)  # unpaced: stall test
    result: dict[str, object] = {"done": False, "error": None}

    import sys as _sys
    from pathlib import Path as _Path

    _e2e_dir = _Path(__file__).resolve().parent / "e2e"
    if str(_e2e_dir) not in _sys.path:
        _sys.path.insert(0, str(_e2e_dir))
    import helpers_win32 as w32

    token = w32.unique_window_token("t8b5-stall")
    scratch = _Path(__file__).resolve().parent / "e2e" / "scratch" / "t8_b5_backslash"
    scratch.mkdir(parents=True, exist_ok=True)
    marker_file = scratch / f"{token}.txt"
    marker_file.write_text("", encoding="utf-8")

    def worker() -> None:
        proc = None
        hwnd = None
        try:
            proc, hwnd = w32.launch_marked_notepad(
                w32.Deadline(30.0), marker_file, timeout_s=30.0
            )
            # D6 right-window proof: OUR marker window is focused before typing.
            assert w32.focus_window(hwnd), "could not focus the marker Notepad"
            assert token.casefold() in w32.window_text(hwnd).casefold(), w32.window_text(hwnd)
            time.sleep(0.6)  # the driver-protocol settle after activation
            # WP12e MINOR: re-check IMMEDIATELY before the dispatch — if focus moved
            # during the settle, the 60-char burst must NOT be sent at all.
            assert int(w32.user32.GetForegroundWindow() or 0) == hwnd, (
                "focus moved during the settle; refusing to type"
            )
            assert token.casefold() in w32.window_text(hwnd).casefold(), (
                "marker tab no longer active after the settle; refusing to type"
            )
            action = GroundedAction(action="type", text=INCIDENT_TEXT, confidence=1.0)
            started = time.perf_counter()
            real_backend.execute(action, None)
            result["elapsed"] = time.perf_counter() - started
            result["done"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced by the watchdog below
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if proc is not None and hwnd is not None:
                w32.release_notepad(proc, hwnd, token)
            elif proc is not None:
                w32.kill_process_tree(proc.pid)
            # WP12e NIT: the scratch file is deleted after the run (it is
            # git-ignored, tests/e2e/scratch/.gitignore).
            try:
                marker_file.unlink(missing_ok=True)
            except OSError:
                pass  # best-effort: the file is git-ignored and marker-named

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert result["error"] is None, f"B5 real-dispatch worker failed: {result['error']}"
    assert result["done"] is True, "B5 STALL REPRODUCED: the path type did not finish in 30 s"
    assert result["elapsed"] < 1.0, f"path typing took {result['elapsed']:.2f}s (must be <1s)"


# --- WP-H offline twin of the gated live dispatch ----------------------------------------------------
#
# The live test above (LIVE_DESKTOP-gated) launches a real Notepad and types through
# the real SendInput engine; the offline suite must never do that. What it pins that
# IS offline-provable — the exact incident payload driven through the REAL
# SendInputEngine.type_text batching path completes promptly and delivers the full
# down+up VK_PACKET INPUT array — is pinned here with the OS call stubbed at the
# LOWEST seam (backend_module._user32.SendInput; array construction, chunking,
# failsafe check and pacing all stay real).


class _RecordingSendInputUser32:
    """user32 stand-in: records SendInput batches AND serves the failsafe pre-check.

    ``GetCursorPos``/``GetSystemMetrics`` feed ``get_cursor_position`` /
    ``_primary_screen_size`` — so the engine's REAL failsafe corner check runs
    against a controlled cursor (screen 1920x1080, cursor at 960, 540 = no corner).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, bytes]] = []

    def SendInput(self, count: int, array: Any, size: int) -> int:
        self.calls.append((count, bytes(array)))
        return count

    def GetCursorPos(self, lp_point: Any) -> int:
        point = lp_point._obj
        point.x, point.y = 960, 540  # center: NOT a failsafe corner
        return 1

    def GetSystemMetrics(self, index: int) -> int:
        return (1920, 1080)[index]


def test_offline_twin_backslash_path_through_real_sendinput_batching_completes_under_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What this OFFLINE twin proves: the exact incident payload, driven through the
    REAL ``SendInputEngine.type_text`` production path (chunking, failsafe pre-check
    served by this fake's cursor/screen, per-chunk pacing), completes promptly
    (<1 s) and delivers the complete INPUT array in ONE batched SendInput call —
    one down+up VK_PACKET pair per code unit, byte-identical to ``_key_event`` over
    ``_text_to_key_units``, backslashes included.

    What ONLY the LIVE_DESKTOP-gated real-dispatch node proves: the B5 activation
    race against a REAL window (win+r/activation topology) — a real OS stall can
    never be reproduced with the OS call stubbed, by construction.
    """
    fake_user32 = _RecordingSendInputUser32()
    monkeypatch.setattr(input_engine_module, "_user32", fake_user32)
    engine = backend_module.SendInputEngine()

    started = time.perf_counter()
    engine.type_text(INCIDENT_TEXT)
    elapsed = time.perf_counter() - started

    units = _text_to_key_units(INCIDENT_TEXT)
    expected_array = (_INPUT * len(units))(*[_key_event(vk, scan, flags) for vk, scan, flags in units])
    assert len(fake_user32.calls) == 1, (
        f"expected ONE batched SendInput call, got {len(fake_user32.calls)}"
    )
    count, raw = fake_user32.calls[0]
    assert count == 2 * len(INCIDENT_TEXT)  # down+up per code unit, nothing dropped
    assert raw == bytes(expected_array)  # the exact INPUT array, backslashes included
    assert elapsed < 1.0, f"offline batching took {elapsed:.3f}s (must be <1s)"
