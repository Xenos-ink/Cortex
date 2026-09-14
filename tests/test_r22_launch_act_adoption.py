"""R-22 per-acceptance-criterion regression corpus: launch-act adoption tightening.

Acceptance criteria (ROADMAP.md ``## P0 — v0.7.0``, R-22):

- **AC-22a**: a foreign window immediately following a keypress into a ``#32770`` /
  ``explorer.exe`` anchor is refused as the session anchor, with the named
  ``REANCHOR_REFUSED`` payload, the anchor KEPT. Covered here in the keypress-letter,
  explorer-process-branch, and hotkey (non-commit) forms, plus the two causal-joint
  pins the fix adds: a REJECTED chord never arms the launch-act marker (D-1), and a
  SEEDED launch act adopts only a seed-correlated candidate (D-2).
- **AC-22b**: R-20's documented positive adoption paths still work — the Win+R
  positive control, the one-action marker consumption, the seedless commit-key shape,
  the dead-anchor TARGET_GONE -> unbind chain, and (executed cross-file) the
  unmodified ``tests/test_r20_reanchor_causality.py`` suite.

In-process only, ``FakeComputerBackend``, mirroring the agent's exact call order
(``verify_pre_dispatch`` -> verified -> ``reanchor_after_success``; agent.py wiring),
the same harness shape as ``tests/test_r20_reanchor_causality.py`` (that file is a
frozen AC-U7 pin and is NOT modified — its suite is executed cross-file below).

ADJUDICATED RESIDUAL (D5, r22-root-cause §6.3): a seedless COMMIT-KEY chord (enter)
followed by an unrelated titled foreign window is evidence-identical in-process to the
frozen R-20 positive control (test 3) — any gate refusing it would break the frozen
AC-22b pin. It stays adoptable BY ADJUDICATION and is pinned as such below with the
residual comment; separation requires spawn-time evidence (a backend probe, outside
this mission's file set).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import REANCHOR_REFUSED, parse_interference
from computer_use_mcp.models import GroundedAction, WindowInfo

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN = WindowInfo(hwnd=5, pid=500, process_name="explorer.exe", window_class="#32770", title="Run")
EXPLORER_CAB = WindowInfo(
    hwnd=6, pid=500, process_name="explorer.exe", window_class="CabinetWClass", title="Downloads",
)
EVIL = WindowInfo(
    hwnd=9, pid=555, process_name="chrome.exe", window_class="Chrome_WidgetWin_1", title="Evil Tab",
)
LAUNCHED = WindowInfo(
    hwnd=12, pid=501, process_name="notepad.exe", window_class="Notepad", title="Untitled - Notepad",
)
DOCS = WindowInfo(
    hwnd=20, pid=502, process_name="explorer.exe", window_class="CabinetWClass", title="Documents",
)
TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel",
)

TYPE_NOTEPAD = GroundedAction(action="type", text="notepad", confidence=1.0)
TYPE_PATH = GroundedAction(action="type", text="C:/Users/me/Documents", confidence=1.0)
KEYPRESS_A = GroundedAction(action="keypress", keys=["a"], confidence=1.0)
KEYPRESS_ENTER = GroundedAction(action="keypress", keys=["enter"], confidence=1.0)
HOTKEY_NON_COMMIT = GroundedAction(action="hotkey", keys=["ctrl", "shift", "f5"], confidence=1.0)
CLICK = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)


def _guard(backend: FakeComputerBackend) -> tuple[InterferenceGuard, list[tuple[str, str]]]:
    """Guard bound to RUN plus a captured (audit result, event payload) log."""
    events: list[tuple[str, str]] = []

    def _emit(*args: Any, **kwargs: Any) -> None:
        metadata = kwargs.get("metadata") or {}
        events.append((str(kwargs.get("result", "")), str(metadata.get("payload", ""))))

    guard = InterferenceGuard(backend, parse_interference(None), emit=_emit)
    guard.rebind(RUN)
    return guard, events


def _refusals(events: list[tuple[str, str]]) -> list[str]:
    return [payload for result, payload in events if result == "reanchor_refused"]


# --- AC-22a: the reported vector (non-commit keyboard acts) must never adopt --------------------


def test_ac22a_keypress_letter_into_32770_anchor_refuses_foreign_window() -> None:
    """AC-22a verbatim vector (RT-E8-05 S1): keypress 'a' into a #32770 anchor must NOT
    arm the launch-act marker, so the next titled foreign window is refused by name, the
    anchor is KEPT, and the foreign foreground keeps being REJECTED for dispatch."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    assert guard.verify_pre_dispatch(KEYPRESS_A) is None  # the chord itself is innocuous
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd  # anchor KEPT
    refusals = _refusals(events)
    assert len(refusals) == 1 and refusals[0].startswith(REANCHOR_REFUSED)
    assert "refused_title='Evil Tab'" in refusals[0]
    # the post-adoption hijack stays closed: the foreign foreground is still rejected
    backend.set_active_window(EVIL)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith("FOCUS_TAKEN_BY")


