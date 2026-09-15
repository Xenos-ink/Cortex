"""Secret detection and redaction for text and screenshots.

Redaction is deliberately conservative (fail closed): secret-like assignments, bearer
tokens, JWTs, AWS access keys, private-key blocks, basic-auth URLs, and Luhn-valid credit
card numbers are replaced with ``[REDACTED:<pattern>]`` placeholders. Pattern matching is
value-oriented (``name=value`` / ``name: value``) so prose that merely mentions the word
"password" or "token" is not flagged.

R-23: matching runs on the canonical view(s) of the input (:mod:`textnorm`) — ASCII
input is matched exactly as before; non-ASCII input is matched on both the delete and
fold views (never-downgrade). A hit returns the matching view's redacted text (a sink
representation only — redaction output is never the dispatched payload); with no hit
the original string is returned byte-identical.

R-21: the assignment grammar is filler-tolerant (spaced compound nouns ``api key`` /
``access key``, the abbreviation ``pass``, and the copula separators ``is/was/are``
immediately after the noun), and value-shape families cover untagged bearer tokens
(``ghp_``-class GitHub tokens, ``github_pat_``, ``xox[abprs]`` Slack tokens, ``npm_``)
with length floors so truncated/benign strings stay clean.

v07-007 repairs: **S3** (RT-D1-03) — the copula arm accepts an optional ``[:=]``
separator after the copula, so ``password is: X`` / ``pwd was: X`` are caught;
**S5** (RT-D1-07, Commander-refined) — the copula arm's value must be secret-shaped:
``len(V) >= 6`` and not (pure-lowercase ASCII of length <= 10), so benign copula
prose ("the password is stored in the vault") stays clean and can no longer block
checkpoint persistence while "hunter2"-class evidence-corpus values and real
assigned values still match; **P2** — the PEM pair and the bearer/value-shape
families are single compiled alternations (one pass on the clean path), with
per-family hit labels resolved from the matched group.

Screenshot note: pixel-level secret detection is OUT of P0 scope. :func:`redact_image`
blurs explicitly supplied regions, and without regions consults the
:func:`_scan_image_for_secrets` hook (a no-op in P0) that later waves can extend.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from PIL import Image, ImageFilter

from .models import TextRegion
from .textnorm import canonical_views

REDACTED_TEMPLATE = "[REDACTED:{name}]"


def _luhn_ok(candidate: str) -> bool:
    """Return True when ``candidate`` (digits, spaces, or dashes) passes the Luhn check."""
    digits = re.sub(r"\D", "", candidate)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


class SecretPattern(NamedTuple):
    """One registered secret detector: a compiled regex plus optional value validator.

    ``group_names`` (trailing optional, P2) maps the regex's named alternative groups
    to redaction-label names for FUSED patterns — one compiled alternation covering
    several families costs one regex pass instead of N on the clean path while every
    hit still carries its family name (``m.lastgroup`` selects it).
    """

    name: str
    regex: re.Pattern[str]
    value_validator: Callable[[str], bool] | None = None
    group_names: Mapping[str, str] | None = None


#: Assignment-grammar separator + value (R-21 grammar, S3/S5 v07-007 repairs).
#: Direct separator arm (``[=:]``): value floor ``\\S{4,}`` (password) / ``\\S{6,}``
#: (token) — measured-fine on the benign corpus. Copula arm (``is|was|are``, S3: an
#: OPTIONAL ``[:=]`` may follow the copula so ``password is: X`` is caught): the
#: Commander-refined S5 secret-shape rule — value V matches iff
#: ``len(V) >= 6 AND NOT (V is pure lowercase ASCII-alpha AND len(V) <= 10)`` —
#: "hunter2" (7, digit) matches (evidence corpus, AC-21a) while D1's benign prose
#: rows ("stored" 6, "required" 8, "legendary" 9, "documented" 10 — all pure
#: lowercase) stay clean. BOUNDARY NOTE: the ruling's exemption text says <=9, but
#: D1's own FP row "documented" is a 10-char pure-lowercase run; the exemption is
#: <=10 so requirement "the 4 D1 FP rows stay clean" holds with no evidence-corpus
#: loss. The ``(?-i:[a-z])`` scope keeps the shape check case-sensitive inside the
#: IGNORECASE pattern (capitalized "Stored" is secret-shaped, not prose). Group 1 is
#: set only by the direct arm and selects the value alternative via a regex
#: conditional. RT2-D1-02: the copula arm additionally SKIPS leading prose-shaped
#: tokens (the decoy slot) before the secret-shaped value.
_ASSIGNMENT_SEPARATOR = r"(?:([=:])\s*|\b(?:is|was|are)\b\s*[:=]?\s*)['\"]?"

#: RT2-D1-02 (decoy-slot): the copula arm's value region may skip leading PROSE
#: tokens — a token followed by whitespace that is not itself secret-shaped (a run
#: of 1-5 arbitrary characters, or a pure-lowercase ASCII run of 2-10) — so
#: "password is abcdefghij supersecret123" catches the real value behind an exempt
#: decoy. Bounded to the same whitespace-delimited value region (each skip consumes
#: exactly one token + one separator; a token at end-of-region is never skipped),
#: inside the SAME compiled pattern (no extra pass; a no-hit scan costs the same
#: single pass). Secret-shaped tokens are never skipped, and every skip alternative
#' is case-sensitive where the shape rule demands it.
_COPIULA_SKIP_TOKEN = r"(?:(?-i:[a-z]){2,10}|\S{1,5})(?=\s)\s*"
_COPIULA_SECRET = r"(?!(?-i:[a-z]){1,10}(?:\s|$))\S{6,}"


def _assignment_value(direct_floor: int) -> str:
    """Value group: direct arm keeps its measured floor; copula arm = prose-token
    skips + the refined secret-shape token (S5 adjudicated + RT2-D1-02)."""
    return rf"(?(1)\S{{{direct_floor},}}|(?:{_COPIULA_SKIP_TOKEN})*{_COPIULA_SECRET})"


def _assignment_pattern(nouns: str, direct_floor: int) -> re.Pattern[str]:
    return re.compile(
        rf"\b(?:{nouns})['\"]?\s*{_ASSIGNMENT_SEPARATOR}{_assignment_value(direct_floor)}",
        re.IGNORECASE,
    )


SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    SecretPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]{4,})?"),
    ),
    # P2: the PEM block and its bare header share one compiled alternation (block
    # branch first, so a full block still wins over its own header exactly as the
    # ordered tuple did) — one pass instead of two on clean text.
    SecretPattern(
        "private_key_block",
        re.compile(
            r"(?P<block>-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
            r"[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)"
            r"|(?P<header>-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)"
        ),
        group_names={"block": "private_key_block", "header": "private_key_header"},
    ),
    # P2: the bearer separators and the R-21 value-shape families share ONE compiled
    # alternation (the authorization arm first, preserving the tuple precedence). The
    # `(?i:...)` scopes keep the separators case-insensitive exactly like the original
    # IGNORECASE patterns while the value-shape prefixes stay case-sensitive.
    SecretPattern(
        "bearer_authorization",
        re.compile(
            r"\b(?:(?P<bearer_authorization>(?i:authorization|auth)\s*[=:]\s*(?i:bearer)\s+"
            r"[A-Za-z0-9\-._~+/]{16,})"
            r"|(?P<bearer_token>(?i:bearer)\s+[A-Za-z0-9\-._~+/]{24,})"
            r"|(?P<github_token>gh[pousr]_[A-Za-z0-9]{20,})"
            r"|(?P<github_finegrained_pat>github_pat_[A-Za-z0-9_]{17,})"
            r"|(?P<slack_token>xox[abprs][-_][A-Za-z0-9-]{8,})"
            r"|(?P<npm_token>npm_[A-Za-z0-9]{20,})"
            r"|(?P<xocr_token>xocr_[A-Za-z0-9]{20,}))"
        ),
        group_names={
            "bearer_authorization": "bearer_authorization",
            "bearer_token": "bearer_token",
            "github_token": "github_token",
            "github_finegrained_pat": "github_finegrained_pat",
            "slack_token": "slack_token",
            "npm_token": "npm_token",
            "xocr_token": "xocr_token",
        },
    ),
    SecretPattern(
        "basic_auth_url",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@[^\s]+"),
    ),
    # S3/S5: the assignment grammar's copula arm accepts an optional separator after
    # the copula (``password is: X``) and requires the \\S{8,}+digit value floor.
    SecretPattern(
        "password_assignment",
        _assignment_pattern(r"passphrase|password|credentials?|passwd|pwd|pass", 4),
    ),
    SecretPattern(
        "token_assignment",
        _assignment_pattern(
            r"access[\s_-]?key|client[\s_-]?secret|secret[\s_-]?key|api[\s_-]?key|apikey"
            r"|auth[\s_-]?token|secret|token",
            6,
        ),
    ),
    SecretPattern(
        "credit_card",
        re.compile(r"\b(?:\d{4}[ -]?){3,4}\d{1,7}\b"),
        value_validator=_luhn_ok,
    ),
)


def register_secret_pattern(pattern: SecretPattern) -> None:
    """Append a detector to the registry (tuple rebinding keeps readers consistent)."""
    global SECRET_PATTERNS
    SECRET_PATTERNS = (*SECRET_PATTERNS, pattern)


def _redaction_label(pattern: SecretPattern, match: re.Match[str]) -> str:
    """Label for one hit: the matched alternative's family name for fused patterns."""
    if pattern.group_names is not None:
        return REDACTED_TEMPLATE.format(name=pattern.group_names[match.lastgroup])
    return REDACTED_TEMPLATE.format(name=pattern.name)


