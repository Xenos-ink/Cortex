# ROADMAP

This file is the planned-work ledger for Cortex: what ships in each upcoming version — written for the owner and for any AI agent continuing the work.

## Purpose and scope

This is the future-improvements backlog for Cortex (`computer-use-mcp`). It tells the
owner — or any future AI agent — exactly what to implement in each upcoming version,
why, and how to prove it was implemented. Items come from three sources: defects and
deferred ideas discovered during the maintainer's internal performance & effectiveness
QA cycle (internal designation perf-004), the research digests produced during that
cycle, and user-commissioned follow-ups. Everything here is *planned*, not shipped:
the shipped history lives in [VERSIONS.md](VERSIONS.md), and the authoritative
released version is `pyproject.toml`.

## How to use this file

- **Pick items per version.** Each priority group below maps to a target version
  (P0 → v0.6.0, P1 → v0.7.0, P2 → v0.8.0+). Work through a group top-down; item IDs
  (R-01 …) are stable references — never renumber, only append.
- **Every item is self-contained.** Each carries a `Status` label, the `Problem`
  (with the concrete symptoms), the suggested `Approach`, an `Evidence` pointer, and
  an `Accept` line — the acceptance test that defines "done" on this machine. An
  implementer should be able to work from the item text alone.
- **Evidence pointers:** entries marked `(QA: perf-004 evidence)` refer to the
  maintainer-local QA evidence tree for that cycle. Those artifacts are **not** part
  of the public repository; maintainers resolve them locally, and public readers can
  treat the pointers as provenance placeholders. A few items point at public,
  in-repository paths instead — those are usable by everyone.
- **Status labels:** `DISCOVERED-DEFERRED` (defect found and consciously deferred
  during the perf-004 QA cycle), `RESEARCH-DEFERRED` (deferred research item from the
  cycle's research digests), `USER-COMMISSIONED` (requested by the owner based on QA
  findings), `OPTIONAL-RESUME` (a half-finished, user-accepted piece of work that can
  be resumed to full completion).
- **Honesty rule:** record only numbers that exist in the referenced evidence. If a
  measurement is missing, measure it during implementation instead of estimating.

## P0 — v0.6.0 (quality & reliability)

### R-01 Verification tier defaults: pixel-diff false-negatives on type actions

- **Status:** DISCOVERED-DEFERRED.
- **Problem:** the pixel-diff verification tier produced false-negatives on `type`
  actions (verdict `failed` although the effect had landed), which forced chain stops
  and single-action fallbacks — 40+ occurrences in the internal live validation run.
- **Approach:** make the deterministic / observe-confirm tier the default for `type`
  and `keypress`; demote pixel-diff to an ambiguous-band escalation (used only when
  the deterministic tier is inconclusive).
- **Evidence:** (QA: perf-004 evidence — live-run progress log and post-fix
  comparison notes).
- **Accept:** type chains complete without fallback in a live bridge; zero false-fails
  in a 50-type loop.

### R-02 Safety text-classifier tuning (false positives on benign text)

- **Status:** DISCOVERED-DEFERRED.
- **Problem:** benign strings were rejected as secrets by the safety text heuristic —
  observed: "black-scholes closed-form formula expository", "binomial option pricing
  model Cox Ross Rubinstein", "closed form", "call and put prices", and DOI tokens.
- **Approach:** tune the heuristic against a benign-corpus test suite **while keeping
  true secret/credential detection**. Both-direction tests are mandatory: the benign
  corpus must pass AND the secret corpus must still be blocked. Do NOT weaken real
  protection.
- **Evidence:** (QA: perf-004 evidence — live-run anomaly log).
- **Accept:** the benign corpus passes; the secret corpus is still blocked.

### R-03 ensure_app minor bug: empty-title focus_window call

- **Status:** DISCOVERED-DEFERRED.
- **Problem:** `ensure_app` builds a `focus_window` call with an empty title (A7b
  finding), which surfaces as a validation error instead of a focused reattach.
