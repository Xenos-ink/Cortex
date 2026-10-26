"""AL-002 Amendment A3 (proposal): the pluggable text-substrate seam.

Owner directive D-3 (MISSION-CRF-AVR-009): the spatial-text substrate must be FIXABLE
via an OPTIONAL side-install — a DOM-aware substrate or a real visual OCR. UIA stays the
DEFAULT; the MCP auto-detects an installed side substrate and uses it automatically;
installing it is the user's implicit acceptance of the added latency. No configuration
is consulted or required — detection is by a well-known package name.

Design of record: ``research/AVR009-substrate-design.md`` (Amendment A3 proposal).
LAB form (recorded deviation): the seam wires into the OCR-once spatial-text build
(``visual_views.spatial_text_of``), NOT at backend init — ``backend.py`` stays
byte-identical (the AL-002 integration table forbids touching it, and the capture
critical path must stay stock). Without a side package the block differs from the A2
baseline by exactly the one additive ``"substrate": "uia"`` key, the absent-request
path gains ZERO new work (H19 byte-identity holds — the detection never runs there),
and A2's pixel_evidence defaults are untouched.

Layering rule (the ``visual_views.py`` precedent): this module imports ONLY ``models`` +
stdlib + PIL — never ``agent``/``server``/``verification``/``backend``.

Components:

- :class:`TextSubstrate` — the protocol: ``available() -> bool`` and
  ``regions(monitor, verdict, timeout_budget, *, frame=None) -> list[TextRegion]``
  (screenshot-local coordinates; ``confidence`` real or None — never invented; bounded
  by the same caps as the UIA needle: 30 regions / 50 ms-class budget —
  ``backend.py:140`` / ``backend.py:142``).
- :class:`UiaSubstrate` — the DEFAULT implementation (always available): wraps the
  existing UIA-derived ``Observation.ocr_text`` population (``backend.py:3665``); the
  needle itself is never re-run (the OCR-once doctrine).
- :func:`detect_side_substrate` — the auto-detect probe, IN ORDER (A3 §1):
  (1) ``importlib.util.find_spec("cortex_text_ocr")`` (an installed side-package, or any
  ``sys.path``-visible one); (2) the same name via an explicit ``PYTHONPATH``
  side-directory scan (for launches whose ``sys.path`` does not carry ``PYTHONPATH``);
  (3) nothing -> the UIA default. Detection is side-effect-free and never raises; a
  package that is ABSENT or UNIMPORTABLE reports ``available() -> False`` (honest
  absence — the EXP-020.1 UNAVAILABLE-DEFEASIBLE class), while a package that LOADS but
  violates the call contract fails CLOSED with :class:`SubstrateContractError` (the
  caller records an honest ``substrate_error`` and serves the UIA regions).
- :class:`SideOcrSubstrate` — the visual-OCR side-arm STUB: binds the side package's
  documented module-level entrypoint
  ``regions(monitor, verdict, timeout_budget, *, frame=None)`` and validates its result
  fail-closed (list, <= 30 entries, well-typed screenshot-local fields, confidence
  None/float-in-[0,1]). The OCR ENGINE itself is FUTURE WORK (Windows.Media.Ocr via a
  bounded PowerShell/WinRT probe — UNAVAILABLE-DEFEASIBLE on this machine per
  ``experiments/EXP-020-card.md:142``); the DOM-aware substrate is an interface stub in
  the design (``dom:<name>``), not implemented here.
- :func:`merge_substrate_regions` — UIA first, side regions appended after, duplicates
  (positive-area bbox overlap) dropped — UIA wins every overlap; zero-area boundary
  touch is non-intersecting (the W5.1 zoom precedent).
- :func:`substrate_pass` — the single seam call used by ``spatial_text_of``: detect,
  merge, and produce the additive block provenance keys. ALL of it happens INSIDE the
  single OCR-once build — H21' is untouched (the merge shapes WHAT the single build
  computes, never the computation count).

Coordinate doctrine (AL-002 I-6): side-substrate regions are consumed in CANONICAL
SCREENSHOT space, exactly like UIA regions — the crop origin ships as block metadata,
never rewritten here.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from PIL import Image

from .models import MonitorInfo, Observation, TextRegion

#: The well-known side-package name the side-install ships (A3 §1). Detection is BY
#: THIS NAME only — there is deliberately no config file, env toggle, or flag: installing
#: the package IS the opt-in (owner directive D-3, zero-config auto-detect).
SIDE_SUBSTRATE_PACKAGE = "cortex_text_ocr"

#: Region cap for one side-substrate call: the SAME bounded-list class as the UIA
#: needle (``UIA_MAX_ELEMENTS = 30``, ``backend.py:140``). More regions is a contract
#: violation (fail-closed), never a silent truncation of unvalidated data.
SIDE_SUBSTRATE_MAX_REGIONS = 30

#: Wall-clock budget passed to one side-substrate call, in seconds: the SAME 50 ms-class
#: hard wall as one UIA semantic read (``UIA_READ_BUDGET_SECONDS = 0.05``,
#: ``backend.py:142``). The substrate must self-bound; its ACTUAL elapsed time is
#: reported in the block (``substrate_ms``) — cost is REPORTED, never gated (the owner's
#: implicit latency acceptance). The full A3 backend-init form would enforce a hard
#: worker-thread deadline; this LAB stub is synchronous (a recorded stub-scope
#: limitation — it proves the contract, it does not host untrusted engines).
SIDE_SUBSTRATE_BUDGET_SECONDS = 0.05

#: The stock text-truncation convention of the UIA needle (``backend.py:2244``).
_TEXT_TRUNCATE = 200


class SubstrateContractError(Exception):
    """A side substrate violated the TextSubstrate contract (A3 §2; fail-closed).

    Raised by :class:`SideOcrSubstrate` — never by the default UIA path. The caller
    (``substrate_pass``) degrades to the UIA regions + an honest ``substrate_error``
    record; a broken side substrate can never fabricate a served region and can never
    fail an observation.
    """


@dataclass(frozen=True)
class SubstrateVerdict:
    """Lightweight, duck-compatible stand-in for ``CoordinateVerdict`` (backend.py:742).

    The substrate contract passes the coordinate verdict to the side package so it can
    convert its own measurements into screenshot-local coordinates the same way the
    UIA needle does (multiply by the measured scale; subtract the crop origin). The
    backend's real ``CoordinateVerdict`` satisfies this shape; ``backend`` is
    intentionally NOT imported (layering rule).
    """

    space: str
    scale_x: float
    scale_y: float


class TextSubstrate(Protocol):
    """A3 §2: one text-region source behind the spatial-text block.

    ``name`` is the provenance token shipped in the block's ``substrate`` key
    (``"uia"`` | ``"ocr:<name>"`` | ``"dom:<name>"``). ``regions`` returns regions in
    CANONICAL SCREENSHOT space (the ``models.TextRegion`` convention): ``confidence``
    is the substrate's REAL measured value or None — never an invented 0.0 and never a
    replacement for the UIA ``confidence=None`` truth (A1.1 doctrine). ``monitor`` is
    the capture ``MonitorInfo`` (crop origin = ``bounds[0:2]``; None in fake worlds ->
    origin (0, 0)); ``verdict`` duck-types ``CoordinateVerdict``; ``timeout_budget`` is
    the wall-clock SECONDS the substrate must self-bound; ``frame`` is the
    observation's pixel source for pixel-based substrates (DOM substrates ignore it).
    """

    name: str

    def available(self) -> bool:
        """Cheap, side-effect-free availability (re-consultable per observation)."""
        ...

    def regions(
        self,
        monitor: MonitorInfo | None,
        verdict: Any,
        timeout_budget: float,
        *,
        frame: Image.Image | None = None,
    ) -> list[TextRegion]:
        """The substrate's regions for this observation (screenshot-local, bounded)."""
        ...


