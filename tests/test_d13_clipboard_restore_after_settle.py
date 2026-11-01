"""D13 / repair 3.2 — the paste is given time to land BEFORE the clipboard is restored.

``backend._execute_type_via_clipboard`` restored the saved clipboard in the statement
immediately after ``engine.chord(["ctrl", "v"])`` — measured **5 µs** later — while the
transport's own settle budget ``TYPE_VERIFY_SETTLE_SECONDS = 0.05`` was slept
**AFTER** the restore.  ``SendInputEngine.chord`` only QUEUES keystrokes, so a target
that reads the clipboard even slightly late pastes the user's OLD text.  The window is
zero-width by construction, so it loses every time.

The fix moves the restore AFTER the settle.  The save -> set -> paste ordering itself is
correct today and MUST NOT change; only the rollback's position moves, and the ``finally``
is kept for the abort paths (an exception between the set and the paste must still
restore).

THE TEST DOUBLE.  ``FakeComputerBackend._simulate_type_clipboard`` recorded the LOCAL
``text`` variable instead of reading the simulated clipboard at delivery time, so it was
structurally incapable of detecting this class of defect — 141 typing tests passed
unchanged against a broken restore order.  This module's doubles read the clipboard
**at delivery time** (or on the real in-repo fake, which now does the same), and the
tests below drive the REAL production method with the two OS-clipboard primitives
replaced in memory.  No real clipboard is ever touched.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.desktop.clipboard as clipboard_module
from computer_use_mcp.desktop import backend as B
from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
from computer_use_mcp.core.state import StopToken
from computer_use_mcp.desktop.backend import TYPE_VERIFY_SETTLE_SECONDS
from computer_use_mcp.core.models import ActionType, GroundedAction

# --- the in-memory OS clipboard ----------------------------------------------------------------


class InMemoryClipboard:
    """A recording stand-in for the two OS-clipboard primitives the transport uses."""

    def __init__(self, value: str | None = "") -> None:
        self.value = value
        self.ops: list[tuple[str, str, float]] = []
        self.fail_writes: set[str] = set()

    def read_text(self) -> str | None:
        self.ops.append(("READ", repr(self.value), time.perf_counter()))
        return self.value

    def verdict(self) -> tuple[str | None, str]:
        """F3/3.3: production saves through the three-way verdict, so the double does too."""
        self.ops.append(("READ", repr(self.value), time.perf_counter()))
        return (self.value, "restorable" if self.value is not None else "non_text")

    def write_text(self, text: str) -> bool:
        self.ops.append(("WRITE", repr(text), time.perf_counter()))
        if text in self.fail_writes:
            return False
        self.value = text
        return True

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _text, _at in self.ops]

    def stamps(self, kind: str) -> list[float]:
        return [at for k, _t, at in self.ops if k == kind]


# --- the simulated target application ------------------------------------------------------------


class LateReadingApp:
    """A paste consumer that reads the clipboard when the app PROCESSES the chord.

    ``chord()`` returns as soon as the keystrokes are QUEUED (exactly like SendInput).
    The app then needs ``app_latency_s`` of wall time before it reads the clipboard —
    this is the shape that made the pre-fix restore lose the race.
    """

    def __init__(self, clipboard: InMemoryClipboard, app_latency_s: float = 0.0) -> None:
        self.clipboard = clipboard
        self.app_latency_s = app_latency_s
        self.pasted: list[str] = []
        self.control = ""
        self.raise_on_chord: Exception | None = None

    def chord(self, keys: list[str]) -> None:
        self.clipboard.ops.append(("CHORD", "+".join(keys), time.perf_counter()))
        if self.app_latency_s > 0:
            time.sleep(self.app_latency_s)
        self.pasted.append(self.clipboard.value or "")
        self.control += self.pasted[-1]
        if self.raise_on_chord is not None:
            raise self.raise_on_chord


class _Engine:
    """The InputEngine surface the transport touches: ``chord`` only."""

    def __init__(self, app: LateReadingApp) -> None:
        self._app = app

    def chord(self, keys: list[str]) -> None:
        self._app.chord(keys)

    def release_modifiers(self, keys: list[str]) -> None:  # pragma: no cover
        pass

    def type_text(self, text: str, before_chunk: Any = None) -> None:  # pragma: no cover
        raise AssertionError("per-character engine must not be used by the clipboard transport")


def _action(text: str) -> GroundedAction:
    return GroundedAction(action=ActionType.TYPE, text=text, via="clipboard", confidence=1.0)


def run_transport(
    text: str,
    *,
    prior: str | None = "OLD-USER-TEXT",
    app_latency_s: float = 0.0,
    stop: StopToken | None = None,
    chord_raises: Exception | None = None,
) -> tuple[str, InMemoryClipboard, LateReadingApp, Exception | None]:
    """Drive the REAL ``LocalComputerBackend._execute_type_via_clipboard`` once."""
    clipboard = InMemoryClipboard(prior)
    app = LateReadingApp(clipboard, app_latency_s=app_latency_s)
    app.raise_on_chord = chord_raises

    instance = object.__new__(B.LocalComputerBackend)
    instance._engine = _Engine(app)  # type: ignore[attr-defined]
    instance._last_key_dispatch = 0.0
    instance._last_focus_transition = 0.0
    instance._semantic_reader = None
    instance.read_focused_control_value = lambda: app.control  # type: ignore[method-assign]
    instance._apply_focus_settle = lambda: None  # type: ignore[method-assign]
    instance._apply_key_dispatch_gap = lambda keys: None  # type: ignore[method-assign]

    saved = (
        B._clipboard_write_text,
        B._clipboard_read_verdict,
    )
    B._clipboard_write_text = clipboard.write_text  # type: ignore[assignment]
    # F3 fix 3.3: production saves through the three-way verdict, so the double does too.
    B._clipboard_read_verdict = clipboard.verdict  # type: ignore[assignment]
    raised: Exception | None = None
    message = ""
    try:
        message = instance._execute_type_via_clipboard(_action(text), stop, None)
    except Exception as exc:  # noqa: BLE001 - a raise is a decisive observation
        raised = exc
    finally:
        (
            B._clipboard_write_text,
            B._clipboard_read_verdict,
        ) = saved  # type: ignore[assignment]
    return message, clipboard, app, raised


# --- the headline: a late reader receives the INTENDED text --------------------------------------


@pytest.mark.parametrize("latency_ms", [1, 5, 25, 49])
def test_a_late_reader_receives_the_intended_text(latency_ms: int) -> None:
    """The repro's D13-9 shape: the app reads the clipboard *after* the chord is queued."""
    _message, clipboard, app, raised = run_transport("INTENDED", app_latency_s=latency_ms / 1000.0)

    assert raised is None
    assert app.pasted == ["INTENDED"], (
        f"the target read the clipboard {latency_ms} ms after the chord and pasted "
        f"{app.pasted[0]!r} instead of the intended text"
    )
    # The user's clipboard IS restored by the end of the action (unchanged contract).
    assert clipboard.value == "OLD-USER-TEXT"


