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
  (P0 → v0.7.0, P1 → v0.7.0, P2 → v0.8.0+). Work through a group top-down; item IDs
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

## P0 — v0.7.0 (safety & guard residuals discovered during v0.6.0)

### R-21 Safety text-classifier vocabulary gaps (polite destructives + unlabeled secrets)

- **Status:** DISCOVERED-DEFERRED (red-team findings RT-E8-01/02/03, severity
  MEDIUM each).
- **Problem:** destructive verbs in polite phrasings type at LOW risk — "please
  delete the model", "delete the database/table", "please remove the model
  weights", "wipe the disk now" all pass the safety gate and the classifier;
  space-separated or abbreviated credential phrasings ("api key: sk-…",
  "access key: …", "pwd is hunter2", "pass: hunter2x") bypass BOTH the safety
  gate and secret redaction, so raw secrets can reach the audit JSONL and tool
  responses; bare token values with no keyword label (ghp_…, xoxb-…, npm_…)
  match no redaction pattern and no safety marker.
- **Approach:** extend the token/phrase marker vocabulary and the risk
  classifier for polite destructive phrasings; add value-shape detection for
  bearer-token prefixes (ghp_/xoxb-/npm_ families) to redaction. The
  both-directions rule applies: the benign corpus must still pass AND the true
  secret/credential corpus must still block — do NOT weaken real protection.
- **Evidence:** (maintainer-local) `evidence/v06-006/redteam/report.md`
  (RT-E8-01/02/03; `artifacts/r02_nearmiss_results.json`).
- **Accept:** the red-team near-miss corpus blocks on both the safety and
  redaction layers; the R-02 benign corpus still passes unchanged.

### R-22 Re-anchor adoption residual (launch-act widening)

- **Status:** DISCOVERED-DEFERRED (red-team finding RT-E8-05, severity MEDIUM).
- **Problem:** ANY keypress into a `#32770`/explorer.exe anchor arms R-20's
  launch-act marker, so the NEXT window — even a foreign-process one — is
  adopted as the session anchor. The one-action bound holds and the allowlists
  still gate dispatch, but adoption is wider than R-20's causal intent.
- **Approach:** tighten launch-act arming (allowlist-bound anchors and/or
  chord+window-class pairing) without reintroducing the dead-anchor deadlock
  R-20 removed.
- **Evidence:** (maintainer-local) `evidence/v06-006/redteam/report.md`
  (RT-E8-05).
- **Accept:** a foreign window immediately following a keypress into a
  `#32770`/explorer.exe anchor is refused as anchor (named `REANCHOR_REFUSED`);
  R-20's documented positive adoption paths still work.
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

- **Status:** OBSOLETE (the internal autonomous loop was removed by user order —
  there is no internal decide phase to batch; host-path `follow_ups` batching is the
  surviving batching mechanism and remains fully supported).
- **Problem:** (historical) the internal autonomous loop decided one action per model
  call. Multi-action decide batching was descoped in perf-004 because no
  `VISION_API_KEY` was configured to measure it; the host-path `follow_ups` batching
  shipped instead. UFO2 reported 13.30 → 7.40 steps/task for this technique.
- **Evidence:** (QA: perf-004 evidence — architecture research digest, adoption #1).
- **Accept:** n/a — resolved by removal.

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