def test_ac22a_explorer_process_branch_anchor_refuses_foreign_window() -> None:
    """AC-22a process branch (S2): the transient-launcher test also matches explorer.exe
    surfaces with a non-dialog class — the refusal must hold there too."""
    backend = FakeComputerBackend()
    backend.set_windows([EXPLORER_CAB])
    backend.set_active_window(EXPLORER_CAB)
    events: list[tuple[str, str]] = []

    def _emit(*args: Any, **kwargs: Any) -> None:
        metadata = kwargs.get("metadata") or {}
        events.append((str(kwargs.get("result", "")), str(metadata.get("payload", ""))))

    guard = InterferenceGuard(backend, parse_interference(None), emit=_emit)
    guard.rebind(EXPLORER_CAB)
    assert guard.verify_pre_dispatch(KEYPRESS_A) is None
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == EXPLORER_CAB.hwnd
    refusals = _refusals(events)
    assert len(refusals) == 1 and refusals[0].startswith(REANCHOR_REFUSED)


def test_ac22a_hotkey_non_commit_form_refuses_foreign_window() -> None:
    """AC-22a hotkey form (S3): a non-commit HOTKEY into the launcher anchor is not a
    launch act either — the takeover is refused."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    assert guard.verify_pre_dispatch(HOTKEY_NON_COMMIT) is None
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd
    assert any(payload.startswith(REANCHOR_REFUSED) for payload in _refusals(events))


def test_ac22a_rejected_chord_is_never_a_launch_act() -> None:
    """D-1 causal-joint pin (S5): a chord REJECTED by the foreground gate never
    dispatched, so it can never arm the launch-act marker — the takeover that follows
    (with no intervening pre-dispatch) must be REFUSED. Pre-fix this exact sequence
    ADOPTED the foreign window (arming preceded the gates)."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    backend.set_active_window(EVIL)  # the foreground is occupied by the foreign window
    verdict = guard.verify_pre_dispatch(KEYPRESS_A)
    assert verdict is not None and verdict.blocking  # REJECT[FOCUS_TAKEN_BY]
    assert verdict.event.startswith("FOCUS_TAKEN_BY")
    # no verified dispatch happened — the marker must be unset (behavioral probe: the
    # next reanchor decision must refuse; pre-fix the armed marker adopted here)
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd
    assert any(payload.startswith(REANCHOR_REFUSED) for payload in _refusals(events))


# --- R-22 D-2: seed <-> outcome correlation for Path-B adoption ---------------------------------


