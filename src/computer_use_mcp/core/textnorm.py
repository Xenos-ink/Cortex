"""Matching-only text canonicalization (R-23, v07-007 SPEC v3).

Zero-width/variation-selector/tatweel characters split the tokens and ``\\b`` anchors
that the safety keyword gate, the contextual classifier, and the redaction patterns
match on (``"de\\u200blete"`` tokenizes as ``[de, lete]``; ``"for\\u200bmat C:"`` breaks
the ``\\b``-anchored disk pattern). This module provides the single canonicalizer every
text matcher consumes INSTEAD of raw text.

Design (SPEC v3, proven by the executed obfuscation matrix in
``evidence/v07-007/r23-root-cause.md``):

- **Pipeline:** ``view(x) = TRANSLATE(NFKC(x))`` — NFKC FIRST, strip/fold second. The
  order is load-bearing: NFKC can *manufacture* strip-set characters during
  composition (the Arabic presentation ligatures U+FCF2-FCF4/U+FE71..U+FE7F decompose
  through tatweel U+0640), so the final translate pass guarantees the output is
  strip-free and ASCII-whitespace-only by construction (proven over all 1,114,112
  codepoints, both views, fully idempotent).
- **Two views of the same strip set** (437 codepoints / 30 contiguous ranges,
  Unicode 15.0.0: all category Cf format characters, the variation selectors
  U+FE00-FE0F + U+E0100-E01EF, the Arabic tatweel U+0640, and — v07-007 repair S1
  (RT-D1-02) — the nine zero-width/invisible non-Cf splitters U+034F (COMBINING
  GRAPHEME JOINER), U+180B-U+180D (Mongolian free variation selectors),
  U+115F/U+1160/U+3164 (Hangul fillers), U+FFA0 (halfwidth Hangul filler), and
  U+17B4/U+17B5 (deprecated Khmer inherent vowels); all nine are NFKC-invariant
  or NFKC-fold into the set (U+FFA0 -> U+3164), none is ``str.isspace()``, and
  none is ASCII, so the ASCII fast path and the benign corpus are unaffected):
  - *delete view*: strip set removed — catches in-word insertion (``for\\u200bmat``);
  - *fold view*: strip set -> U+0020 — catches between-word insertion and
    space-substitution residue (``restart\\u200bthe\\u200bserver``, ``drop\\u200bdatabase``).
  All non-ASCII whitespace (28 ``str.isspace()`` codepoints) folds to U+0020 in both
  views; ASCII whitespace folding is deferred on the fast path (behavior-neutral
  today: every existing pattern is ``\\s``-aware and the tokenizer treats any
  non-token character as a separator).
- **ASCII fast path:** every ASCII string is returned as-is, so the entire benign
  corpus and the overwhelming TYPE population run exactly today's single-pass
  matching at today's cost (the gate check is a single ``str.isascii`` call).
- **Merge rule (callers):** match on every returned view and merge never-downgrade —
  block if any view blocks, risk = max across views. :func:`canonical_views` returns
  ONE element when the views coincide (always true for ASCII, and for non-ASCII text
  that canonicalizes to a single distinct form), so well-formed input pays zero
  added matching cost.

INVARIANTS: this module is used for matching/decisions ONLY. The text dispatched to
the OS, approval-message reasons, and audit display fields keep the ORIGINAL bytes
(``CORTEX_TYPE_INTEGRITY``); on a redaction hit the caller may return the matching
view's redacted text (a sink representation), and on NO match the caller must return
the original string byte-identical.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["canonical_views", "fold_marked"]

#: RT2-D1-01: sentinel marking strip-derived separators in :func:`fold_marked`.
#: U+001F is ``str.isspace()`` (it is in the whitespace fold set) and ``\s``-matched,
#: so a marked fold view behaves EXACTLY like the fold view for every pattern, while
#: remaining distinguishable from a real U+0020 space.
_FOLD_MARK = "\u001f"


def _cp_escape(cp: int) -> str:
    return f"\\u{cp:04X}" if cp <= 0xFFFF else f"\\U{cp:08X}"


def _class_range(lo: int, hi: int) -> str:
    if lo == hi:
        return _cp_escape(lo)
    return f"{_cp_escape(lo)}-{_cp_escape(hi)}"


#: Strip set — all Unicode category Cf (format) characters, the variation selectors
#: (U+FE00-FE0F, U+E0100-E01EF), the Arabic tatweel U+0640, and the invisible
#: non-Cf splitters added by the v07-007 S1 repair (RT-D1-02): 437 codepoints in 30
#: contiguous ranges. Pinned to Unicode 15.0.0; derived over the full codepoint space
#: by ``evidence/v07-007/r23_matrix.py`` (``meta.normalization_spec``) and extended by
#: ``evidence/v07-007/redteam/report.md`` (RT-D1-02: each added codepoint provably
#: splits keywords in BOTH canonical views exactly like the Cf set).
_STRIP_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD),  # soft hyphen
    (0x034F, 0x034F),  # COMBINING GRAPHEME JOINER (Mn, Default_Ignorable) — S1/RT-D1-02
    (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD),  # Arabic number signs/mark
    (0x0640, 0x0640),  # Arabic tatweel (kashida) — visual-stretch joiner, no lexical content
    (0x070F, 0x070F),  # Syriac abbreviation mark
    (0x0890, 0x0891), (0x08E2, 0x08E2),  # Arabic pound/mark signs, date mark
    (0x115F, 0x1160),  # HANGUL CHOSEONG/JUNGSEONG FILLER (Lo, zero-width) — S1/RT-D1-02
    (0x17B4, 0x17B5),  # KHMER VOWEL INHERENT AQ/AA (Mn, deprecated) — S1/RT-D1-02
    (0x180B, 0x180D),  # Mongolian free variation selectors (Mn) — S1/RT-D1-02
    (0x180E, 0x180E),  # Mongolian vowel separator (Cf)
    (0x200B, 0x200D),  # zero-width space / non-joiner / joiner
    (0x200E, 0x200F), (0x202A, 0x202E),  # bidi controls
    (0x2060, 0x2064),  # word joiner + invisible operators
    (0x2066, 0x206F),  # bidi isolates + deprecated format characters
    (0x3164, 0x3164),  # HANGUL FILLER (Lo, zero-width; NFKC target of U+FFA0) — S1/RT-D1-02
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xFFA0, 0xFFA0),  # HALFWIDTH HANGUL FILLER (Lo; NFKC -> U+3164) — S1/RT-D1-02
    (0xFFF9, 0xFFFB),  # interlinear annotation
    (0x110BD, 0x110BD), (0x110CD, 0x110CD),  # Kaithi number signs
    (0x13430, 0x1343F),  # Egyptian format controls
    (0x1BCA0, 0x1BCA3),  # shorthand format controls
    (0x1D173, 0x1D17A),  # musical symbol control
    (0xE0001, 0xE0001), (0xE0020, 0xE007F),  # tag characters
    (0xFE00, 0xFE0F),  # variation selectors (block 1)
    (0xE0100, 0xE01EF),  # variation selectors (supplement)
)

#: Whitespace fold set — every ``str.isspace()`` codepoint except ASCII U+0020, folded
#: to U+0020 (behavior-neutral today; makes the views deterministic).
_WS_RANGES: tuple[tuple[int, int], ...] = (
    (0x09, 0x0D), (0x1C, 0x1F), (0x85, 0x85), (0xA0, 0xA0),
    (0x1680, 0x1680), (0x2000, 0x200A), (0x2028, 0x2029), (0x202F, 0x202F),
    (0x205F, 0x205F), (0x3000, 0x3000),
)


def _union_pattern(ranges: tuple[tuple[int, int], ...]) -> str:
    return "".join(_class_range(lo, hi) for lo, hi in ranges)


def _build_invisible_pattern() -> str:
    # One alternation class covering strip set + whitespace fold set; the two sets are
    # disjoint (no Cf/VS/tatweel character is ``str.isspace()``), so a match classifies
    # by ``isspace()`` alone.
    return "[" + _union_pattern(_STRIP_RANGES) + _union_pattern(_WS_RANGES) + "]"


_INVISIBLE_RE = re.compile(_build_invisible_pattern())


def _to_delete(match: re.Match[str]) -> str:
    return "" if not match.group().isspace() else " "


def _to_mark(match: re.Match[str]) -> str:
    """Fold-replacement that keeps separator provenance (RT2-D1-01): strip-class
    characters become the U+001F sentinel, real whitespace becomes U+0020."""
    return _FOLD_MARK if not match.group().isspace() else " "


def canonical_views(text: str) -> tuple[str, ...]:
    """Return the matching view(s) of ``text`` per SPEC v3.

    One element — ``text`` itself — when the text is already canonical (every ASCII
    string, and non-ASCII text whose two views coincide): callers run their existing
    single-pass matching unchanged. Otherwise ``(delete_view, fold_view)``, to be
    merged never-downgrade by the caller.
    """
    if text.isascii():
        return (text,)
    nfkc = unicodedata.normalize("NFKC", text)
    delete_view = _INVISIBLE_RE.sub(_to_delete, nfkc)
    fold_view = _INVISIBLE_RE.sub(" ", nfkc)
    if delete_view == fold_view:
        return (delete_view,)
    return (delete_view, fold_view)


def fold_marked(text: str) -> str | None:
    """The fold view of ``text`` with strip-derived separators MARKED (RT2-D1-01).

    Identical to the fold view (the second element of :func:`canonical_views`) except
    that every separator a strip-set character produced is the U+001F sentinel while
    real whitespace is U+0020 — token streams are identical, gap provenance is kept.
    ``None`` when the text cannot contain strip characters (pure-ASCII input, and
    non-ASCII input whose NFKC form is pure ASCII): there is nothing to mark, and the
    caller's boundary-faithful fusion is simply inactive. Matching behavior of the
    marked view equals the fold view for every whitespace-tolerant pattern.
    """
    if text.isascii():
        return None
    nfkc = unicodedata.normalize("NFKC", text)
    if nfkc.isascii():
        return None
    return _INVISIBLE_RE.sub(_to_mark, nfkc)