class UiaSubstrate:
    """The DEFAULT substrate (always available): the stock UIA needle's regions.

    Wraps the existing UIA-derived ``ocr_text`` population (``backend.py:3665`` /
    ``backend.py:2215-2245``). ``available()`` is True unconditionally — the needle is
    the stock read on every install, and it is the ALWAYS-RUN fallback when a side
    substrate fails (A3 §5: the substrate can fail; the observation cannot). The needle
    itself is NOT re-run: ``Observation.ocr_text`` was already populated at capture
    (the AL-002 OCR-once doctrine); this wrapper only exposes it behind the protocol.
    """

    name = "uia"

    def __init__(self, observation: Observation) -> None:
        self._observation = observation

    def available(self) -> bool:
        return True

    def regions(
        self,
        monitor: MonitorInfo | None,
        verdict: Any,
        timeout_budget: float,
        *,
        frame: Image.Image | None = None,
    ) -> list[TextRegion]:
        regions = self._observation.ocr_text
        return list(regions) if regions else []


def _load_side_module() -> Any:
    """Locate and import the side package (A3 §1 probe order); None when absent.

    (1) the import probe ``find_spec`` — covers site-packages installs AND any
    ``sys.path``-visible directory (PYTHONPATH directories are on ``sys.path``);
    (2) an explicit ``PYTHONPATH`` side-directory scan — for launches whose interpreter
    ``sys.path`` does not carry ``PYTHONPATH`` (embedded/frozen launches), the
    side-install stays a drop-in directory; (3) None -> the UIA default. Never raises:
    a broken/unimportable side package degrades to honest absence
    (``available() -> False``), exactly the EXP-020.1 UNAVAILABLE-DEFEASIBLE class.
    """
    # (1) installed side-package (import probe)
    try:
        spec = importlib.util.find_spec(SIDE_SUBSTRATE_PACKAGE)
    except (ImportError, ValueError, AttributeError):
        spec = None
    if spec is not None:
        try:
            return importlib.import_module(SIDE_SUBSTRATE_PACKAGE)
        except Exception:  # noqa: BLE001 - a broken install degrades to absence, never raises
            return None
    # (2) the same package name via a PYTHONPATH side-directory
    for raw_entry in os.environ.get("PYTHONPATH", "").split(os.path.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        for candidate in (
            os.path.join(entry, SIDE_SUBSTRATE_PACKAGE, "__init__.py"),
            os.path.join(entry, SIDE_SUBSTRATE_PACKAGE + ".py"),
        ):
            if not os.path.isfile(candidate):
                continue
            try:
                spec = importlib.util.spec_from_file_location(SIDE_SUBSTRATE_PACKAGE, candidate)
            except (ValueError, ImportError):
                continue
            if spec is None or spec.loader is None:
                continue
            try:
                module = importlib.util.module_from_spec(spec)
                sys.modules[SIDE_SUBSTRATE_PACKAGE] = module
                spec.loader.exec_module(module)
                return module
            except Exception:  # noqa: BLE001 - broken side dir: honest absence
                sys.modules.pop(SIDE_SUBSTRATE_PACKAGE, None)
                continue
    # (3) nothing -> UIA default
    return None


class SideOcrSubstrate:
    """The visual-OCR side-arm STUB: binds the side package's documented entrypoint.

    Contract (A3 §3, the stability contract for side-install authors): the package
    :data:`SIDE_SUBSTRATE_PACKAGE` exposes a module-level
    ``regions(monitor, verdict, timeout_budget, *, frame=None) -> list`` whose entries
    are dicts (or TextRegion-like objects) carrying ``text`` (non-empty str), ``x``/
    ``y`` (>= 0), ``width``/``height`` (> 0, all numeric, screenshot-local) and
    ``confidence`` (None or float in [0, 1]). EVERY violation — entrypoint missing or
    non-callable, non-list result, more than :data:`SIDE_SUBSTRATE_MAX_REGIONS`
    entries, malformed/typed-wrong fields, or ANY exception from the side call — raises
    :class:`SubstrateContractError`: the caller fails CLOSED to the UIA regions and
    records an honest ``substrate_error``. The OCR engine itself is FUTURE WORK
    (Windows.Media.Ocr is UNAVAILABLE-DEFEASIBLE on this machine,
    ``experiments/EXP-020-card.md:142``); this stub proves the seam, the contract, and
    the fail-closed degradation.
    """

    name = "ocr:" + SIDE_SUBSTRATE_PACKAGE

    def __init__(self, module: Any = None) -> None:
        self._module = module

    def available(self) -> bool:
        """True only when the side package LOADS and exposes a callable entrypoint."""
        module = self._module if self._module is not None else _load_side_module()
        self._module = module
        return module is not None and callable(getattr(module, "regions", None))

    def regions(
        self,
        monitor: MonitorInfo | None,
        verdict: Any,
        timeout_budget: float,
        *,
        frame: Image.Image | None = None,
    ) -> list[TextRegion]:
        """Call the side package's entrypoint and validate its result (fail-closed)."""
        module = self._module if self._module is not None else _load_side_module()
        if module is None:
            raise SubstrateContractError(
                f"side package {SIDE_SUBSTRATE_PACKAGE!r} is not importable."
            )
        entrypoint = getattr(module, "regions", None)
        if not callable(entrypoint):
            raise SubstrateContractError(
                f"side package {SIDE_SUBSTRATE_PACKAGE!r} exposes no callable "
                f"regions(monitor, verdict, timeout_budget, *, frame=None) entrypoint."
            )
        try:
            raw = entrypoint(monitor, verdict, float(timeout_budget), frame=frame)
        except SubstrateContractError:
            raise
        except Exception as exc:  # noqa: BLE001 - any side failure is a contract failure
            raise SubstrateContractError(
                f"side substrate call failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _normalize_side_regions(raw)


def _normalize_side_regions(raw: Any) -> list[TextRegion]:
    """Validate + normalize the side result into ``TextRegion``s (fail-closed, A3 §2)."""
    if not isinstance(raw, list):
        raise SubstrateContractError(
            f"side substrate returned {type(raw).__name__}; contract requires a list."
        )
    if len(raw) > SIDE_SUBSTRATE_MAX_REGIONS:
        raise SubstrateContractError(
            f"side substrate returned {len(raw)} regions; contract cap is "
            f"{SIDE_SUBSTRATE_MAX_REGIONS}."
        )
    regions: list[TextRegion] = []
    for index, entry in enumerate(raw):
        text, x, y, width, height, confidence = _region_fields(entry, index)
        regions.append(
            TextRegion(
                text=text[:_TEXT_TRUNCATE],
                x=x,
                y=y,
                width=width,
                height=height,
                confidence=confidence,
            )
        )
    return regions


def _region_fields(entry: Any, index: int) -> tuple[str, int, int, int, int, float | None]:
    """Extract + validate one side entry's fields (dict or TextRegion-like)."""
    if isinstance(entry, dict):
        getter = entry.get
    elif hasattr(entry, "text"):
        getter = lambda key, _entry=entry: getattr(_entry, key, None)  # noqa: E731
    else:
        raise SubstrateContractError(
            f"side region #{index}: entries must be dicts or TextRegion-like objects; "
            f"got {type(entry).__name__}."
        )
    text = getter("text")
    if not isinstance(text, str) or not text:
        raise SubstrateContractError(
            f"side region #{index}: 'text' must be a non-empty str; got {text!r}."
        )
    coords: list[int] = []
    for key in ("x", "y", "width", "height"):
        value = getter(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SubstrateContractError(
                f"side region #{index}: {key!r} must be numeric; got {value!r}."
            )
        coords.append(int(value))
    x, y, width, height = coords
    if x < 0 or y < 0:
        raise SubstrateContractError(
            f"side region #{index}: screenshot-local x/y must be >= 0; got ({x}, {y})."
        )
    if width <= 0 or height <= 0:
        raise SubstrateContractError(
            f"side region #{index}: width/height must be > 0; got ({width}, {height})."
        )
    confidence = getter("confidence")
    if confidence is not None:
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise SubstrateContractError(
                f"side region #{index}: 'confidence' must be None or a float in "
                f"[0, 1]; got {confidence!r}."
            )
        confidence = float(confidence)
    return text, x, y, width, height, confidence


def boxes_overlap(a: TextRegion, b: TextRegion) -> bool:
    """POSITIVE-AREA bbox intersection (a zero-area boundary touch does NOT count —
    the W5.1 zoom precedent, ``implementations/AL-002/README.md`` deviation 11)."""
    return (
        a.x < b.x + b.width
        and b.x < a.x + a.width
        and a.y < b.y + b.height
        and b.y < a.y + a.height
    )


def merge_substrate_regions(
    uia_regions: list[TextRegion], side_regions: list[TextRegion]
) -> tuple[list[TextRegion], int]:
    """A3 §4 merge: UIA first, side appended after, duplicates dropped — UIA wins.

    A side region overlapping (positive area) ANY already-kept region is a duplicate
    and is dropped; the returned count is the number dropped as duplicates. The side
    substrate only ADDS coverage beyond the trusted default needle.
    """
    kept = list(uia_regions)
    deduped = 0
    for region in side_regions:
        if any(boxes_overlap(region, existing) for existing in kept):
            deduped += 1
        else:
            kept.append(region)
    return kept, deduped


def substrate_pass(
    observation: Observation,
    uia_regions: list[TextRegion],
    frame_once: Callable[[], Image.Image | None],
) -> tuple[list[TextRegion], dict[str, Any]]:
    """The A3 seam, called INSIDE the single OCR-once spatial-text build.

    Detects the side substrate (zero-config, §1); when one reports available, calls it
    (outside the capture critical path — this runs post-capture, in the block build),
    merges its regions AFTER the UIA needle's (§4), and returns the merge-region list
    plus the ADDITIVE block provenance keys, in shipping order:

    - no side substrate (absent, unimportable, or ``available() -> False``):
      ``{"substrate": "uia"}`` — the ONLY delta vs the A2 baseline block;
    - side substrate succeeded: ``{"substrate": "ocr:<name>", "substrate_merged": n,
      "substrate_deduped": m, "substrate_ms": ms}``;
    - side substrate failed the contract: ``{"substrate": "uia", "substrate_error":
      reason, "substrate_ms": ms}`` — fail-open to the default, never fail the
      observation, and the served regions are ALWAYS the UIA truth in that case.

    ``frame_once`` lazily supplies the observation's pixels (stash, else at most ONE
    payload decode per build — the A1.1 fetch semantics) for pixel-based substrates;
    when no side substrate runs, it is never called, so the default world still never
    fetches/decodes the frame (the A2 canary property). H21': all of this happens
    inside the single build — the computation count is untouched.
    """
    keys: dict[str, Any] = {"substrate": UiaSubstrate.name}
    side = detect_side_substrate()
    if not side.available():
        return uia_regions, keys
    started = time.perf_counter()
    try:
        verdict = SubstrateVerdict(
            space=observation.coordinate_space.value,
            scale_x=float(observation.coordinate_scale_x),
            scale_y=float(observation.coordinate_scale_y),
        )
        side_regions = side.regions(
            observation.monitor,
            verdict,
            SIDE_SUBSTRATE_BUDGET_SECONDS,
            frame=frame_once(),
        )
        merged, deduped = merge_substrate_regions(uia_regions, side_regions)
    except SubstrateContractError as exc:
        keys["substrate_error"] = str(exc)
        keys["substrate_ms"] = (time.perf_counter() - started) * 1000.0
        return uia_regions, keys
    keys["substrate"] = side.name
    keys["substrate_merged"] = len(merged) - len(uia_regions)
    keys["substrate_deduped"] = deduped
    keys["substrate_ms"] = (time.perf_counter() - started) * 1000.0
    return merged, keys


def detect_side_substrate() -> SideOcrSubstrate:
    """The A3 §1 detection: return the side-arm handle (``available()`` tells truth).

    Never raises and never imports anything unless a side package is actually present;
    the absent world costs one ``find_spec`` + (only when that misses) a PYTHONPATH
    file-existence scan — and this function is consulted ONLY inside the OCR-once
    spatial-text build, so the absent-request default path performs zero new work (H19).
    """
    return SideOcrSubstrate(_load_side_module())