def test_d2_seeded_enter_adopts_the_matching_candidate() -> None:
    """D-2 positive: type 'notepad' + enter into the Run dialog -> the launched notepad
    surface (process/title name the seed) is adopted."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    assert guard.verify_pre_dispatch(TYPE_NOTEPAD) is None  # TYPE records the seed
    assert guard.verify_pre_dispatch(KEYPRESS_ENTER) is None  # the commit key arms
    guard.reanchor_after_success(LAUNCHED)
    assert guard.bound is not None and guard.bound.hwnd == LAUNCHED.hwnd
    assert _refusals(events) == []


def test_d2_seeded_enter_refuses_the_unrelated_candidate() -> None:
    """D-2 negative (the new causal joint): type 'notepad' + enter, but an UNRELATED
    window ('Evil Tab', chrome.exe) takes over — nothing names the typed seed, so the
    adoption is REFUSED with the named payload and the anchor is kept."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    guard.verify_pre_dispatch(TYPE_NOTEPAD)
    guard.verify_pre_dispatch(KEYPRESS_ENTER)
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd
    refusals = _refusals(events)
    assert len(refusals) == 1 and refusals[0].startswith(REANCHOR_REFUSED)
    assert "refused_title='Evil Tab'" in refusals[0]


def test_d2_path_seed_adopts_the_matching_folder_surface() -> None:
    """D-2 with a PATH seed: 'C:/Users/me/Documents' + enter -> a surface whose title
    names a typed token ('Documents') is adopted (tokens split at separators)."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    assert guard.verify_pre_dispatch(TYPE_PATH) is None
    assert guard.verify_pre_dispatch(KEYPRESS_ENTER) is None
    guard.reanchor_after_success(DOCS)
    assert guard.bound is not None and guard.bound.hwnd == DOCS.hwnd
    assert _refusals(events) == []


def test_d2_rejected_type_records_no_seed() -> None:
    """D-2 causal-joint pin: a TYPE that never dispatched (foreground gate rejection)
    records NO launcher seed. White-box pin on the bounded token set (comment: the
    behavioral projection — a later commit key adopting per the seedless legacy shape —
    is pinned by test_residual_seedless_commit_key below; asserting it here would make
    this row evidence-identical to that residual)."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, _events = _guard(backend)
    backend.set_active_window(EVIL)  # the foreground is occupied
    verdict = guard.verify_pre_dispatch(TYPE_NOTEPAD)
    assert verdict is not None and verdict.blocking
    assert guard._launcher_seed == frozenset()


def test_d2_seed_is_cleared_when_the_anchor_moves() -> None:
    """D-2 hygiene: the seed belongs to the launcher surface just left — an adoption
    (rebind) clears it, so a stale seed can never correlate a LATER takeover."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, _events = _guard(backend)
    guard.verify_pre_dispatch(TYPE_NOTEPAD)
    assert guard._launcher_seed != frozenset()
    guard.verify_pre_dispatch(KEYPRESS_ENTER)
    guard.reanchor_after_success(LAUNCHED)  # adoption rebinds -> seed must be cleared
    assert guard.bound is not None and guard.bound.hwnd == LAUNCHED.hwnd
    assert guard._launcher_seed == frozenset()


# --- AC-22b: positive controls (R-20 paths must keep working) -----------------------------------


def test_ac22b_win_r_positive_control_still_adopts() -> None:
    """AC-22b positive control: the Win+R doctrine shape (frozen R-20 test 3) — a
    launcher-COMMIT keypress (enter) into the Run dialog anchor, then the launched app
    takes over — ADOPTS."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    assert guard.verify_pre_dispatch(KEYPRESS_ENTER) is None
    guard.reanchor_after_success(LAUNCHED)
    assert guard.bound is not None and guard.bound.hwnd == LAUNCHED.hwnd
    assert _refusals(events) == []