- **Approach:** fix the title resolution in `ensure_app` so the call is only built
  when a non-empty target exists; fail loudly (typed error) when it cannot resolve.
- **Evidence:** (QA: perf-004 evidence — still-open findings list).
- **Accept:** focused reattach completes without a validation error.

### R-04 Keystroke-burst resilience (B13)

- **Status:** DISCOVERED-DEFERRED.
- **Problem:** the reference machine randomly drops 1–25-character bursts mid-type —
  observed corrupting editor source once and dropping spreadsheet rows repeatedly
  during internal live runs.
- **Approach:** chunked typing + chunk-integrity verification + a *verified retype*
  policy (retype only after integrity verification fails; never blind-retype).
- **Evidence:** (QA: perf-004 evidence — live-run anomaly log).
- **Accept:** 200 typed characters across 20 bursts with disk-verified integrity and
  zero manual fixes.

### R-05 B5 residual: backslash-type wedge — root-cause or accept

- **Status:** DISCOVERED-DEFERRED.
- **Problem:** the backslash-type wedge was mitigated (settle handling + watchdog
  tests) but never root-caused; it is recorded as still-open rather than fixed.
- **Approach:** re-run the functional probe (reproduction scripts live in the local
  QA evidence tree); then either close the item with a documented root cause or
  document it as an accepted risk in the project docs.
- **Evidence:** (QA: perf-004 evidence — I/O parity report, `keyboard_delivery_gate`
  key; change log).
- **Accept:** the item is closed with either a root cause or an explicit
  accepted-risk note — no silent residue.

### R-18 Follow_ups invalid-item errors are unstructured

- **Status:** DISCOVERED-DEFERRED (red-team finding, severity MEDIUM).
- **Problem:** queue items that are `ActionSpec`-valid but `GroundedAction`-invalid —
  a target-less `focus_window`, a half-specified `drag` (one endpoint missing), a
  1-key `hotkey` — raise an uncaught `ValidationError` inside the `follow_ups` queue.
  The effect is fail-closed (zero items dispatch) but the host receives an
  unstructured error instead of a typed rejection.
- **Approach:** map queue-item validation onto the typed `invalid_action` rejection
  path, with teach-in-text hints listing the valid shapes.
- **Evidence:** (QA: perf-004 evidence — red-team report).
- **Accept:** each malformed class returns a typed rejection listing the valid shapes.

### R-19 FocusGuard hwnd-recycle identity check

- **Status:** DISCOVERED-DEFERRED (red-team finding, severity LOW-MEDIUM).
- **Problem:** a recycled hwnd now owned by a foreign process matches the bound
  identity by hwnd equality alone, so the guard can bless the wrong window.
- **Approach:** verify process/title-class alongside the hwnd at each pre-dispatch
  check (cheap; the pid is already available in the observation identity).
- **Evidence:** (QA: perf-004 evidence — red-team report).
- **Accept:** a recycled-hwnd test dispatches to the new foreign owner →
  `FOCUS_TAKEN_BY`.

### R-20 Re-anchor causality check for launcher/dialog anchors

- **Status:** DISCOVERED-DEFERRED (red-team finding, severity LOW).
- **Problem:** re-anchoring from launcher/dialog anchors accepts any titled
  foreground window without a causality check, so an unrelated foreground window can
  be adopted as the session anchor.
- **Approach:** re-anchor only to windows in the session's launched/attached set or
  to same-process descendants; refuse everything else.
- **Evidence:** (QA: perf-004 evidence — red-team report).
- **Accept:** an adversarial foreign-titled window is not adopted as an anchor.

## P1 — v0.7.0 (performance & driver economics)

### R-06 Driver-economics pack: the "fast-model profile"

- **Status:** USER-COMMISSIONED.
- **Problem:** internal session forensics attributed the overwhelming majority of
  wall time to driver think time, and wall-clock duration on composite tasks was
  identified as an improvement area; the runtime defaults are not tuned for a
  cheap/fast driver model.
