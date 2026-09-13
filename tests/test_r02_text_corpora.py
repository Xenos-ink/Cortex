"""R-02 both-direction text corpora: benign text passes, secrets still block.

The safety keyword gate (:meth:`computer_use_mcp.safety.SafetyPolicy._looks_sensitive`,
enforced by ``SafetyPolicy.evaluate`` for ``type`` actions) and the redaction layer
(:func:`computer_use_mcp.redaction.contains_secret` / :func:`redact_text`) are pinned
in BOTH directions against the corpora below:

- the BENIGN corpus (the R-02 field findings plus their neighborhood prose) must PASS
  both layers — this is the false-positive regression the ROADMAP item fixes;
- the SECRET/DESTRUCTIVE corpus must still be BLOCKED by the safety gate, and the
  value-bearing REDACTION corpus must still be detected (and redacted) — this is the
  zero-weakening half of the contract.

HARD RULE: no row of the blocked corpora may be relaxed to make a test pass. If a fix
needs a blocked row to pass, that is a reported conflict, never a corpus edit.

Corpus definitions (as tested, English) — this file IS the corpus of record:

- "black-scholes closed-form formula expository"  (legacy ``"rm "`` ⊂ ``"closed-form "``)
- "binomial option pricing model Cox Ross Rubinstein"  (legacy ``"del "`` ⊂ ``"model "``)
- "closed form", "call and put prices"  (ROADMAP R-02 field findings)
- the DOI-token strings  (legacy ``"token"`` ⊂ ``"tokens"``/``"doi-token"``)
"""

from __future__ import annotations

import pytest

from computer_use_mcp.models import GroundedAction, RiskLevel, SessionState
from computer_use_mcp.redaction import contains_secret, redact_text
from computer_use_mcp.safety import SafetyContext, SafetyPolicy

#: Benign text (finance/academic prose and its neighborhood) that must pass BOTH the
#: safety keyword gate AND the redaction value scan. Order-stable for failure output.
BENIGN_CORPUS: tuple[str, ...] = (
    # the exact R-02 field findings (ROADMAP R-02 / master-mission §2):
    "black-scholes closed-form formula expository",
    "binomial option pricing model Cox Ross Rubinstein",
    "closed form",
    "call and put prices",
    "DOI tokens",
    "the DOI token uniquely identifies the published paper",
    "doi-token strings in the bibliography",
    # neighborhood prose guarding against re-narrowing to substring matching:
    "the model reformulated the differential equation",
    "confirm deletion of the draft paragraph",  # "deletion" is not the "del " command
    "reformatting a paragraph in the document",
    "black-scholes closed form expository summary",
    "pricing calls and puts with the binomial model",
)

#: Destructive commands, credential material, and direct secret mentions that the
#: SAFETY keyword gate must still block (and the benign corpus must never drift into).
SAFETY_BLOCKED_CORPUS: tuple[str, ...] = (
    # destructive commands (each legacy marker family keeps at least one pin):
    "rm -rf /",
    "del C:\\Users\\reports",
    "erase the disk",
    "format C: /q",
    "shutdown /r /t 0",
    "restart the server now",
    "powershell -enc AAAA",
    "cmd.exe /c del %TEMP%",
    "reg delete HKLM\\SOFTWARE\\Key",
    "remove-item -path C:\\data -recurse",
    "drop database production",
    "delete account admin",
    "delete user bob",
    "reset password for the admin",
    # credential/secret material (value-bearing or direct mention):
    "my password is hunter2",
    "password=hunter2",
    "api_key=AKIAIOSFODNN7EXAMPLE",
    "apikey rotation scheduled tonight",
    "token=ghp_0123456789abcdefghijklmnopqrstuvwxyz",
    "enter the access token in the header",
    "store the api tokens in the vault",
    "client_secret rounds the corner",  # direct "secret" mention still blocks
    "paste the credentials into the form",
)

#: Value-bearing secrets the REDACTION layer must detect and replace. (Prose that
#: merely MENTIONS "password"/"token" is deliberately NOT here — redaction is
#: value-oriented by design and its benign half is BENIGN_CORPUS.)
REDACTION_BLOCKED_CORPUS: tuple[str, ...] = (
    "AKIAIOSFODNN7EXAMPLE",
    "aws key AKIA0Z98XYZABCDEF456 in config",
    "auth-token: abcdefgh123456",  # hyphenated key form: caught by token_assignment
    "authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    "https://alice:hunter2@example.com/path",
    "password = 'hunter2dozen'",
    "api_key: sk-proj-abcdefgh1234567890",
    "auth_token=abcdef1234567890",
    "card 4111 1111 1111 1111 on file",  # Luhn-valid PAN
)

#: Near-miss strings that must stay UNDETECTED by the redaction layer (value-oriented
#: precision is part of the contract; the validator gates must not over-trigger).
REDACTION_BENIGN_CORPUS: tuple[str, ...] = (
    *BENIGN_CORPUS,
    "the token bucket algorithm limits the request rate",
    "password policy was updated today",
    "AKIAIOSFODNN7EXAMPL",  # 15 chars after AKIA: not an AWS access key
    "card 4111 1111 1111 1112 on file",  # Luhn-invalid
)


def test_benign_corpus_passes_safety_keyword_gate() -> None:
    for text in BENIGN_CORPUS:
        assert SafetyPolicy._looks_sensitive(text) is False, text


def test_blocked_corpus_is_blocked_by_safety_keyword_gate() -> None:
    for text in SAFETY_BLOCKED_CORPUS:
        assert SafetyPolicy._looks_sensitive(text) is True, text


def test_blocked_corpus_is_blocked_end_to_end_by_evaluate() -> None:
    """The evaluate() gate (the enforcement point) blocks a ``type`` of that text."""
    policy = SafetyPolicy()
    state = SessionState(session_id="r02-corpus")
    for text in SAFETY_BLOCKED_CORPUS:
        action = GroundedAction(action="type", text=text, confidence=1.0)
        decision = policy.evaluate(action, state, SafetyContext())
        assert decision.allowed is False, text
        assert decision.reason == "Text resembles a secret, credential, or destructive command.", text
        # destructive rows additionally classify CRITICAL via the contextual patterns;
        # the keyword gate blocks them independently of the classifier's verdict.
        assert decision.risk in (None, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.CRITICAL), text


def test_benign_corpus_types_clean_through_evaluate() -> None:
    """The R-02 field findings type without the false secret/credential block."""
    policy = SafetyPolicy()
    state = SessionState(session_id="r02-corpus")
    for text in (
        "black-scholes closed-form formula expository",
        "binomial option pricing model Cox Ross Rubinstein",
        "closed form",
        "call and put prices",
        "DOI tokens",
    ):
        action = GroundedAction(action="type", text=text, confidence=1.0)
        decision = policy.evaluate(action, state, SafetyContext())
        assert decision.allowed is True, text
        assert "resembles a secret" not in decision.reason, text


@pytest.mark.parametrize("text", list(REDACTION_BENIGN_CORPUS))
def test_benign_corpus_passes_redaction(text: str) -> None:
    assert contains_secret(text) is False, text
    redacted, count = redact_text(text)
    assert count == 0, (text, redacted)
    assert redacted == text, text


@pytest.mark.parametrize("text", list(REDACTION_BLOCKED_CORPUS))
def test_blocked_corpus_is_detected_and_redacted(text: str) -> None:
    assert contains_secret(text) is True, text
    redacted, count = redact_text(text)
    assert count >= 1, text
    assert "[REDACTED:" in redacted, (text, redacted)
    assert contains_secret(redacted) is False, (text, redacted)
