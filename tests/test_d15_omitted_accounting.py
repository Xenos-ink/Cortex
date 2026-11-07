"""D15 / repair 3.1 (ORVEX-2026-108 WP4b) — EVERY region the semantic read dropped is
accounted for.

``omitted_count`` was computed only as ``len(merged) - cap`` inside the block builder
(``visual_views.spatial_text_of``), which structurally cannot see the drops the
semantic reader makes on its own:

- the UIA needle's ``UIA_MAX_ELEMENTS`` cap (``backend.py`` ``UiaSemanticReader.read``),
- its wall-clock budget break,
- the ``Win32TextReader``'s count/level/budget caps,
- per-element drops in ``_uia_snapshot_to_text_regions`` (rect-less, degenerate rect,
  text-less) and its own second ``UIA_MAX_ELEMENTS`` cap,
- the silent ``text[:200]`` clip (no marker at all).

A partial read therefore shipped as a HEALTHY block: ``region_count=12,
omitted_count=0`` while 33 of 45 known regions were missing, and the agent — which
decides *whether a control exists* from this block — could not tell.

The fix is additive only: the semantic read now REPORTS what it knows it dropped, the
count rides ``Observation.ocr_dropped`` into the existing ``omitted_count`` channel,
and each 200-char clip carries a per-region ``text_truncated`` marker on the existing
``TextRegion`` model. ``omitted_count`` and ``pixel_evidence`` already existed in the
schema, so no key is removed and none is renamed.

E-15 (ORVEX-2026-106, fixed during the WP4b port): the dev copy merged the reader's
cap/budget split with ``drops.update(...)``, which REPLACED — and thereby ZEROED — the
``offscreen``/``unavailable`` counts the walk had already accumulated. The fix
accumulates (:meth:`UiaSemanticReader._account_cap_and_budget`) and is pinned at the
bottom of this module. The dev copy's ``Win32TextReader._dropped_for_total`` unit pins
are NOT ported: that helper was DEAD code in dev (never called by the Win32 walk,
which builds its breakdown directly) and is deliberately removed here.
"""

from __future__ import annotations

from typing import Any

import computer_use_mcp.desktop.backend as backend_module
from computer_use_mcp.desktop import backend as B
import computer_use_mcp.desktop.text_readers as text_readers_module
from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
from computer_use_mcp.desktop.text_readers import (
    SNAPSHOT_DROPPED_KEY,
    UIA_MAX_ELEMENTS,
    _empty_drops,
    _uia_snapshot_to_text_regions,
)
from computer_use_mcp.core.models import TextRegion
from computer_use_mcp.vision.visual_views import SPATIAL_TEXT_CAP, spatial_text_of

# --- the scripted semantic reader (the UiaSemanticReader / Win32TextReader contract) ---------


def element(index: int, *, name: str | None = None, x: int = 0, y: int = 0) -> dict[str, Any]:
    """One UIA-snapshot element, exactly the shape ``read()`` builds.

    ``rect`` is ``(left, top, width, height)`` — the production contract consumed by
    ``_uia_rect_to_screenshot`` (a degenerate rect is one whose width or height is 0).
    """
    return {
        "name": name if name is not None else f"Label {index}",
        "control_type": "Text",
        "automation_id": f"id{index}",
        "value": None,
        "offscreen": False,
        "rect": (float(x), float(y), 120.0, 20.0),
        "focused": False,
    }


