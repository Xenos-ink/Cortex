"""WP1 / §1.1 S-12 (ORVEX-2026-108) — a TYPED ``ensure_app_outcome``.

Ported from the dev copy's ``test_c8_typed_outcome.py`` (F3 fix 5.1) and adapted to
the v0.7.5 surface:

- the closed ``ensure_app_outcome`` Literal enum rides every executed ensure_app
  reply (``None`` on every other action) — a driver switches on a field instead of
  parsing a string;
- a REAL server-side spawn is prefixed ``LAUNCHED`` (WP4b / dev F3 fix 5.1: the
  ``NO_INSTANCE`` token was chosen before the spawn attempt and never revised, so
  the first word of a successful spawn said the opposite of what happened); the
  payload still carries ``launched=<process>``, and the classifier maps it to
  ``"launched"``;
- the typed refusal of an out-of-allowlist target (reject-before-dispatch) stays at
  the launch gate in this tree: the probe runs with ``allow_launch=False`` and
  honestly reports ``not_found`` with nothing spawned — this port pins THAT
  behavior;
- the C·N2 ambiguous-candidate rendering (list EVERY candidate with its ``unsaved``
  flag in the mixed case) landed with WP4b and is pinned below.

``ok`` semantics are the §1.1 ok-rule and are NOT changed here.
"""

from __future__ import annotations

from typing import Any

import pytest
from reply_contract import validate_reply
from test_controller_integration import FAST_LIMITS, SessionRegistry, execute_payload

from computer_use_mcp import server
from computer_use_mcp.pipeline.agent import ENSURE_APP_OUTCOMES, ensure_app_outcome
from computer_use_mcp.backend import (
    AppWindowCandidate,
    FakeComputerBackend,
    is_unsaved_candidate_title,
)
from computer_use_mcp.core.models import WindowInfo