def _label_replacer(pattern: SecretPattern) -> Callable[[re.Match[str]], str]:
    """Stable per-pattern replacement callback for fused-alternation patterns (P2)."""

    def _replace(match: re.Match[str]) -> str:
        return _redaction_label(pattern, match)

    return _replace


def _redact_view(view: str) -> tuple[str, int]:
    result = view
    count = 0
    for secret in SECRET_PATTERNS:
        if secret.group_names is not None:
            # fused alternation: resolve each hit's family name from its group
            result, replaced = secret.regex.subn(_label_replacer(secret), result)
            count += replaced
        elif secret.value_validator is None:
            replacement = REDACTED_TEMPLATE.format(name=secret.name)
            result, replaced = secret.regex.subn(replacement, result)
            count += replaced
        else:
            validator = secret.value_validator
            replacement = REDACTED_TEMPLATE.format(name=secret.name)
            valid = sum(1 for match in secret.regex.finditer(result) if validator(match.group(0)))
            if valid:

                def _conditional(
                    match: re.Match[str],
                    _validator: Callable[[str], bool] = validator,
                    _replacement: str = replacement,
                ) -> str:
                    return _replacement if _validator(match.group(0)) else match.group(0)

                result = secret.regex.sub(_conditional, result)
                count += valid
    return result, count