class ScriptedReader:
    """The reader contract with a known ground truth.

    ``known_total`` is how many text elements the controlled surface actually has —
    the loss the agent currently cannot see.  ``dropped`` is what the reader REPORTS
    (a faithful reader reports every element it saw but did not hand over).
    """

    def __init__(
        self,
        elements: list[dict[str, Any]] | None,
        *,
        known_total: int = 0,
        available: bool = True,
        dropped: int | None = None,
    ) -> None:
        self._elements = elements
        self.known_total = known_total
        self._available = available
        # A faithful reader of ``known_total`` elements reports the shortfall; the
        # default is "reported nothing dropped" (the pre-fix shape).
        self._dropped = (
            dropped if dropped is not None else max(0, known_total - len(elements or []))
        )

    @property
    def available(self) -> bool:
        return self._available

    def read(self) -> dict[str, Any] | None:
        if self._elements is None:
            return None
        return {
            "elements": list(self._elements),
            SNAPSHOT_DROPPED_KEY: {"cap_exceeded": self._dropped},
        }


class TextWorld(FakeComputerBackend):
    """A fake world whose observation carries the REAL converted semantic fields."""

    def __init__(self, reader: ScriptedReader, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.reader = reader

    def observe(self, monitor_index: int | None = None) -> Any:
        observation = super().observe(monitor_index=monitor_index)
        self._semantic_reader = self.reader
        ocr_text, ui_elements, dropped = B.LocalComputerBackend._uia_semantic_fields(
            self, observation.monitor, self._active_context.verdict
        )
        observation.ocr_text = ocr_text
        observation.ui_elements = ui_elements
        observation.ocr_dropped = dropped
        return observation


def _world(reader: ScriptedReader) -> Any:
    world = TextWorld(reader)
    observation = world.observe()
    return observation


# --- (a) the headline: a partial read is fully accounted for ---------------------------------


def test_partial_read_reports_region_count_plus_omitted_as_the_known_total() -> None:
    """D15-1: 12 of 45 regions reached the block, 33 were silently missing."""
    reader = ScriptedReader([element(i, y=i * 24) for i in range(12)], known_total=45)
    observation = _world(reader)

    block = spatial_text_of(observation)

    assert block is not None
    assert block["region_count"] == 12
    # Every known region is now accounted for: 12 served + 33 omitted == 45.
    assert block["omitted_count"] == 33
    assert block["region_count"] + block["omitted_count"] == reader.known_total


def test_a_forty_five_element_surface_is_fully_accounted() -> None:
    """The briefed headline case, end to end, including the block cap."""
    reader = ScriptedReader([element(i, y=i * 24) for i in range(45)], known_total=45)
    observation = _world(reader)

    block = spatial_text_of(observation)

    assert block is not None
    # 45 offered, the conversion caps at UIA_MAX_ELEMENTS (30) and the block at 20.
    assert block["region_count"] == SPATIAL_TEXT_CAP
    # 15 beyond the block cap + 15 the conversion never produced.
    assert block["omitted_count"] == 45 - block["region_count"]
    assert block["region_count"] + block["omitted_count"] == 45


def test_the_conversion_reports_its_own_cap_drops_too() -> None:
    """A reader that reports 0 drops but offers 45 elements still loses 15."""
    reader = ScriptedReader(
        [element(i, y=i * 24) for i in range(45)], known_total=45, dropped=0
    )
    observation = _world(reader)

    block = spatial_text_of(observation)

    assert block is not None
    # 20 served; 45 known; the 25 the conversion itself dropped are the difference.
    assert block["region_count"] == 20
    assert block["omitted_count"] == 25
    assert block["region_count"] + block["omitted_count"] == 45


# --- (c) rect-less / text-less elements are counted -----------------------------------------


def test_rect_less_and_text_less_elements_are_counted_as_omitted() -> None:
    """D15-10: 4 elements reported, only 2 have usable text AND a rect — 2 must be
    accounted.  (A ``control_type`` of ``"Custom"`` IS text: the conversion falls back
    to it, so ``nameless`` legitimately produces a region.)"""
    nameless = {
        "name": "", "control_type": "Custom", "automation_id": "x", "value": None,
        "offscreen": False, "rect": (0.0, 0.0, 80.0, 20.0), "focused": False,
    }
    unknown_type = {
        "name": "", "control_type": "", "automation_id": "y", "value": None,
        "offscreen": False, "rect": (0.0, 30.0, 80.0, 20.0), "focused": False,
    }
    no_rect = {
        "name": "NoRect", "control_type": "Text", "automation_id": "z", "value": None,
        "offscreen": False, "rect": None, "focused": False,
    }
    reader = ScriptedReader(
        [element(0, name="Visible label", y=60), nameless, unknown_type, no_rect],
        known_total=4,
        dropped=0,
    )
    observation = _world(reader)

    block = spatial_text_of(observation)

    assert block is not None
    assert block["region_count"] == 2  # "Visible label" + the "Custom" fallback
    assert block["omitted_count"] == 2  # the text-less one + the rect-less one
    assert block["region_count"] + block["omitted_count"] == 4


def test_a_degenerate_rect_counts_as_dropped_too() -> None:
    """``_uia_rect_to_screenshot`` returns None for a zero-width/height rect."""
    degenerate = {
        "name": "Zero width", "control_type": "Text", "automation_id": "d",
        "value": None, "offscreen": False,
        "rect": (10.0, 10.0, 0.0, 20.0),  # width == 0
        "focused": False,
    }
    reader = ScriptedReader([element(0), degenerate], known_total=2, dropped=0)
    observation = _world(reader)

    block = spatial_text_of(observation)

    assert block is not None
    assert block["region_count"] == 1
    assert block["omitted_count"] == 1
    assert block["region_count"] + block["omitted_count"] == 2


# --- (d) the 200-char clip is marked per region ---------------------------------------------


def test_a_four_hundred_char_label_is_marked_truncated() -> None:
    """D15-6: the region is PRESENT, so the count criterion cannot fire — but its
    CONTENT was silently clipped to 200 chars with no marker anywhere."""
    long_label = "W" * 400
    reader = ScriptedReader([element(1, name=long_label)], known_total=1, dropped=0)
    observation = _world(reader)

    block = spatial_text_of(observation)

    assert block is not None
    served = block["regions"][0]
    assert len(served["text"]) == 200
    assert served["text_truncated"] is True


def test_a_short_label_carries_no_truncation_marker() -> None:
    reader = ScriptedReader([element(1, name="short")], known_total=1, dropped=0)
    observation = _world(reader)

    served = spatial_text_of(observation)["regions"][0]

    assert served["text_truncated"] is False


def test_exactly_two_hundred_chars_is_not_truncated() -> None:
    """The clip fires only when there is something to clip — boundary pinned."""
    exact = "W" * 200
    reader = ScriptedReader([element(1, name=exact)], known_total=1, dropped=0)
    observation = _world(reader)

    served = spatial_text_of(observation)["regions"][0]

    assert len(served["text"]) == 200
    assert served["text_truncated"] is False


# --- (e) regression: every already-labelled path keeps its marker -----------------------------


def test_a_degraded_read_still_ships_no_block_and_no_omitted_count() -> None:
    reader = ScriptedReader(None, known_total=45, available=False)
    observation = _world(reader)

    assert observation.ocr_text is None
    block = spatial_text_of(observation)
    assert block is None


def test_an_empty_read_still_reports_region_count_zero_without_inventing_omissions() -> None:
    reader = ScriptedReader([], known_total=0)
    observation = _world(reader)
    observation.ocr_text = []

    block = spatial_text_of(observation)

    assert block is not None
    assert block["region_count"] == 0
    assert block["omitted_count"] == 0


def test_the_cap_drop_and_the_block_drop_do_not_double_count() -> None:
    """``omitted_count`` is a COUNT, never a sum of the two channels.

    The reader reported 5 dropped, so the conversion only ever saw 30 elements; 20 of
    those were served and 10 fell past the block cap.  The 5 the reader dropped are NOT
    among the 30, so the two channels are disjoint and the total is 15, not 20.
    """
    reader = ScriptedReader(
        [element(i, y=i * 24) for i in range(UIA_MAX_ELEMENTS)],
        known_total=UIA_MAX_ELEMENTS + 5,
        dropped=5,
    )
    observation = _world(reader)

    block = spatial_text_of(observation)

    assert block is not None
    assert block["region_count"] == SPATIAL_TEXT_CAP
    assert block["omitted_count"] == 15  # 10 block-cap + 5 reader-reported
    assert block["region_count"] + block["omitted_count"] == UIA_MAX_ELEMENTS + 5


def test_a_hand_built_observation_with_no_drop_channel_reports_zero_omissions() -> None:
    """A snapshot from a reader that predates the channel is never over-counted."""
    world = FakeComputerBackend()
    observation = world.observe()
    observation.ocr_text = [TextRegion(text="a", x=0, y=0, width=4, height=4)]

    block = spatial_text_of(observation)

    assert block is not None
    assert block["omitted_count"] == 0


# --- (f) pixel_evidence OFF still omits its field entirely ------------------------------------


def test_pixel_evidence_off_still_omits_its_field_but_keeps_the_truncation_marker() -> None:
    reader = ScriptedReader([element(0, name="W" * 400)], known_total=1, dropped=0)
    observation = _world(reader)

    block = spatial_text_of(observation, pixel_evidence=False)

    assert block is not None
    assert block["pixel_evidence_mode"] == "off"
    assert "pixel_evidence" not in block["regions"][0]
    # The additive marker is orthogonal to the opt-in measurement.
    assert block["regions"][0]["text_truncated"] is True


# --- producer-side unit pins: the real readers report their own drops -----------------------


def test_uia_reader_reports_the_elements_the_cap_dropped() -> None:
    """``UiaSemanticReader._dropped_for_total`` counts what ``get_Length`` promised but
    never visited."""
    drops = text_readers_module.UiaSemanticReader._dropped_for_total(45)
    assert drops["cap_exceeded"] == 45 - UIA_MAX_ELEMENTS
    assert drops["cap_exceeded"] == 15
    assert sum(drops.values()) == 15


def test_uia_reader_reports_nothing_dropped_for_a_small_surface() -> None:
    assert text_readers_module.UiaSemanticReader._dropped_for_total(3) == {
        "cap_exceeded": 0,
        "budget_exceeded": 0,
        "offscreen": 0,
        "unavailable": 0,
    }


def test_the_win32_walk_reports_its_drops_in_the_snapshot_shape() -> None:
    """The Win32 walk builds its breakdown DIRECTLY in ``_bounded_children`` (the dev
    copy's separate ``_dropped_for_total`` helper there was dead code and is not
    ported): the snapshot carries the breakdown under ``SNAPSHOT_DROPPED_KEY`` with the
    four-reason vocabulary, all-zero for an unpopulated walk."""
    drops = _empty_drops()
    assert set(text_readers_module.SNAPSHOT_DROP_REASONS) == set(drops)
    assert sum(drops.values()) == 0


def test_win32_walk_counts_every_remaining_sibling_on_the_cap_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1 (VAL-WP4b MINOR): when the level-2 walk fills the element cap exactly,
    the early exit must count EVERY level-1 sibling it abandons — not +1. Four
    level-1 hosts, the first host's 29 grandchildren fill the cap exactly (1 + 29 =
    30): the walk breaks with THREE remaining level-1 siblings, all of them known
    losses under ``budget_exceeded`` (the "healthy-looking partial read" failure D15
    targets: ``region_count + omitted_count`` must still account for them)."""
    import time as _time

    reader = object.__new__(text_readers_module.Win32TextReader)
    level1 = [10, 11, 12, 13]

    def _fake_chain(parent: int, *, limit: int) -> list[int]:
        if parent == 1:
            return level1
        assert parent == 10  # only the first host gets a second level
        return [100 + i for i in range(29)]  # exactly fills the cap with the host itself

    monkeypatch.setattr(text_readers_module, "_window_child_chain", _fake_chain)
    monkeypatch.setattr(
        reader,
        "_control_summary",
        lambda hwnd, focused=False: {
            "name": str(hwnd),
            "control_type": "Text",
            "automation_id": None,
            "value": None,
            "offscreen": False,
            "rect": (0.0, 0.0, 10.0, 10.0),
            "focused": focused,
            "source": "win32",
        },
    )

    elements, drops = reader._bounded_children(1, _time.perf_counter() + 5.0)

    assert len(elements) == UIA_MAX_ELEMENTS
    assert drops["budget_exceeded"] == 3  # the THREE remaining level-1 siblings
    assert drops["unavailable"] == 0
    assert drops["cap_exceeded"] == 0
    assert drops["offscreen"] == 0


def test_conversion_returns_its_drop_count_alongside_the_regions() -> None:
    snapshot = {
        "elements": [element(0), {"name": "x", "rect": None, "control_type": "Text"}],
        SNAPSHOT_DROPPED_KEY: {"cap_exceeded": 4},
    }

    regions, dropped = _uia_snapshot_to_text_regions(snapshot, (0, 0), (1.0, 1.0))

    assert len(regions) == 1
    assert dropped == 5  # 1 rect-less + 4 reader-reported


def test_a_snapshot_with_no_drop_key_is_never_invented() -> None:
    snapshot = {"elements": [element(0)]}

    _regions, dropped = _uia_snapshot_to_text_regions(snapshot, (0, 0), (1.0, 1.0))

    assert dropped == 0


def test_the_reader_drop_total_is_the_sum_of_its_breakdown() -> None:
    breakdown = {"cap_exceeded": 3, "budget_exceeded": 1, "offscreen": 2, "unavailable": 4}
    assert text_readers_module.snapshot_dropped_total(breakdown) == 10
    assert text_readers_module.snapshot_dropped_total(None) == 0
    assert text_readers_module.snapshot_dropped_total({"cap_exceeded": "x"}) == 0


# --- E-15: the cap/budget split ACCUMULATES over the walk's own counts -----------------------


def test_e15_cap_split_accumulates_over_the_walks_own_counts() -> None:
    """THE E-15 REGRESSION: the dev copy merged the cap/budget split with
    ``drops.update(...)``, which — because the split dict carries ALL FOUR reasons —
    ZEROED the ``offscreen``/``unavailable`` counts the loop had already accumulated.
    A read that dropped 2 offscreen elements and 1 unreadable, then hit the cap, used
    to report ``offscreen: 0, unavailable: 0``. Accumulation keeps them."""
    drops = _empty_drops()
    drops["offscreen"] = 2
    drops["unavailable"] = 1

    text_readers_module.UiaSemanticReader._account_cap_and_budget(drops, 45, visited=30)

    assert drops["offscreen"] == 2  # NOT overwritten to 0
    assert drops["unavailable"] == 1  # NOT overwritten to 0
    assert drops["cap_exceeded"] == 45 - UIA_MAX_ELEMENTS  # the split is ADDED
    assert drops["budget_exceeded"] == 0
    assert sum(drops.values()) == 18


def test_e15_budget_split_accumulates_the_same_way() -> None:
    drops = _empty_drops()
    drops["offscreen"] = 3

    text_readers_module.UiaSemanticReader._account_cap_and_budget(drops, 40, visited=12)

    assert drops["offscreen"] == 3
    assert drops["budget_exceeded"] == 28
    assert drops["cap_exceeded"] == 0


def test_e15_no_split_when_the_walk_served_everything() -> None:
    drops = _empty_drops()
    drops["offscreen"] = 1

    text_readers_module.UiaSemanticReader._account_cap_and_budget(drops, 10, visited=10)

    assert drops == {**_empty_drops(), "offscreen": 1}
