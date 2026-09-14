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

Screenshot note: pixel-level secret detection is OUT of P0 scope. :func:`redact_image`
blurs explicitly supplied regions, and without regions consults the
:func:`_scan_image_for_secrets` hook (a no-op in P0) that later waves can extend.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
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
    """One registered secret detector: a compiled regex plus optional value validator."""

    name: str
    regex: re.Pattern[str]
    value_validator: Callable[[str], bool] | None = None


SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    SecretPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]{4,})?"),
    ),
    SecretPattern(
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"
            r"[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"
        ),
    ),
    SecretPattern(
        "private_key_header",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"),
    ),
    SecretPattern(
        "bearer_authorization",
        re.compile(r"\b(?:authorization|auth)\s*[=:]\s*bearer\s+[A-Za-z0-9\-._~+/]{16,}", re.IGNORECASE),
    ),
    SecretPattern(
        "bearer_token",
        re.compile(r"\bbearer\s+[A-Za-z0-9\-._~+/]{24,}", re.IGNORECASE),
    ),
    SecretPattern(
        "basic_auth_url",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@[^\s]+"),
    ),
    SecretPattern(
        "password_assignment",
        re.compile(
            r"\b(?:passphrase|password|credentials?|passwd|pwd|pass)['\"]?\s*"
            r"(?:[=:]\s*|\b(?:is|was|are)\s+)['\"]?\S{4,}",
            re.IGNORECASE,
        ),
    ),
    SecretPattern(
        "token_assignment",
        re.compile(
            r"\b(?:access[\s_-]?key|client[\s_-]?secret|secret[\s_-]?key|api[\s_-]?key|apikey"
            r"|auth[\s_-]?token|secret|token)['\"]?\s*(?:[=:]\s*|\b(?:is|was|are)\s+)['\"]?\S{6,}",
            re.IGNORECASE,
        ),
    ),
    SecretPattern(
        "credit_card",
        re.compile(r"\b(?:\d{4}[ -]?){3,4}\d{1,7}\b"),
        value_validator=_luhn_ok,
    ),
    # R-21 value-shape families (the AKIA shape generalized): untagged bearer tokens,
    # each with a length floor so truncated or coincidental strings stay clean.
    SecretPattern("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    SecretPattern("github_finegrained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{17,}\b")),
    SecretPattern("slack_token", re.compile(r"\bxox[abprs][-_][A-Za-z0-9-]{8,}\b")),
    SecretPattern("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")),
    SecretPattern("xocr_token", re.compile(r"\bxocr_[A-Za-z0-9]{20,}\b")),
)


def register_secret_pattern(pattern: SecretPattern) -> None:
    """Append a detector to the registry (tuple rebinding keeps readers consistent)."""
    global SECRET_PATTERNS
    SECRET_PATTERNS = (*SECRET_PATTERNS, pattern)


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


def _redact_view(view: str) -> tuple[str, int]:
    result = view
    count = 0
    for secret in SECRET_PATTERNS:
        replacement = REDACTED_TEMPLATE.format(name=secret.name)
        if secret.value_validator is None:
            result, replaced = secret.regex.subn(replacement, result)
            count += replaced
        else:
            validator = secret.value_validator
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