def redact_text(text: str) -> tuple[str, int]:
    """Redact secret-like substrings; return ``(redacted_text, replacement_count)``.

    R-23: matched on the canonical view(s) of ``text``. A single view (identical to
    the input — every ASCII string) is matched exactly as before. Otherwise both
    views are tried (never-downgrade); a hit returns that view's redacted text (the
    delete view is preferred: it heals in-word invisible insertions so whole
    value-shape tokens are covered). With no hit the ORIGINAL string is returned
    byte-identical — benign pass-through never rewrites text.
    """
    if not text:
        return text, 0
    views = canonical_views(text)
    for view in views:
        result, count = _redact_view(view)
        if count:
            return result, count
    return text, 0


def contains_secret(text: str) -> bool:
    """Return True when ``text`` matches any registered secret pattern (any view)."""
    for view in canonical_views(text):
        for secret in SECRET_PATTERNS:
            if secret.value_validator is None:
                if secret.regex.search(view):
                    return True
            elif any(
                secret.value_validator(match.group(0)) for match in secret.regex.finditer(view)
            ):
                return True
    return False


def safe_repr(obj: object) -> str:
    """``repr()`` with enforced redaction — use for ANY logging of untrusted objects."""
    return redact_text(repr(obj))[0]


def _normalize_regions(
    regions: Sequence[TextRegion | tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    boxes: list[tuple[int, int, int, int]] = []
    for region in regions:
        if isinstance(region, TextRegion):
            boxes.append((region.x, region.y, region.width, region.height))
        else:
            boxes.append((int(region[0]), int(region[1]), int(region[2]), int(region[3])))
    return boxes


def _scan_image_for_secrets(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Extension hook for pixel-level secret detection (out of P0 scope: returns []).

    Later waves may implement OCR-based or template-based secret region detection here;
    :func:`redact_image` blurs whatever boxes this returns.
    """
    del image  # unused in P0; kept so the hook signature stays stable
    return []


def redact_image(
    image: Image.Image,
    regions: Sequence[TextRegion | tuple[int, int, int, int]] | None = None,
) -> tuple[Image.Image, int]:
    """Return a redacted COPY of ``image`` plus the number of redacted regions.

    With ``regions`` given, each box ``(x, y, width, height)`` (screenshot-local, or a
    :class:`~computer_use_mcp.models.TextRegion`) is blurred. Without regions, the
    :func:`_scan_image_for_secrets` hook decides what to blur (no-op in P0). The input
    image is never mutated.
    """
    if regions is not None:
        boxes = _normalize_regions(regions)
    else:
        boxes = _scan_image_for_secrets(image)
    redacted = image.copy()
    count = 0
    for x, y, width, height in boxes:
        left = max(0, int(x))
        top = max(0, int(y))
        right = min(image.width, int(x) + int(width))
        bottom = min(image.height, int(y) + int(height))
        if right <= left or bottom <= top:
            continue
        box = (left, top, right, bottom)
        radius = max(8.0, min(right - left, bottom - top) / 4.0)
        blurred = redacted.crop(box).filter(ImageFilter.GaussianBlur(radius))
        redacted.paste(blurred, box)
        count += 1
    return redacted, count
