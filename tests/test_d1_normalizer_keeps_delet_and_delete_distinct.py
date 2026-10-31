"""ORVEX-2026-105 normalizer tripwire.

THIS TEST IS SUPPOSED TO FAIL LOUDLY. It is not a regression guard in the usual
sense — it is a guard against a FUTURE "IMPROVEMENT" silently re-opening the
named-identity binding.

The story. ``tests/test_fault_injection.py``'s dialog re-pin only stays honest
because the effect ``"the record is deleted"`` and the observed window title
``"Confirm Delete?"`` do not collide after normalisation. They do not collide for a
reason nobody designed: the minimal stemmer (R-2.5) strips the trailing ``"ed"``
from ``deleted`` (remainder ``delet``, 5 chars, kept) while ``delete`` ends in
``te``, which no rule matches, so it survives whole. The corroboration predicate
therefore finds ``{record, delet} ∩ {confirm, delete} = ∅`` and the false
``verified`` stays closed.

That is an ACCIDENT of the stemmer's current shape, not a property of the design. A
general-purpose stemmer would collapse both words onto ``delet`` (Porter/Snowball
both strip ``e``/``-ed`` to a common root), the intersection would become
non-empty, and the Confirm-Delete? dialog would once again "prove" that the record
was deleted — with no failing test in sight, because the re-pin would have been
updated by whoever changed the stemmer.

So the pair is pinned HERE, deliberately and narrowly. The binding rule is required
to be auditable by reading it: no general stemmer, no POS tagger, no dependency. If
a future change makes ``stem("deleted") == stem("delete")``, THIS FILE FAILS. That
failure is the intended behaviour, and re-opening ORVEX-2026-105 with the owner is
the required response — the fix is never to relax the assertion here.
"""

from __future__ import annotations

from computer_use_mcp.core.models import Observation, WindowInfo
from computer_use_mcp.pipeline.verification import (
    FOCUS_CHANGE_INTENT_FLAG,
    VerificationIntent,
    VerificationKind,
    _content_tokens,
    _effect_tokens,
    _identity_token_classes,
    _novel_tokens,
    stem,
)


def _window(title: str) -> WindowInfo:
    return WindowInfo(hwnd=1, pid=10, process_name="app.exe", title=title)


def _observation(title: str) -> Observation:
    return Observation(
        image_base64="not-a-real-png-the-named-identity-binding-never-reads-pixels",
        width=64,
        height=48,
        active_window=title,
        active_window_info=_window(title),
    )


def test_stem_keeps_deleted_and_delete_distinct() -> None:
    """The literal pin the owner named: ``deleted`` → ``delet`` and ``delete`` →
    ``delete``. If either half moves, the ORVEX-2026-105 review is re-opened with the
    owner."""
    assert stem("deleted") == "delet"
    assert stem("delete") == "delete"
    assert stem("deleted") != stem("delete")


def test_deleted_and_delete_do_not_meet_through_the_content_token_rule() -> None:
    """The same pair one layer up: the ``"ed"`` rule strips ``deleted`` to ``delet`` but
    does not fire on ``delete`` (which ends in ``te``), so the two never reach the same
    token — which is the property the re-pin actually depends on."""
    assert _content_tokens("deleted") == frozenset({"delet"})
    assert _content_tokens("delete") == frozenset({"delete"})
    assert _content_tokens("deleted") & _content_tokens("delete") == frozenset()


def test_the_delete_effect_does_not_intersect_the_confirm_delete_identity() -> None:
    """The consequence the re-pin rides on: the effect's content tokens and the novel
    tokens of the observed ``"Confirm Delete?"`` window must stay disjoint — which is
    the whole reason the Confirm-Delete?-dialog case is ``uncertain`` and not a
    silent ``verified``."""
    effect = _effect_tokens("the record is deleted")
    before = _identity_token_classes(_observation("Main"))
    after = _identity_token_classes(_observation("Confirm Delete?"))
    novel = _novel_tokens(before, after)

    assert effect == frozenset({"record", "delet"}), effect
    assert effect & set(novel) == frozenset(), (
        f"ORVEX-2026-105 RE-OPENED: the delete effect now corroborates on "
        f"{sorted(effect & set(novel))} — a 'Confirm Delete?' dialog would verify "
        f"'the record is deleted' again"
    )


def test_the_pinned_intent_would_still_be_uncertain_for_this_pair() -> None:
    """The behavioural tail of the tripwire, at the intent the re-pin actually uses: a
    bare click carrying a stated effect is the only shape that reaches this binding."""
    intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE,
        expected_change=True,
        expected_effect="the record is deleted",
        metadata={FOCUS_CHANGE_INTENT_FLAG: True},
    )
    # Nothing here verifies on the pair alone: this is the normaliser pin's scope, and
    # the tier's own verdict is pinned in tests/test_d1_focus_change_named_identity.py.
    assert intent.expected_effect is not None
    assert _effect_tokens(intent.expected_effect) & set(
        _novel_tokens(
            _identity_token_classes(_observation("Main")),
            _identity_token_classes(_observation("Confirm Delete?")),
        )
    ) == frozenset()
