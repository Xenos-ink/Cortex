"""WP5 port — the NON-PLAN nodes of the dev copy's ``test_evidence_one_type_per_path.py``.

D·N2 / repair 3.5 (decision D-8a) pinned ONE ``evidence`` container type per response
path.  The dev file's plan-handshake nodes (``evidence_by_backend`` on plan step and
terminal replies) are PLAN-DEPENDENT — the plan machinery does not exist in the stable
tree yet — and are DEFERRED to WP7 (owning WP), which builds ``evidence_by_backend``
from the evidence store + lease at the ``WP7b-SEAM(evidence-by-backend)`` marker in
``server.py``.  The two nodes below need no plan and are ported as-is in substance:

* the verification ``evidence`` stays a LIST on executed replies;
* a non-plan execute reply never carries the plan-side dict key (after WP5 it also
  never carries it — the WP5 evidence keys are a different, flat key family).
"""

from __future__ import annotations

from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.models import VerificationResult
from computer_use_mcp.state import SessionRegistry

#: The plan-handshake evidence key (D-8a rename) — ABSENT from every non-plan reply.
PLAN_EVIDENCE_KEY = "evidence_by_backend"


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


async def test_the_verification_evidence_is_still_a_list(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _b = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=True),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )

    payload = execute_payload(await server.computer_execute(session_id, "click", x=100, y=100))

    assert isinstance(payload["verification"]["evidence"], list)
    assert all(isinstance(line, str) for line in payload["verification"]["evidence"])


async def test_a_single_execute_reply_carries_only_the_list_evidence(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-plan reply never carried the dict — and still does not carry the new key
    (the WP5 evidence keys are a separate flat family on the same reply)."""
    session_id, _bundle, _backend, _b = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=True),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )

    payload = execute_payload(await server.computer_execute(session_id, "click", x=100, y=100))

    assert PLAN_EVIDENCE_KEY not in payload
    assert "evidence" not in payload  # the bare key is the plan-path one
    assert "evidence" in payload["verification"]


def test_the_two_evidence_paths_are_distinguishable_by_name_alone() -> None:
    """The point of the rename: no reply anywhere can be typed by the bare key alone."""
    assert PLAN_EVIDENCE_KEY != "evidence"
    assert VerificationResult.model_fields["evidence"].annotation == list[str]