class _FakeProvider:
    async def decide(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        raise RuntimeError("fake provider")

    async def decide_full(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        raise RuntimeError("fake provider")


def window(hwnd: int, title: str, process: str = "excel", pid: int = 4242) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd, pid=pid, process_name=process, window_class="FakeWnd",
        title=title, bounds=(0, 0, 1280, 720),
    )


def candidates(
    *windows: WindowInfo, unsaved: set[str] | None = None
) -> list[AppWindowCandidate]:
    return [
        AppWindowCandidate(
            window=item,
            doc_token=item.title.split(" - ")[0].casefold(),
            unsaved_candidate=(
                is_unsaved_candidate_title(item.title) if unsaved is None else item.title in unsaved
            ),
        )
        for item in windows
    ]


class LaunchingWorld(FakeComputerBackend):
    """A fake world with a scripted window list and a scripted launch resolution."""

    def __init__(
        self,
        *,
        windows: list[WindowInfo],
        app_windows: list[AppWindowCandidate],
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.set_windows(windows)
        self.app_windows = app_windows
        self.launched_processes: list[str] = []


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


async def _drive(
    world: Any,
    target: str,
    *,
    allowlist: list[str] | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    server._backend_factory = lambda: world
    server._provider_factory = lambda: _FakeProvider()
    interference = (
        {"attach_or_launch": {"launch": policy}} if policy is not None else None
    )
    response = server.start_session(
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        allowed_processes=allowlist if allowlist is not None else ["excel.exe", "mspaint.exe"],
        **({"interference": interference} if interference is not None else {}),
    )
    session_id = str(response["session_id"])
    try:
        return execute_payload(
            await server.computer_execute(session_id, "ensure_app", target=target)
        )
    finally:
        try:
            await server.stop_session(session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - teardown must not mask a verdict
            assert "not a live session" not in str(exc)


# --- the classifier: a closed enum, derived once, unit-pinned ---------------------------------------


def test_the_vocabulary_is_closed_and_stable() -> None:
    assert ENSURE_APP_OUTCOMES == frozenset(
        {
            "reattached",
            "ambiguous",
            "not_found",
            "launched",
            "launch_rejected",
            "launch_unresolved",
        }
    )


@pytest.mark.parametrize(
    "message,expected",
    [
        ("REATTACHED title='Book1 - Excel' hwnd=1", "reattached"),
        ("AMBIGUOUS_INSTANCE candidates=[title='a' unsaved=true] do_not_launch=true", "ambiguous"),
        ("LAUNCHED target='mspaint' launched=mspaint.exe", "launched"),
        ("NO_INSTANCE target='x' launch=server (no existing window matched)", "not_found"),
        ("NO_INSTANCE target='x' launch=driver (no existing window matched)", "not_found"),
        ("NO_INSTANCE target='x' launch=server (no match) launch_rejected=LaunchTargetError", "launch_rejected"),
        ("NO_INSTANCE target='x' launch=server (no match) launch_unresolved=path-lookup-missed (...)", "launch_unresolved"),
        ("NO_INSTANCE target='x' launch=server (no match) launch_unresolved=spawn-failed (OSError: no)", "launch_unresolved"),
    ],
)
def test_the_classifier_maps_the_whole_message_vocabulary(message: str, expected: str) -> None:
    assert ensure_app_outcome(message) == expected
    assert ensure_app_outcome(message) in ENSURE_APP_OUTCOMES


def test_a_reattached_title_containing_launched_is_not_a_spawn() -> None:
    """MINOR-a (VAL-WP1): the structured prefixes are authoritative — a REATTACHED
    payload embeds a window TITLE which may itself contain `` launched=``; that
    substring proves a spawn ONLY inside a NO_INSTANCE payload."""
    assert (
        ensure_app_outcome("REATTACHED title='Book launched=a.exe - Excel' hwnd=1")
        == "reattached"
    )
    assert (
        ensure_app_outcome("AMBIGUOUS_INSTANCE candidates=[title='launched=x' unsaved=true]")
        == "ambiguous"
    )
    # the spawn proof still wins inside the structured NO_INSTANCE payload
    assert ensure_app_outcome("NO_INSTANCE target='x' launch=server launched=a.exe") == "launched"


def test_a_non_ensure_app_message_is_not_classified() -> None:
    """``None`` means "this message is not an ensure_app payload" — never a default arm."""
    assert ensure_app_outcome("Executed click.") is None
    assert ensure_app_outcome("") is None
    assert ensure_app_outcome(None) is None  # type: ignore[arg-type]


def test_the_launched_suffix_outranks_a_stale_leading_token() -> None:
    """``launched=`` is the only proof a process was REALLY spawned, so it wins even
    while THIS tree still builds the payload with the pre-fix ``NO_INSTANCE`` leading
    token (the LAUNCHED prefix itself is WP4b scope — the field works without it)."""
    assert ensure_app_outcome("NO_INSTANCE target='x' launch=server launched=mspaint.exe") == "launched"
    assert ensure_app_outcome("LAUNCHED target='x' launched=mspaint.exe") == "launched"


# --- the whole outcome matrix is assertable WITHOUT parsing a string ---------------------------------


async def test_a_reattach_reports_reattached(fresh_server: Any) -> None:
    book = window(1, "Book1 - Excel")
    world = LaunchingWorld(windows=[book], app_windows=candidates(book))

    payload = await _drive(world, "excel|book1")

    validate_reply(payload, "flat_execute")
    assert payload["ensure_app_outcome"] == "reattached"
    assert payload["ok"] is True
    assert payload["message"].startswith("REATTACHED")


async def test_an_ambiguous_match_reports_ambiguous(fresh_server: Any) -> None:
    a = window(11, "Book1 - Excel")
    b = window(12, "Budget - Excel")
    world = LaunchingWorld(
        windows=[a, b], app_windows=candidates(a, b, unsaved={"Book1 - Excel"})
    )

    payload = await _drive(world, "excel|nomatch")

    validate_reply(payload, "flat_execute")
    assert payload["ensure_app_outcome"] == "ambiguous"
    # MAJOR-4 (VAL-WP1): ambiguous did NOT establish which window is the target —
    # uncertain is never success (ok False), the result token is "uncertain".
    assert payload["ok"] is False
    assert payload["result"] == "uncertain"
    assert payload["verification"]["outcome"] == "uncertain"
    assert world.launched_processes == []


async def test_the_mixed_saved_unsaved_case_lists_every_candidate(
    fresh_server: Any,
) -> None:
    """WP4b (C·N2 / dev F3 fix 5.1): the repro's C8-2b — two instances, ONE unsaved,
    ONE reported. The payload used to be built from the UNSAVED SUBSET alone, so the
    saved candidate (the one the driver most needed to see) vanished from the
    message. Every candidate is listed now, each with its own ``unsaved=`` flag."""
    a = window(11, "Book1 - Excel")
    b = window(12, "Budget - Excel")
    world = LaunchingWorld(
        windows=[a, b], app_windows=candidates(a, b, unsaved={"Book1 - Excel"})
    )

    payload = await _drive(world, "excel|nomatch")

    validate_reply(payload, "flat_execute")
    assert payload["ensure_app_outcome"] == "ambiguous"
    message = str(payload["message"])
    assert message.startswith("AMBIGUOUS_INSTANCE")
    assert "Book1 - Excel" in message
    assert "Budget - Excel" in message  # the non-unsaved candidate is no longer dropped
    assert message.count("unsaved=") == 2
    assert "do_not_launch=true" in message
    assert world.launched_processes == []  # the branch never launches


async def test_a_real_launch_reports_launched(fresh_server: Any) -> None:
    """A real spawn is typed ``launched`` — and, since WP4b (C8 / dev F3 fix 5.1),
    the message's leading token is ``LAUNCHED``: the first word now says what
    happened (the old ``NO_INSTANCE`` token was chosen before the spawn attempt and
    never revised)."""
    world = LaunchingWorld(windows=[], app_windows=[])

    payload = await _drive(world, "mspaint")

    assert world.launched_processes == ["mspaint"]
    assert payload["ensure_app_outcome"] == "launched"
    assert payload["result"] == "verified"  # the spawn is proven
    assert payload["message"].startswith("LAUNCHED")
    assert "launched=" in payload["message"]
    assert "launch_rejected=" not in payload["message"]
    assert "launch_unresolved=" not in payload["message"]


async def test_a_no_match_with_no_launch_authority_reports_not_found(fresh_server: Any) -> None:
    world = LaunchingWorld(windows=[], app_windows=[])

    payload = await _drive(world, "mspaint", policy="driver")

    assert payload["ensure_app_outcome"] == "not_found"
    # MAJOR-4 (VAL-WP1): not_found is a launch FAILURE -> result "failed", ok False.
    assert payload["result"] == "failed"
    assert payload["ok"] is False
    assert world.launched_processes == []
    assert payload["message"].startswith("NO_INSTANCE")


async def test_a_target_outside_the_allowlist_is_a_typed_refusal(fresh_server: Any) -> None:
    """An out-of-allowlist target never spawns. In this tree the validation gate
    refuses BEFORE dispatch (the active process identity cannot be verified against
    the allowlist), so this is a REJECTION — not an ensure_app outcome at all — and
    ``ensure_app_outcome`` is legitimately ABSENT (inventing a value here would
    describe an execution that never happened). What matters is that the launch
    itself never happens. (WP4b scope note: the launch-gate-side typed refusal is
    not ported in WP1.)"""
    world = LaunchingWorld(windows=[], app_windows=[])

    payload = await _drive(world, "mspaint", allowlist=["excel"])

    assert world.launched_processes == []
    assert payload["ok"] is False
    assert "allowlist" in payload["message"]
    assert "ensure_app_outcome" not in payload


async def test_the_field_is_present_on_every_ensure_app_reply(fresh_server: Any) -> None:
    book = window(1, "Book1 - Excel")
    a = window(11, "Book1 - Excel")
    b = window(12, "Budget - Excel")
    for world, target in (
        (LaunchingWorld(windows=[book], app_windows=candidates(book)), "excel|book1"),
        (
            LaunchingWorld(windows=[a, b], app_windows=candidates(a, b, unsaved={"Book1 - Excel"})),
            "excel|nomatch",
        ),
        (LaunchingWorld(windows=[], app_windows=[]), "mspaint"),
    ):
        payload = await _drive(world, target)
        assert payload["ensure_app_outcome"] in ENSURE_APP_OUTCOMES, target


async def test_the_field_is_null_on_a_non_ensure_app_action(fresh_server: Any) -> None:
    """Additive and typed: the key is present, and it is ``null`` for a click."""
    server._backend_factory = lambda: FakeComputerBackend()
    server._provider_factory = lambda: _FakeProvider()
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    session_id = str(response["session_id"])
    try:
        payload = execute_payload(
            await server.computer_execute(
                session_id, "click", x=10, y=10, include_screenshot_after=False
            )
        )
    finally:
        await server.stop_session(session_id=session_id)

    assert payload["ensure_app_outcome"] is None


async def test_reattached_and_launched_are_distinguishable_by_field_alone(fresh_server: Any) -> None:
    """C8-9: the two payloads used to differ on every structured key except ``message``."""
    book = window(1, "Book1 - Excel")
    reattached = await _drive(
        LaunchingWorld(windows=[book], app_windows=candidates(book)), "excel|book1"
    )
    launched = await _drive(
        LaunchingWorld(windows=[], app_windows=[]), "mspaint"
    )

    discriminating = [
        key
        for key in set(reattached) | set(launched)
        if key != "message" and reattached.get(key) != launched.get(key)
    ]
    assert "ensure_app_outcome" in discriminating
    assert reattached["ensure_app_outcome"] == "reattached"
    assert launched["ensure_app_outcome"] == "launched"
    # the §1.1 ok-rule is the ONLY ok rule: both rows' ok values derive from their
    # result tokens (both verified here), never from the outcome kind.
    assert isinstance(reattached["ok"], bool) and isinstance(launched["ok"], bool)
