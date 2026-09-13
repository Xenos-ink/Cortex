"""R-20: re-anchor causality — only session-owned surfaces may become the anchor.

Red-team finding RT2-2: ``reanchor_after_success`` accepted any titled foreground
window whenever the anchor was a launcher/dialog surface (or the anchor was dead),
so an unrelated window that won the foreground race after a verified action was
ADOPTED as the session target. The causality fix: adoption requires membership in
the session's launched/attached set (every hwnd the session ever bound), a
same-process descendant (pid match or GW_OWNER chain), or the session's own keyboard
launch act (a chord dispatched into a launcher/dialog anchor — the Win+R doctrine).
Everything else is refused with the named ``REANCHOR_REFUSED`` payload (audited,
annotation-only) and the anchor is KEPT. Stubbed enumeration only — no live input.
"""

from __future__ import annotations

from typing import Any

from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import REANCHOR_REFUSED, parse_interference
from computer_use_mcp.models import GroundedAction, WindowInfo

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel",
)
ADVERSARY = WindowInfo(
    hwnd=9, pid=900, process_name="evil.exe", window_class="Stealth", title="Innocent Notes",
)
RUN = WindowInfo(hwnd=5, pid=500, process_name="explorer.exe", window_class="#32770", title="Run")
LAUNCHED = WindowInfo(hwnd=12, pid=501, process_name="notepad.exe", window_class="Notepad", title="Untitled - Notepad")
SIBLING_DIALOG = WindowInfo(hwnd=7, pid=100, process_name="EXCEL.EXE", window_class="bosa_sdm_XL9", title="Microsoft Excel")
FOREIGN_OWNED = WindowInfo(hwnd=8, pid=777, process_name="vendor.exe", window_class="VendorDlg", title="Vendor modal")

CLICK = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)
ENTER = GroundedAction(action="keypress", keys=["enter"], confidence=1.0)


class _AuditedBackend(FakeComputerBackend):
    """Fake backend that records which windows are alive + GW_OWNER ownership pairs."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_windows([TARGET])


def _guard(backend: FakeComputerBackend) -> tuple[InterferenceGuard, list[tuple[str, str]]]:
    """Armed guard bound to TARGET plus a captured (audit result, event payload) log."""
    events: list[tuple[str, str]] = []

    def _emit(*args: Any, **kwargs: Any) -> None:
        metadata = kwargs.get("metadata") or {}
        events.append((str(kwargs.get("result", "")), str(metadata.get("payload", ""))))

    guard = InterferenceGuard(backend, parse_interference(None), emit=_emit)
    guard.rebind(TARGET)
    return guard, events


def _refusals(events: list[tuple[str, str]]) -> list[str]:
    return [event for result, event in events if result == "reanchor_refused"]


def test_adversarial_foreign_window_is_not_adopted() -> None:
    """The accept-test: a foreign-titled window that appears after a VERIFIED action
    (anchor alive, no causality) is refused by name; the anchor is untouched."""
    backend = _AuditedBackend()
    guard, events = _guard(backend)
    backend.set_active_window(ADVERSARY)
    guard.reanchor_after_success(ADVERSARY)
    assert guard.bound is not None and guard.bound.hwnd == TARGET.hwnd
    refusals = _refusals(events)
    assert len(refusals) == 1 and refusals[0].startswith(REANCHOR_REFUSED)
    assert "refused_title='Innocent Notes'" in refusals[0]
    # steal protection stays intact: the next dispatch rejects with FOCUS_TAKEN_BY
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith("FOCUS_TAKEN_BY")


def test_launcher_anchor_without_launch_act_refuses_adoption() -> None:
    """The RT2-2 hole: a launcher/dialog anchor alone is NOT causality — without the
    session's own keyboard launch act, the takeover is refused."""
    backend = _AuditedBackend()
    guard, events = _guard(backend)
    guard.rebind(RUN)  # launcher-anchored session
    backend.set_active_window(ADVERSARY)
    guard.reanchor_after_success(ADVERSARY)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd
    assert any(event.startswith(REANCHOR_REFUSED) for event in _refusals(events))


def test_launcher_anchor_with_own_launch_act_adopts_the_launched_window() -> None:
    """Positive: the Win+R doctrine — a chord into the launcher anchor, then the
    launched app takes over; the adoption follows the session's own act."""
    backend = _AuditedBackend()
    guard, events = _guard(backend)
    guard.rebind(RUN)
    backend.set_active_window(RUN)
    assert guard.verify_pre_dispatch(ENTER) is None  # the launch act arms the marker
    guard.reanchor_after_success(LAUNCHED)
    assert guard.bound is not None and guard.bound.hwnd == LAUNCHED.hwnd
    assert _refusals(events) == []


