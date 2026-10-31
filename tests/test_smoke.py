from computer_use_mcp.core.models import GroundedAction, SessionState
from computer_use_mcp.security.safety import SafetyPolicy


def test_server_module_imports() -> None:
    import computer_use_mcp.server.app as srv_server_app

    assert srv_server_app.mcp.name == "Cortex"


def test_stopped_session_rejects_actions() -> None:
    state = SessionState(session_id="test", stopped=True)
    decision = SafetyPolicy().evaluate(GroundedAction(action="wait", delta=1), state)
    assert decision.allowed is False