- **Approach:** document and default a "fast-model profile":
  `include_screenshot_after=false` on `computer_execute`, text_summary-first
  verification, and deliberate `follow_ups` usage. Refine the `follow_ups`
  digest-surprise semantics so deliberate screen-changing chains with an explicit
  stated intent are allowed to continue instead of stopping at the first digest
  surprise.
- **Evidence:** (QA: perf-004 evidence — session forensics; live-run log).
- **Accept:** a scripted driver loop completes an h2-class task with ≤ N turns —
  define N and document the benchmark in `docs/`.

### R-07 JPEG q85 opt-in payload encoding

- **Status:** RESEARCH-DEFERRED.
- **Problem:** PNG screenshots dominate payload size/latency on the observe path; JPEG
  q85 is a candidate cheaper encoding (~9 ms encode; both major vision APIs accept
  it), with a quality tradeoff on flat UI content.
- **Approach:** implement as an opt-in flag; the semantic-verification diff path must
  stay lossless regardless of the transport encoding.
- **Evidence:** (QA: perf-004 evidence — Win32 research digest).
- **Accept:** opt-in flag exists, the decided payload is smaller, verification
  behavior is unchanged (lossless diff path).

### R-08 Internal-loop multi-action decide batching

- **Status:** RESEARCH-DEFERRED.
- **Problem:** the internal `run_goal` loop decides one action per model call. Multi-
  action decide batching was descoped in perf-004 because no `VISION_API_KEY` was
  configured to measure it; the host-path `follow_ups` batching shipped instead.
  UFO2 reported 13.30 → 7.40 steps/task for this technique.
- **Approach:** enable internal decide batching when a provider key is configured;
  keep single-action behavior as the fallback when the provider rejects batches.