def test_the_restore_happens_after_the_settle_sleep() -> None:
    """The ordering IS the fix: chord -> settle -> restore, not chord -> restore."""
    _message, clipboard, _app, _raised = run_transport("INTENDED")

    kinds = clipboard.kinds
    assert kinds == ["READ", "WRITE", "CHORD", "WRITE"]
    chord_at = clipboard.stamps("CHORD")[0]
    restore_at = clipboard.stamps("WRITE")[-1]
    assert restore_at - chord_at >= TYPE_VERIFY_SETTLE_SECONDS * 0.9


def test_the_save_set_paste_ordering_itself_is_unchanged() -> None:
    """Only the ROLLBACK moves: save -> set -> paste must stay in that order."""
    _message, clipboard, _app, _raised = run_transport("INTENDED")

    assert clipboard.kinds[:3] == ["READ", "WRITE", "CHORD"]
    assert clipboard.ops[0][1] == repr("OLD-USER-TEXT")  # the save read the old text
    assert clipboard.ops[1][1] == repr("INTENDED")  # the set wrote the new text


# --- no regression across the corpora -----------------------------------------------------------


@pytest.mark.parametrize(
    "corpus",
    [
        "plain ascii",
        "café – naïve",
        "中文文本",
        "مرحبا بالعالم",
        "🎉🚀 emoji astral",
        "mixed 中文 café 🚀 – " * 20,
        "W" * 2000,
        "x\ny\r\nz\ttab",
    ],
)
def test_a_synchronous_reader_still_receives_the_text(corpus: str) -> None:
    _message, clipboard, app, raised = run_transport(corpus)

    assert raised is None
    assert app.pasted == [corpus]
    assert clipboard.value == "OLD-USER-TEXT"


