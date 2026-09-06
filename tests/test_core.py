from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.models import GroundedAction, SessionState
from computer_use_mcp.safety import SafetyPolicy


def test_click_requires_approval_by_default() -> None:
    decision = SafetyPolicy().evaluate(
        GroundedAction(action="click", point={"x": 10, "y": 20}),
        SessionState(session_id="test"),
    )
    assert decision.allowed is True
    assert decision.requires_approval is True


def test_secret_like_text_is_blocked() -> None:
    decision = SafetyPolicy().evaluate(
        GroundedAction(action="type", text="API_KEY=secret-value"),
        SessionState(session_id="test"),
    )
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_fake_backend_records_grounded_action() -> None:
    backend = FakeComputerBackend()
    action = GroundedAction(action="click", point={"x": 100, "y": 200})
    message = backend.execute(action)
    assert message == "Simulated click."
    assert backend.executed == [action]
