from computer_use_mcp.core.models import GroundedAction, SessionState
from computer_use_mcp.security.safety import SafetyPolicy


def test_server_module_imports() -> None:
    from computer_use_mcp import server

    assert server.mcp.name == "Cortex"


def test_stopped_session_rejects_actions() -> None:
    state = SessionState(session_id="test", stopped=True)
    decision = SafetyPolicy().evaluate(GroundedAction(action="wait", delta=1), state)
    assert decision.allowed is False