@pytest.mark.parametrize(
    "corpus",
    ["café – 中文 🎉", "中文文本" * 50, "👨‍👩‍👧‍👦 family ZWJ astral", "ascii only", "M" * 2000],
)
def test_a_moderately_late_reader_still_receives_the_text(corpus: str) -> None:
    """A 5 ms consumer — the realistic case the settle is sized for."""
    _message, _clipboard, app, raised = run_transport(corpus, app_latency_s=0.005)

    assert raised is None
    assert app.pasted == [corpus]


# --- the abort paths still restore ----------------------------------------------------------------


def test_an_exception_between_the_set_and_the_paste_still_restores() -> None:
    """The ``finally`` is kept: an abort must not leave the agent's text on the clipboard."""
    _message, clipboard, _app, raised = run_transport(
        "PASTE-ME", chord_raises=RuntimeError("engine chord exploded")
    )

    assert raised is not None
    assert clipboard.value == "OLD-USER-TEXT"


def test_a_stopped_token_after_the_set_still_restores() -> None:
    token = StopToken()
    token.stop()
    _message, clipboard, _app, raised = run_transport("PASTE-ME", stop=token)

    assert raised is not None
    assert clipboard.value == "OLD-USER-TEXT"


def test_an_unreadable_clipboard_is_never_restored_toward() -> None:
    """``prior is None`` (nothing restorable): no restore write, no annotation."""
    _message, clipboard, app, raised = run_transport("PASTE-ME", prior=None)

    assert raised is None
    assert clipboard.kinds == ["READ", "WRITE", "CHORD"]  # no restore write
    assert app.pasted == ["PASTE-ME"]


# --- the in-repo fake's blind spot is closed --------------------------------------------------------


def test_the_in_repo_fake_reads_the_clipboard_value_at_paste_time() -> None:
    """``_simulate_type_clipboard`` now records what was ON THE CLIPBOARD, not the local text.

    This is the structural fix to the double that let the defect through: the recorded
    paste payload is the clipboard's own value, so a restore ordered before the paste is
    visible to the fake too.
    """
    world = FakeComputerBackend()
    world.clipboard_value = "OLD"
    world.focused_control_value = ""

    world._simulate_type_clipboard(_action("NEW"), None)

    assert world.clipboard_pastes == ["NEW"]
    assert world.clipboard_value == "OLD"


def test_the_fake_can_simulate_a_late_consumer_too() -> None:
    """``clipboard_app_latency_s`` makes the delivery-time read reachable from a test."""
    world = FakeComputerBackend(clipboard_app_latency_s=0.025, initial_clipboard="OLD")
    world.focused_control_value = ""
    assert world.clipboard_value == "OLD"

    world._simulate_type_clipboard(_action("NEW"), None)

    # The fake pasted what was ON THE CLIPBOARD at delivery time, not the local text —
    # exactly the check the pre-fix double was structurally unable to make.
    assert world.clipboard_pastes == ["NEW"]
    assert world.clipboard_value == "OLD"
    assert world.focused_control_value == "NEW"


def test_the_fake_does_not_touch_the_real_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake that was made smarter must still never reach the OS clipboard."""
    reached: list[str] = []
    monkeypatch.setattr(
        backend_module,
        "_clipboard_read_verdict",
        lambda: reached.append("read") or (None, "unknown"),
    )
    monkeypatch.setattr(
        backend_module,
        "_clipboard_write_text",
        lambda text: reached.append("write") or True,
    )

    world = FakeComputerBackend(initial_clipboard="OLD")
    world.focused_control_value = ""
    world._simulate_type_clipboard(_action("NEW"), None)

    assert reached == []