def test_ac22b_one_action_marker_consumption_bound_holds() -> None:
    """AC-22b: the causality window stays ONE action — after an adoption, a second
    unrelated takeover with no new chord is refused."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    guard.verify_pre_dispatch(KEYPRESS_ENTER)
    guard.reanchor_after_success(LAUNCHED)  # consumes the marker
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == LAUNCHED.hwnd
    assert any(payload.startswith(REANCHOR_REFUSED) for payload in _refusals(events))


def test_ac22b_r20_reanchor_causality_suite_green_unmodified() -> None:
    """AC-22b / AC-U7 cross-file proof: the UNMODIFIED R-20 causality suite (the
    executable definition of the documented-correct paths) passes on this tree."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            str(REPO_ROOT / "tests" / "test_r20_reanchor_causality.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,  # the return code IS the assertion below
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ac22b_intervening_click_still_breaks_the_causal_chain() -> None:
    """AC-22b: a verified non-keyboard dispatch between the commit key and the takeover
    still clears the launch-act marker (the R-20 one-action doctrine, post-fix)."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    guard.verify_pre_dispatch(KEYPRESS_ENTER)  # arms
    guard.verify_pre_dispatch(CLICK)  # clears: the chord is no longer the last act
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd
    assert any(payload.startswith(REANCHOR_REFUSED) for payload in _refusals(events))


# --- dead-anchor path (R-20's solved problem must stay intact) ----------------------------------


def test_dead_anchor_target_gone_unbind_then_reattach_only() -> None:
    """Dead-anchor chain intact: a refusal against a DEAD anchor keeps the anchor; the
    next pre-dispatch reports TARGET_GONE and UNBINDS (no deadlock); the guard is then
    DORMANT (dispatches proceed without an anchor) and re-arms ONLY by explicit
    identity reattach (``maybe_bind`` with the session allowlist)."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    backend.set_windows([])  # the anchor dies
    backend.set_active_window(EVIL)
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd  # anchor KEPT
    assert any(payload.startswith(REANCHOR_REFUSED) for payload in _refusals(events))
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.event.startswith("TARGET_GONE")
    assert guard.armed is False  # unbind_and_report default: back to DORMANT
    # dormant dispatch proceeds (no arbitrary successor was ever adopted):
    assert guard.verify_pre_dispatch(CLICK) is None
    # re-arming is EXPLICIT: the allowlisted observation rebinds by identity
    backend.set_windows([TARGET])
    backend.set_active_window(TARGET)
    guard.maybe_bind(backend.observe(), ["excel.exe"])
    assert guard.armed is True and guard.bound is not None and guard.bound.hwnd == TARGET.hwnd


# --- adjudicated residual (D5): seedless commit key + unrelated foreign window ------------------


def test_residual_seedless_commit_key_unrelated_foreign_window_adopts() -> None:
    """RESIDUAL ROW — pinned CURRENT behavior, NOT a desired outcome.

    A seedless COMMIT-KEY chord (enter, nothing typed) followed by an unrelated titled
    foreign window is, on in-process evidence, IDENTICAL to the frozen R-20 positive
    control (``test_launcher_anchor_with_own_launch_act_adopts_the_launched_window``:
    titled candidate, unrelated pid/class/owner, immediate succession, no seed). Per
    the D5 adjudication (r22-root-cause.md §6.3) this shape stays ADOPTABLE; any gate
    refusing it would break the frozen AC-22b pin. Separation requires spawn-time
    evidence (a backend process probe — outside the mission file set, routed to the
    Commander as a post-mission hardening path). D-1 + D-2 close the entire
    non-commit-key class and the seeded-enter class around this residual."""
    backend = FakeComputerBackend()
    backend.set_windows([RUN])
    backend.set_active_window(RUN)
    guard, events = _guard(backend)
    assert guard.verify_pre_dispatch(KEYPRESS_ENTER) is None
    guard.reanchor_after_success(EVIL)
    assert guard.bound is not None and guard.bound.hwnd == EVIL.hwnd  # legacy R-20 shape
    assert _refusals(events) == []
    # the one-action bound still caps the blast radius at ONE adoption:
    guard.reanchor_after_success(DOCS)
    assert guard.bound is not None and guard.bound.hwnd == EVIL.hwnd
    assert any(payload.startswith(REANCHOR_REFUSED) for payload in _refusals(events))