- **Evidence:** (QA: perf-004 evidence — architecture research digest, adoption #1).
- **Accept:** measured steps/task reduction on a live provider.

### R-09 Pre-downscale capture option

- **Status:** RESEARCH-DEFERRED.
- **Problem:** downscaling captures before sending would cut payload/encode cost, but
  in perf-004 it was blocked on a coordinate-contract decision: a non-DPI-ratio
  downscale would make the coordinate space `UNVERIFIABLE` (fail-closed), so it cannot
  ship as a silent option.
- **Approach:** first write and accept a documented coordinate-contract extension
  that defines how downscaled captures keep verifiable coordinate integrity; only
  then implement the capture option on top of it.
- **Evidence:** (QA: perf-004 evidence — Win32 research digest, deferred item).
- **Accept:** contract extension documented and reviewed; capture option ships only
  with verifiable coordinate integrity preserved.

### R-10 DXGI Desktop Duplication capture

- **Status:** RESEARCH-DEFERRED.
- **Problem:** `mss` captures through GDI; DXGI Desktop Duplication could lower the
  capture floor (studied ceiling ≤ 25 ms gain) but requires high-risk D3D11+COM
  plumbing via dependency-free ctypes.
- **Approach:** implement behind an internal engine switch with capture-parity tests
  against the `mss` baseline; replace the default only after parity is proven.
- **Evidence:** (QA: perf-004 evidence — Win32 research digest).
- **Accept:** capture parity demonstrated plus a measured gain before any replacement
  of the `mss` default.

## P2 — v0.8.0+ (capability & effectiveness)

### R-11 UIA hybrid control detection + UIA precondition validation

- **Status:** RESEARCH-DEFERRED.
- **Problem:** OCR/UIA are extension-point stubs; grounding and preconditions rely on
  pixels and window identity only. The cycle's research studied a ctypes-legal UIA
  hybrid: +9.86% failure recovery at < 1 s overhead.
- **Approach:** populate the existing `ui_elements` observation field from UIA
  snapshots and add UIA-based precondition validation to the pipeline (fail-closed
  when UIA data is absent, as today).
- **Evidence:** (QA: perf-004 evidence — architecture research digest).
- **Accept:** UIA-backed grounding/preconditions demonstrably improve failure
  recovery without regressing the fail-closed guarantees.

### R-12 Set-of-Mark overlays for grounding

- **Status:** RESEARCH-DEFERRED.
- **Problem:** coordinate grounding offers the model no labeled visual anchors.
- **Approach:** generate Set-of-Mark overlay images from UIA element data so the
  model can reference labeled controls instead of raw coordinates.
- **Dependencies:** R-11 (needs UIA data to label).
- **Accept:** overlays render on real windows and measurably improve grounding in a
  scripted comparison.

### R-13 Reasoning-effort / model knobs exposure to host configs

- **Status:** RESEARCH-DEFERRED.
- **Problem:** the provider's reasoning-effort and model knobs are fixed in code; the
  host cannot tune the cost/quality point per deployment.
- **Approach:** expose the knobs through server configuration (environment variables
  consistent with the existing `VISION_*` scheme) and document them in the README
  environment table.
- **Evidence:** (QA: perf-004 evidence — architecture research digest).
- **Accept:** knobs are settable from the host config and take effect on the next
  model call without code changes.

### R-14 Measured reference timings to supersede calibrated references

- **Status:** RESEARCH-DEFERRED.
- **Problem:** the hard-task benchmark set's reference durations are calibrated
  synthetic estimates, not measured runs (labeled as such in the references file).
- **Approach:** perform real measured reference runs on the same tasks and recompute
  the benchmark ratios when performed.
- **Evidence:** (QA: perf-004 evidence — references file; all references calibrated
  and labeled).
- **Accept:** references carry measured entries with the calibration labels updated;
  ratios recomputed and documented.

### R-15 Composite-task validation completion (optional resume)

- **Status:** OPTIONAL-RESUME.
- **Problem:** a composite multi-application validation task was accepted by the
  owner as a partial result: its comparison output sheet was left empty and the
  validation sheet was never produced.
- **Approach:** rebuild the comparison sheet from the recorded cell map, add the
  validation sheet, and rerun the verification — the full step-by-step resume recipe
  is preserved in the local QA evidence (the progress log's handoff section).
- **Evidence:** (QA: perf-004 evidence — progress log, handoff state section and the
  acceptance note that follows it).
- **Accept:** the task workbook contains the rebuilt comparison sheet and the
  validation sheet, with verification rerun and recorded.

### R-16 Live-provider benchmark mode

- **Status:** RESEARCH-DEFERRED.
- **Problem:** the benchmark harness runner is scripted-provider only (`RunnerProvider`
  is deterministic); no measured mode against a real vision provider exists.
- **Approach:** add a live-provider mode to `python -m benchmarks.runner` (e.g.
  `--provider live`) gated on a configured API key, reusing the existing harness and
  its "NOT benchmark scores" disclaimer discipline until results are real.
- **Evidence:** `benchmarks/` harness (shipped); the runner's current modes are
  documented in the README Benchmarks section.
- **Accept:** a live run completes end-to-end with per-task results recorded under
  the maintainer-local `evidence/` tree (not published).

### R-17 Bridge lifecycle hardening (keepalive / reconnect)

- **Status:** DISCOVERED-DEFERRED.
- **Problem:** during internal live runs the host killed idle bridge transports
  repeatedly (three observed occurrences), interrupting driver sessions.
- **Approach:** add keepalive/ping traffic or transparent reconnect to the
  `benchmarks/mcp_client.py` / driver bridge so idle transports survive host idle
  reaping; reconnects must re-establish session state or fail loudly.
- **Evidence:** (QA: perf-004 evidence — live-run anomaly log).
- **Accept:** a scripted idle-then-resume bridge session survives the same idle
  window without host-side transport death.

## Changelog note

When an item ships: **move it out of this file** and record it in the matching
`## vX.Y.Z` entry in [VERSIONS.md](VERSIONS.md) (Added / Changed / Fixed / Performance
sections + the compatibility-notes line), keeping the item ID in the entry text —
e.g. "(R-01)" — so history stays traceable. Version numbers in this file are targets,
not commitments; `pyproject.toml` remains the authoritative released version. New
items: append with the next free R-number and one of the four status labels; never
renumber or repurpose existing IDs.