def test_launch_act_marker_is_consumed_by_one_decision() -> None:
    """The causality window is ONE action: after an adoption, a second unrelated
    takeover is refused (no stale launch act)."""
    backend = _AuditedBackend()
    guard, events = _guard(backend)
    guard.rebind(RUN)
    backend.set_active_window(RUN)
    guard.verify_pre_dispatch(ENTER)
    guard.reanchor_after_success(LAUNCHED)  # consumes the marker
    backend.set_active_window(ADVERSARY)
    guard.reanchor_after_success(ADVERSARY)
    assert guard.bound is not None and guard.bound.hwnd == LAUNCHED.hwnd
    assert any(event.startswith(REANCHOR_REFUSED) for event in _refusals(events))


def test_click_after_chord_clears_the_launch_act() -> None:
    """An unrelated verified dispatch between the chord and the takeover breaks the
    causal chain — the adoption is refused even from a launcher anchor."""
    backend = _AuditedBackend()
    guard, events = _guard(backend)
    guard.rebind(RUN)
    backend.set_active_window(RUN)
    guard.verify_pre_dispatch(ENTER)  # arms
    guard.verify_pre_dispatch(CLICK)  # clears: the chord is no longer the last act
    backend.set_active_window(ADVERSARY)
    guard.reanchor_after_success(ADVERSARY)
    assert guard.bound is not None and guard.bound.hwnd == RUN.hwnd
    assert any(event.startswith(REANCHOR_REFUSED) for event in _refusals(events))


def test_previously_bound_session_window_is_re_adopted() -> None:
    """Positive: a window the session explicitly attached earlier (alt-tab back to it
    after a verified action) is in the launched/attached set — adopted."""
    backend = _AuditedBackend()
    guard, events = _guard(backend)
    guard.rebind(LAUNCHED)  # an earlier explicit attach joined the set
    guard.rebind(TARGET)  # the session moved back to the anchor
    guard.reanchor_after_success(LAUNCHED)
    assert guard.bound is not None and guard.bound.hwnd == LAUNCHED.hwnd
    assert _refusals(events) == []


def test_same_process_descendant_is_adopted() -> None:
    """Positive: the app's own dialog (same pid, different hwnd/class) is a
    same-process descendant of a session surface — adopted without any launch act."""
    backend = _AuditedBackend()
    guard, events = _guard(backend)
    backend.set_active_window(SIBLING_DIALOG)
    guard.reanchor_after_success(SIBLING_DIALOG)
    assert guard.bound is not None and guard.bound.hwnd == SIBLING_DIALOG.hwnd
    assert _refusals(events) == []


def test_owner_chained_cross_process_dialog_is_adopted() -> None:
    """Positive: a cross-process GW_OWNER chain rooting at a session hwnd (the B11
    shared probe) counts as a descendant of the session's surface."""
    backend = _AuditedBackend()
    backend.owned_windows = {(FOREIGN_OWNED.hwnd, TARGET.hwnd)}
    guard, events = _guard(backend)
    backend.set_active_window(FOREIGN_OWNED)
    guard.reanchor_after_success(FOREIGN_OWNED)
    assert guard.bound is not None and guard.bound.hwnd == FOREIGN_OWNED.hwnd
    assert _refusals(events) == []


def test_dead_anchor_no_longer_adopts_an_arbitrary_successor() -> None:
    """The second non-causal hole (old anchor-gone rule): a dead anchor must NOT hand
    the session to whoever took the foreground. The refusal keeps the anchor; the next
    pre-dispatch then reports TARGET_GONE and unbinds (no deadlock) so the driver
    reattaches EXPLICITLY by identity."""
    backend = _AuditedBackend()
    backend.set_windows([])  # TARGET is gone
    guard, events = _guard(backend)
    backend.set_active_window(ADVERSARY)
    guard.reanchor_after_success(ADVERSARY)
    assert guard.bound is not None and guard.bound.hwnd == TARGET.hwnd  # anchor KEPT
    assert any(event.startswith(REANCHOR_REFUSED) for event in _refusals(events))
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.event.startswith("TARGET_GONE")
    assert guard.armed is False  # unbind_and_report default: re-grounding proceeds


def test_untitled_window_is_never_adopted_and_stays_silent() -> None:
    """B10 (c) unchanged: an anonymous surface is never anchored (no refusal event —
    the existing rule short-circuits before the causality decision)."""
    backend = _AuditedBackend()
    untitled = WindowInfo(hwnd=30, pid=900, process_name="evil.exe", window_class="X", title="")
    guard, events = _guard(backend)
    guard.reanchor_after_success(untitled)
    assert guard.bound is not None and guard.bound.hwnd == TARGET.hwnd
    assert _refusals(events) == []
