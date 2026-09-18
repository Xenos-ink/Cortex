# VERSIONS

A per-release ledger for Cortex (`computer-use-mcp`). Newest first. Reconstructed
from the repository's git history (`git log --oneline`) and the
`docs/ARCHITECTURE.md` header. Internal engineering-process designations are omitted
on purpose — releases are described by content only. The version in `pyproject.toml`
is the authoritative released version.

**Convention for future entries:** while work is in flight, land its notes under the
topmost `## Unreleased` heading; at release time they are folded into a new
`## vX.Y.Z (date, mission)` entry with **Added / Changed / Fixed / Performance**
sections and a **Compatibility notes** line, and `## Unreleased` is emptied again.
Planned work per upcoming version: see **[ROADMAP.md](ROADMAP.md)**.

## v0.7.5 (2026-09-18, MISSION-CRF-AVR-010 — adaptive visual representations)

The observation family becomes adaptive: per-call `visual_view` (raw/grid) with a
`visual_view_density` control, a sixth observation-only `computer_zoom` tool (a fresh
native-resolution crop of a region), and an OCR-once spatial text layer attached to
observe-family results through a pluggable `TextSubstrate` seam whose default and
always-present substrate is the existing UIA read. Scope is honest: the optional
DOM engine package is NOT part of this release — the seam auto-detects an
optional side package (`cortex_text_ocr`) and falls back to UIA. (Amended in place
2026-09-18, AVR-010: the optional Windows.Media.Ocr side package now ships in-repo
at `sidepackages/cortex_text_ocr` — zero dependencies, never installed by default;
the `install`/`update` prompt offers it with default No, and installing it is the
user's acceptance of the added per-observation OCR latency, reported as
`substrate_ms`; `cortex-mcp uninstall-ocr` removes it; the package's PowerShell
JSON transport is UTF-8/control-char-safe (found and fixed in live use).)
Everything is
additive; the default path (no new parameter used) is byte-identical to v0.7.1, and
grounding, validation, safety, approval, the agent pipeline, and backend capture are
untouched. 67 net new tests (two new test files plus minimal extensions of seven
existing suites for the new signatures and the six-tool probe surface); suite now
1753 passed + 11 skipped. Production status: Cortex 0.7.5 is suitable for
production use, with deferred Phase 2 items explicitly treated as not fully
validated, especially OCR performance and `pixel_evidence` theme calibration.

### Added

- **`visual_view` / `visual_view_density` on `computer_observe`** (trailing optionals;
  the absent request = byte-identical pass-through of untouched stock bytes):
  "raw" (default) or "grid" (a derived coordinate-grid annotation; labels show
  canonical screenshot-space coordinates, `displayed + crop_origin = screen` is a
  reader-side fact). `visual_view_density` selects the grid density — closed enum
  `coarse|standard|fine` (default `coarse`) — and is ignored silently by raw/absent
  view requests. Unknown view/density names are rejected fail-closed (typed
  `invalid_visual_view`) BEFORE any capture; a view that cannot be derived never
  substitutes a raw image silently — the executed result is returned honestly with no
  image block and a `view_derive_failed` marker. Additive response keys
  (`visual_view`, `visual_view_scale`) report what was served.
- **`computer_zoom(session_id, region, visual_view, visual_view_density, pixel_evidence)`**
  — the sixth tool, OBSERVATION-ONLY (no action surface, no queue interaction): a
  FRESH capture through the existing lifecycle (a new `observation_id` every call, no
  cross-request frame caching; `screenshot_count` increments and the audit event
  carries `metadata={"source": "computer_zoom"}` even when a later check refuses).
  `region` is `[left, top, width, height]` in canonical screenshot space. Fail-closed
  region contract: malformed/non-int regions are rejected pre-capture, out-of-bounds
  regions or an unverifiable coordinate space against the fresh observation, and every
  violation is the typed `invalid_region` error naming the bounds — never a clipped
  guess, never a silent full-frame fallback. The served image is the NATIVE-RESOLUTION
  crop (no downscale, no upscale — the crop itself is the zoom); the stock outbound
  budget ladder still governs the served bytes and `visual_view_scale` reports any
  ladder-applied scale. `visual_view="grid"` renders the coordinate grid ON the crop
  with the crop origin baked into the label VALUES; the result metadata carries
  top-level `crop_region`. Text-mode sessions get the single-text-block treatment,
  never an image block.
- **OCR-once spatial text layer (`spatial_text`)** — ALWAYS attached on the observe
  family: UIA-derived text regions with bboxes in canonical coordinates, derived ONCE
  per Observation (cached on a private, never-serialized attribute; deliberately NO
  cross-observation store) and never part of any digest. A degraded read reports
  honest absence (`spatial_text` null plus `spatial_text_available: false`), never an
  empty block. `computer_zoom` filters the full cached block to the crop with
  `crop_region` + `crop_origin` provenance so a reader can convert crop-local to
  screenshot coordinates.
- **`TextSubstrate` pluggable seam (`text_substrates.py`)** — the substrate protocol
  behind the spatial text layer, with auto-detect of an optional side package
  (`cortex_text_ocr`, not shipped in this repository) and the UIA substrate as the
  always-present default and fallback; substrate load/derive failures degrade honestly.
- **`pixel_evidence` (opt-in, strict boolean)** — per-region measured-evidence scores
  (frozen formula) on the `spatial_text` regions when `pixel_evidence=true` is passed
  (`"pixel_evidence_mode": "on"`); the default OFF omits the field entirely and
  records `"pixel_evidence_mode": "off"` (no score computation). Non-boolean values
  are rejected fail-closed (`invalid_pixel_evidence`) BEFORE any capture.
- **Instrumentation** — `view_derive_ms` latency recording, the OCR-once compute
  counter (test-visible), and the additive `visual_view` / `visual_view_scale` /
  `pixel_evidence_mode` report keys.
- **Tests** — `tests/test_avr_visual_views.py` and `tests/test_avr_text_substrates.py`
  (byte-identical default path, view/density/pixel-evidence fail-closed matrices, the
  zoom region contract, the substrate seam, OCR-once semantics); seven existing suites
  minimally extended for the six-tool probe surface and the new signatures
  (`test_c059_install_cli.py`, `test_c060_probe_subcommand.py`, `test_p5_redteam.py`,
  `test_perf004_loop_economics.py`, `test_r2_redteam_speed.py`,
  `test_r3_install_cli_redteam.py`, `test_rem_g_plain_schemas.py`).

### Changed

- The served surface is six tools: `computer_zoom` joins `start_session`,
  `computer_observe`, `computer_screenshot`, `computer_execute`, `stop_session`. The
  `cortex-mcp probe` install verifier expects exactly the registered six-tool surface
  and still refuses any tool NOT in the set.
- **`cortex-mcp uninstall-ocr`** — removes the optional `cortex_text_ocr` side
  package from the managed venv (removed / not installed / no-venv are all honest
  no-ops); UIA remains the default substrate either way.
- **`cortex_text_ocr` robustness (live-found)**: PowerShell 5.1's `ConvertTo-Json`
  can emit raw C0 control characters and OEM-codepage stdout — real terminal text
  broke the JSON parse on the live desktop. `ocr.ps1` now forces UTF-8 (no BOM) and
  pre-escapes C0 characters; the parser accepts control characters (`strict=False`)
  with one sanitize-and-retry pass, stamping `json_sanitized` on saved regions. The
  seam's fail-open to UIA held throughout (verified live: parse failure → UIA truth
  + `substrate_error` recorded, no observation failure).
- README (tool reference, install-prompt probe steps) and docs/ARCHITECTURE.md
  (header, module map, tool-reference note) document the new tool, parameters, and
  modules.

### Performance

- The absent-request observe path pays zero view cost (pass-through; the default never
  reads the view registry). The spatial text block is built once per Observation and
  reused (OCR-once); `pixel_evidence` computes only when requested; grid derivations
  and substrate timing are recorded (`view_derive_ms`).

### Compatibility notes

- Wire change, additive only: a sixth tool (`computer_zoom`, observation-only — it
  never dispatches input and never touches the action queue) and trailing optional
  parameters on `computer_observe`. Omitting every new parameter returns the
  byte-identical v0.7.1 response (no new keys on the default path). Grounding,
  validation, safety, approval, agent, and backend capture are untouched. The OCR/DOM
  engine packages are NOT included — the seam auto-detects the optional
  `cortex_text_ocr` side package and UIA remains the default and fallback substrate.
  `cortex-mcp probe` now PASSes the six-tool surface; install prompts verified against
  the old five-tool expectation must be re-read.

## v0.7.1 (2026-09-17, ORVEX-CORTEX-v07-008 — live-session field defects)

The three defect classes that forced a live-field tester to bypass Cortex entirely
while driving Blender (OpenGL, no accessibility tree) on v0.7.0 are fixed at the
design level: visual verification confidently reporting `Mean pixel difference
0.000000` from a dead capture source, `ensure_app` reporting `NO_INSTANCE` while
silently spawning nothing, and per-character `type` inserting zero characters while
chords worked. Every fix is additive, honest-by-contract, and generic (never
app-specific), each carried by its own executable test file plus a cross-track
integration file (78 net new tests; suite now 1672 passed + 10 skipped).

### Added

- **Launch path mapping — `CORTEX_LAUNCH_PATHS`** (env; `needle=path;needle=path`,
  fail-safe parse): an explicitly-authorized `ensure_app` launch of a GUI app not on
  PATH resolves the needle in a fixed order — **mapped path → `PATH`
  (`shutil.which`) → Microsoft Store execution alias → raw-name spawn**. The needle
  side passes the same charset gate as the launch itself (case-insensitive whole-needle
  match); the path side must exist as a FILE (a directory is skipped); every malformed
  entry is skipped fail-safe — never fatal, nothing ever spawned FROM a malformed
  entry. Local trusted configuration (host `env` block); allowlist and approval
  semantics for the launch itself are unchanged.
- **Launch-status honesty** — an authorized (`launch=server`) `NO_INSTANCE` is never
  silent: the payload carries exactly one launch status (`launched=<exe>` unchanged,
  `launch_rejected=LaunchTargetError` unchanged, new
  `launch_unresolved=path-lookup-missed` for a valid needle every resolution step
  missed, new `launch_unresolved=spawn-failed (<Exc>: <bounded msg>)` surfacing the
  real OS error). `launch=driver` payloads stay byte-identical and never spawn; the
  suffixes add no driver-controlled injection surface (static text / bounded local
  OS-error text only). The fake-backend launch mirror honors the same mapping and
  returns the same typed suffixes (real/fake parity pinned).
- **Capture provenance** — `VerificationResult.capture_provenance`
  (`CaptureProvenance`: sha256 + byte size of BOTH frames the visual tier already
  diffed, computed from bytes in hand — zero new captures, zero sleeps; additive
  evidence line `capture sha256 before=<12hex>… after=<12hex>… bytes=<n>/<n>`).
  None-preserving: every non-pixel verdict and legacy constructor is unchanged.
- **Capture-source integrity guard** — a per-session agent counter of CONSECUTIVE
  byte-identical pre/post capture pairs across DISTINCT screen-affecting actions
  (`wait`/`done`/ensure_app probes exempt; any differing pair resets; the
  expected-text fallback re-verification replaces its own contribution). At ≥ 3 the
  verification note+evidence and the audit event carry the typed marker
  `CAPTURE_SOURCE_SUSPECTED identical_pairs=<n>` (same uppercase vocabulary family as
  `TARGET_GONE`/`FOCUS_DRIFTED`/`NO_INSTANCE`). THE VERDICT IS NEVER ALTERED — a
  confident meaningless `0.000000` stream becomes a typed, actionable suspicion
  instead. Works in text-mode sessions (the internal capture is payload-independent).
- **Clipboard text entry — `via="clipboard"`** (additive `type`-only field; default
  `null`/`"sendinput"` is byte-identical to pre-`via` behavior): save the current
  clipboard text → set `CF_UNICODETEXT` (raw ctypes user32/kernel32, zero new
  dependencies) → ONE ctrl+v chord through the existing hotkey path → best-effort
  restore in `finally` (a restore failure is typed-annotated
  `clipboard_restore=failed`, never raised, dispatches no keystrokes; an abort
  anywhere after the set still restores; a set failure fails typed with zero
  keystrokes). For OpenGL/console apps that receive chords but drop per-character
  injection (field case: Blender's Python Console). `follow_ups` entries accept the
  same `via`.
- **`TYPE_UNCONFIRMED no-readable-target`** — typed diagnostic on a blind
  (`integrity=unverified`) type whose semantic reader could not even identify a
  readable focused target, with a transport-appropriate hint (verify visually with a
  stated `expected_effect`; on the default transport additionally: retry with
  `via="clipboard"`). A present-but-valueless control keeps the plain honest message.

### Changed

- `computer_execute` gains the trailing-optional `via` parameter (`None` == legacy
  behavior; wire pin updated — the tool's only parameter addition). `via` on any
  non-type action or an unknown value is a fail-closed typed `invalid_action`
  rejection (validator codes `via_not_allowed` / `via_invalid`; teach-in text covers
  both). Five tools stay five.
- Gate parity by construction and by test: the clipboard transport NEVER bypasses any
  gate — classification, secret redaction (`clipboard_credential` family included),
  and the approval decision are computed from the identical `text` for both `via`
  values (identical `SafetyDecision` rows pinned) and differ only after approval.
  R-04 read-back: the clipboard path uses the honest single-read-back form (no
  per-char repair ladder — a re-paste could double-apply); R-02 corpora untouched.
- Pre-existing payload pins updated for the additive suffixes only (design-mandated
  payload extensions): blind-type payloads in `test_io_parity`/`test_r04` and the
  RT-7 wire pin in `test_p5_redteam`.
- README (env table, action table, type-transports section) and docs/SAFETY.md
  (launch-mapping, launch-outcome-honesty, malformed-mapping rows) document which
  apps need which transport and the local-trusted-config status of the mapping.

### Fixed

- (Defect B) `ensure_app` `NO_INSTANCE` under `launch=server` for a Blender-like
  target now either really launches via the mapping (`launched=<basename>`) or tells
  the driver exactly why not and what to configure — the former silent bare payload
  is gone.
- (Defect A) a dead/frozen capture source is detected and typed
  (`CAPTURE_SOURCE_SUSPECTED`) with frame identity evidence (`capture_provenance`)
  instead of an endless confident `0.000000`.
- (Defect C) text entry into OpenGL/console apps works natively through the clipboard
  transport with clipboard-restore discipline; blind types say so honestly.

### Performance

- Provenance hashing: sha256 over the frames already in hand measured ~1.4 ms per
  ~1.9 MB frame; `blake2b(digest_size=16)` measured ~4.1 ms and missed the ≤~2 ms/frame
  budget, so sha256 ships. The detector performs ZERO new captures (observe-call
  parity pinned against a provenance-disabled run); the default `sendinput` path pays
  zero clipboard cost (the ctypes binding and helpers are reached only on the
  `via="clipboard"` branch).

### Compatibility notes

- No wire change: five tools stay five; every addition is an optional trailing
  parameter (`via`), an additive None-preserving model field (`capture_provenance`),
  or an additive payload suffix. `via=null`/`"sendinput"` and `launch=driver`
  payloads are byte-identical to v0.7.0. Approval semantics unchanged (TYPE stays
  approval-required-by-default; the clipboard transport runs the identical gate
  stack). No protection weakened: R-02 corpora byte-identical, failsafe/stop-token/
  `InputBlockedError` semantics ride the chord dispatch unchanged, launch
  soft-failure still degrades to a payload (never raises).

## v0.7.0 (2026-09-15, ORVEX-CORTEX-v07-007 — safety & guard residuals)

The three P0 safety and guard residual classes discovered during v0.6.0 red-teaming
are closed at the design level (shapes and grammars, not per-string patches), then
attacked for six adversarial rounds (661+ executed attack rows) until the re-verify
verdict was MISSION-GRADE STABLE — every residual that remains is fail-closed,
documented, and pinned by a test.

### Added

- **Matching-only normalization layer — `textnorm.py` (R-23)** — a canonicalization
  pass between action-text ingress and the safety gate, the classifier, and secret
  redaction: dual views `TRANSLATE(NFKC(x))` (437 codepoints / 30 ranges stripped —
  all Cf invisible characters, variation selectors, tatweel — plus whitespace fold
  to ASCII space), never-downgrade merge, boundary-faithful fusion for
  strip-character-joined keywords, and an ASCII fast path that is byte-identical to
  the pre-layer single pass. **Type integrity invariant: dispatched text is never
  rewritten** — normalization feeds matching decisions only. Obfuscated destructive
  payloads (zero-width, invisible separators, fullwidth) now classify at their plain
  form's severity (executed 550-row matrix: 225 full bypasses → 0 in the
  zero-width/space class).
- **Destructive-intent grammar (R-21)** — verb-anchored, filler-tolerant severity
  tiers over normalized components: unambiguous destructive verbs (`delete`,
  `wipe`, `purge`, …) block with any object in a K=3 window (function words do not
  consume slots), contextual verbs require a consequence-noun class hit, bounded
  morphology (s/es/ed/ing) for suffixed forms, mention-form copula guards
  (`delete is a word in the dictionary` stays clean), and a new
  `destructive_intent` classification category. Additive severity floors only —
  every pre-existing pattern, gate, and refusal path is preserved.
- **Secret redaction families (R-21)** — the assignment grammar now accepts spaced
  nouns (`api key:`), abbreviations (`pass`, `credentials`), and copula arms
  (`password is/was/are …`) with a prose-vs-secret token-shape rule and
  bridge-continued maximal secret-run consumption; value-shape families for
  untagged bearer tokens (`ghp_/gho_/ghu_/ghs_/ghr_`, `github_pat_`,
  `xox[abprs]-/_`, `xocr_`, `npm_`) join `AKIA` in one compiled choke point that
  feeds all eight sinks; non-executed response shapes (`rejected`, `safety_denied`,
  `approval_required`, `digest_surprise`, `error`) now redact their payloads too.
- **Regression corpora** — `tests/test_r21_classifier_redaction.py`,
  `tests/test_r22_launch_act_adoption.py`,
  `tests/test_r23_textnorm_normalization.py`,
  `tests/test_v07_redteam_repairs.py`: per-acceptance-criterion matrices with
  adversarial must-block rows, benign must-pass rows, false-positive guards, and
  plain-vs-obfuscated parity pins (218 net new tests; suite now 1594 passed +
  10 skipped).

### Changed

- **Launch-act / re-anchor arming policy (R-22)** — the session launch-act marker
  arms only on commit-key chords (enter/return/numpadenter) and only after the
  pre-dispatch gates pass (a rejected chord never arms); Path-B window adoption
  requires seed↔outcome correlation (tokens the session actually typed into the
  launcher must match the candidate's process/title; ≥3-character tokens). A
  foreign-process window reached by an unrelated keypress is refused as
  `REANCHOR_REFUSED` with the anchor kept. The R-20 launcher-act positive paths
  (enter flows), the one-action marker bound, and the dead-anchor
  (TARGET_GONE → unbind → reattach-only) machinery are unchanged and their suites
  stay green unmodified.

### Fixed

- **R-21** — polite/phrased destructive commands and indirect formulations no
  longer bypass the classifier; credentials written in varied formats
  (copula, spaced nouns, abbreviations, quoted) and untagged bearer tokens
  (`ghp_`, `xoxb-`, `npm_`, …) no longer reach any sink raw (65 executed
  near-miss vectors: 28 bypasses → 0).
- **R-22** — a foreign window following an unrelated keypress can no longer
  become the session anchor (4/4 attack scenarios refused post-fix), with the
  R-20 documented adoption paths proven intact.
- **R-23** — zero-width/whitespace/invisible-character obfuscation of destructive
  commands (and of secret labels/values) no longer evades classification or
  redaction; the residual Cyrillic-homoglyph class (not NFKC-addressable) is
  documented as a future TR39 hardening item.
- **Red-team repair rounds** — six adversarial rounds over the fixed layers
  closed every material finding (invisible-codepoint strip gap, intent-window
  filler overflow, copula-then-colon separator, inflected verbs, decoy-slot and
  bridge-word dodges, capitalized-prose false positives) with zero unexpected
  regressions; final verdict MISSION-GRADE STABLE.

### Performance

- End-to-end action latency statistically unchanged (paired deltas −3.6…+1.8 %
  against the 439a043 baseline; screen-capture counts identical at 9 per
  3-action flow); full suite 98.8–101.9 s vs 100.26 s baseline with 218 more
  tests. Per-function micro-benchmarks on the dual-view obfuscated path run
  ~×1.8 baseline (microseconds-scale, inherent to never-downgrade dual-view
  matching) — disclosed in the mission's `perf-release-verdict.md`.

### Compatibility notes

- No wire change: `tools/list` stays at five tools and every response shape is
  unchanged (redaction is now additionally applied to non-executed response
  payloads). `type` actions dispatch the original bytes — normalization is
  matching-only. `DEFAULT_TRANSIENT_LAUNCH_PROCESSES` is unchanged
  (`explorer.exe`). Plain keypresses and non-commit hotkeys no longer arm the
  launch-act marker — launcher flows that press enter are unaffected.

## v0.6.0 (2026-09-13, ORVEX-CORTEX-060 — quality, right-click, live D365 proof)

Quality and reliability across the verification, input, safety, and guard layers,
plus the owner-commissioned `right_click` action — validated live on the desktop
and against a Dynamics 365 F&O onebox (sales order, customer, invoice posting,
employee hire; `right_click` proven in-UI; maintainer-local evidence).

### Added

- **`right_click` action** — a smooth right-button click at screenshot coordinates
  that opens a real context menu, through the same fail-closed pipeline as every
  action: point-bearing grounding, validator bounds + `source_observation_id`
  binding, risk classification with the click family (LOW
  `known_application_interaction`; MEDIUM `unverified_target_application` when
  identity unknown), backend dispatch with the same stop-check/settle discipline
  (all three input engines resolve the right-button down/up flag pair; typed
  `unsupported mouse button` error for anything else), the `visual_change`
  verification default exactly like click, fake-backend parity, teaching text
  (`right_click (x,y; opens a context menu)` plus a context-menu hint in the
  invalid-action rejection), README/ARCHITECTURE/SAFETY documentation, 41 unit
  tests, and a default-skipped live E2E context-menu test — proven live on a real
  desktop twice: the Win32 `#32768` menu window owned by the launched Notepad
  appeared and closed on ESC, and the post-action screenshot shows the open edit
  context menu.
- **`CORTEX_CAPTURE=dxgi` — optional DXGI Desktop Duplication capture path (R-10,
  shipped early from P1)** — a pure-ctypes Desktop Duplication engine behind the
  existing capture-backend env switch: `blt` (default) keeps the GDI/BitBlt mss
  pipeline, `dxgi` switches the per-monitor pixel grab to duplication (~26–43 ms vs
  ~53–90 ms per 1920x1080 capture on the mission desktop, no new dependency). Any
  other value degrades to `blt`. Fail-open by contract: a duplication that cannot be
  created (RDP/protected session, a second live duplicator in the process) or fails
  mid-session (device lost, lock screen) permanently falls back to the mss path for
  the session — the fast path can only speed captures up, never fail one.
  Idle-desktop semantics: duplication yields a frame only when the compositor changed
  something; on timeout the last frame (still current) is returned, and a cold
  duplication's first frame is fetched with a one-shot retry.

### Changed

- **(Internals-removal wave) Removed the removed-loop's orphaned internals** —
  `recovery.py` / `approval.py` / `health.py` deleted; the agent decide-loop block
  (`run`, `_decide`/`_call_provider`, result builders, failure/dismiss handling), the
  provider `decide`/`decide_full`/`plan_subtasks`/`summarize_context` endpoints with
  their `_LazyProvider` delegates, the context summarizer plumbing, the subtask
  mutation APIs, and the `PlanValidator` classes are gone (`long_running.py` is now
  the per-session checkpoint owner; `plan_validator.py` keeps only the pure
  `find_cycle`/`contains_control_characters` helpers used by the live checkpoint
  path). **No wire change**: tools/list stays at the five tools, every response shape
  is unchanged; the `limits` session-budget fields remain accepted and validated
  (clamping semantics untouched) and are now documented as RESERVED — not enforced on
  the direct five-tool path since the internal loop's removal. The sealed
  checkpoint/resume machinery and the `SessionBudgetTracker` sealed-overshoot gate
  are unchanged and re-proven by the untouched checkpoint/resume suites.
- **(R-01) Deterministic verification decides `type` actions** — the root defect
  behind the pixel-tier false-negatives is fixed: the deterministic
  `ui_control_text` tier now receives the TYPED TEXT as its needle (it previously
  received the effect prose, which never appears in a control value, so the
  deterministic tier structurally abstained on every stated-effect type action and
  everything escalated to the pixel band). Typing now verifies deterministically
  from the field value/title whenever the text is visible; absent evidence still
  degrades to `uncertain` (never the historical false `failed`), and the pixel
  band acts only as the ambiguous-band escalation. Keypress/hotkey keep the
  launch-prefix `window_state` promotion (pinned), and focus-change identity
  signals stay click-scoped (pinned — an unrelated foreground steal must never
  verify a type effect).
- **(R-18) GroundedAction-invalid `follow_ups` items return typed rejections** —
  a target-less `focus_window`, half-specified `drag`, or 1-key `hotkey` no longer
  raises an uncaught `ValidationError`: the server boundary pre-validates every
  queue item and answers with the teaching `invalid_action` shape (valid-shape
  hints), fail-closed with zero dispatches; the agent-level queue guard adds the
  typed `invalid_follow_up` outcome for direct `run_single` callers. Queue
  stop/continue semantics are otherwise unchanged.
- **(R-02) Safety keyword gate tuned to word boundaries, both directions** —
  `_looks_sensitive` now tokenizes text instead of raw substring matching:
  hyphenated compounds stay single tokens (`closed-form` can never hide `rm`),
  identifier compounds split into components (`client_secret` still blocks),
  credential markers carry singular and plural forms, phrase markers (`cmd exe`,
  `reg delete`, `drop database`, `reset password`, …) match consecutive whole
  tokens, and `token(s)` is skipped only in the `doi`-citation context. The
  observed benign corpus (all five ROADMAP strings and the DOI variants) passes
  while the secret/credential corpus still blocks — pinned on both the safety and
  redaction layers; redaction patterns are unchanged.
- **(R-20) Re-anchor is causal** — a verified action's foreground window is
  adopted as the new anchor ONLY when it is a session-launched/attached surface, a
  same-process descendant (or an owner-chained dialog of one), or the outcome of
  the session's own keyboard launch act; everything else is refused with the new
  named `REANCHOR_REFUSED` audit event (annotation-only — the anchor is kept, so
  the next pre-dispatch rejects with `FOCUS_TAKEN_BY`). The old
  `anchor_gone → re-anchor to anything` rule is removed: a dead anchor unbinds
  via `TARGET_GONE` so the driver reattaches explicitly.

### Fixed

- **(R-04) Keystroke-burst resilience: truthful integrity reads + fast-path-first
  verification** — `type` is chunk-dispatched (default 64 characters,
  `CORTEX_TYPE_CHUNK_CHARS`) and verified by reading the focused control's value
  back. The first read path trusted `WM_GETTEXTLENGTH` via `SendMessageTimeoutW`,
  which mis-reports text length on some desktops (measured: it returned 1 for a
  20-character edit) and made the read-back under-report landed text, so a
  wrongful repair could double-apply. The read is now ONE fixed-buffer
  `WM_GETTEXT` call (8192-character cap, `SMTO_ABORTIFHUNG` kept), which reports
  truthfully and also un-truncates the `ui_elements[].value` observation field.
  Verification is fast-path-first: after a 0.05 s settle it polls up to 4 quick
  reads 0.04 s apart, accepting mid-drain buffer growth as evidence and returning
  `verified` the moment a read holds the full expected text; the 2.5 s
  landing-lag horizon plus the single suffix-diff repair are escalation-only
  (shrink/divergence/stalled-partial signatures). Blind reads, agreed-empty
  buffers, and a read budget exhausted while the buffer is still growing report
  honest `unverified` and never repair; a confirmed still-wrong buffer fails the
  action with the typed `TextIntegrityError`. The action message gains the
  additive suffix `integrity=verified|partial|unverified|mismatch(v/total)`
  (plus `healed=<n>`); `CORTEX_TYPE_INTEGRITY=0` restores the byte-identical
  legacy path. An intermediate design that paid the horizon on nearly every
  action measured ~2.9–3.4 s per type action (vs ~0.35–1.0 s on 0.5.9); the fast
  path resolves it — type-action client latency p50 718 ms (the pre-fix candidate
  measured ~3.2 s on the typing task: formal A/B client-latency p50 3196 ms),
  within ~150 ms of the 0.5.9 execution phase. Live proof on the reference desktop
  through the served pipeline at defaults: 200 characters across 20 bursts —
  20/20 executed, all `verified(10/10)`, 200/200 characters exact, zero
  duplication, zero manual fixes — and, on that same intermediate state (the
  fast path does not change chain semantics), a 50-action `follow_ups` chain —
  50/50 executed, 855/855 characters exact.
- **(R-05) B5 backslash-type wedge closed as root-caused** — the historical
  >10-minute stall was a one-off window-activation race: the type dispatched while
  the freshly activated Run dialog's thread input queue was still settling. On the
  re-run the exact incident payload completes in <0.35 s across engine,
  Run-dialog, and full-server topologies; the shipped settle/gap mitigations and
  watchdog regression tests hold. The accepted residual (a one-off OS input-stack
  wedge cannot be made impossible in-process; it is bounded by the watchdog and
  surfaces as a typed failure, never a hang) is documented in `docs/SAFETY.md`
  (Known residual risks).
- **(R-03) `ensure_app` no longer builds an empty-title focus call** — the
  reattach path prefers the top-of-Z-order TITLED match among resolved instances
  and raises the typed `WindowFocusError` when every matched instance has an empty
  title, instead of surfacing a raw validation error or silently reattaching
  unfocused (identical in the real and fake backends).
- **(R-19) FocusGuard verifies identity beyond the hwnd** — the equal-hwnd
  pre-dispatch branch now cross-checks pid → process name → window class → title
  overlap (the strongest identity evidence both sides provide decides); a recycled
  hwnd owned by a foreign process fails the check, falls through to the
  pid-verified rules, and rejects with `FOCUS_TAKEN_BY`.
- **`cortex-mcp probe` no longer intermittently fails against a healthy server** —
  the probe closed the child's stdin immediately after writing its three requests,
  and the server's EOF handling could race-cancel the in-flight tools/list
  response (pre-existing since v0.5.9; A/B loops showed 2/8 failures at the v0.5.9
  baseline and at the integration HEAD alike). The probe now reads the responses
  with stdin held open, like a real host; verdict strings, exit codes, argv/cwd
  forwarding, and the exact-tool-set plus schema-token checks are unchanged
  (10/10 stable probe runs observed post-fix).

### Performance

- **Measured v0.5.9-vs-0.6.0 comparison (scripted deterministic driver, 5
  composite desktop tasks, 3 interleaved repeats per task per version, driver
  think time excluded): server-side mechanics at rough parity.** Window-focus
  switching is ~2.8× faster server-side (S3 server-pipeline p50 46.3→16.4 ms);
  typing actions pay ~+100 ms for the NEW inline integrity verification (S1
  client-latency p50 592→690 ms); driver-context bytes per action are at parity
  (0.99–1.06×). Verdict-driven turn waste is at parity too (driver-faithful
  retry-loop scenario: 0 policy reactions on either version). Stated plainly:
  **the measured data does not support a "much faster than 0.5.9" claim — 0.5.x
  already contained the large speed wins.** 0.6.0's delivery is reliability and
  capability at equal speed. (Raw per-run artifacts and the medians table live in
  the maintainer-local `evidence/v06-006/speed/` tree.)

### Compatibility notes

- **Behavior change worth callers' review:** verification tier defaults changed
  for `type` (a deterministic field-match now decides where the text is visible;
  the pixel band escalates only) and `keypress` (launch-prefix `window_state`
  promotion, pinned).

- The `right_click` action is additive: existing action names, parameters, and
  response shapes are unchanged.
- `type` responses gain the additive `integrity=...` suffix, and a new typed
  failure class (`TextIntegrityError`, a `BackendError`) exists for confirmed
  unhealable drops — drivers should re-observe and decide, never retry blindly.
- Stated-effect `type` actions can now return `verified` via the deterministic
  field match where they previously escalated to the pixel band; absent evidence
  remains `uncertain` (no new failures, no false successes).
- Malformed `follow_ups` items return the typed `invalid_action` rejection with
  valid-shape hints instead of an unstructured error; nothing dispatches in that
  case, exactly as before.
- New guard audit event `REANCHOR_REFUSED` (annotation-only, never a queue stop
  reason); `FOCUS_TAKEN_BY` semantics unchanged.
- The safety keyword-gate tuning is precision-only: derived words and DOI
  citations pass, and every value-bearing secret usage still blocks (both
  directions pinned by tests).

## v0.5.9 (2026-09-12) — RELEASED (canonical install prompts + probe subcommand)

The served-surface verification becomes a first-class command, and installing
Cortex becomes a copy-paste operation for any MCP-capable agent.

### Added

- **`cortex-mcp probe` subcommand** — one-command stdio verification of the served
  surface: it starts the server over stdio, performs the zero-input handshake, and
  passes only when the served surface is exactly the five tools with no `anyOf`/`$ref`
  schema tokens, printing a single `PROBE PASS` line and exiting 0; any other shape
  prints a `PROBE FAIL` line and exits non-zero, so shells, scripts, and agent
  install prompts can gate on the exit code alone.
- **"Installation (agent prompts)" documentation** — two self-contained copy-paste
  prompts (Install / Update) that let any MCP-capable agent install, register itself,
  and verify Cortex in a single canonical location: `%LOCALAPPDATA%\Cortex` on
  Windows, `~/.local/share/cortex` on every other platform (repository clone and its
  own `.venv` live together there, so every agent registers the same command path).
  The prompts preserve the standing guarantees: an existing cortex registration is
  never clobbered, every config write is backed up first, and updates are
  fast-forward-only merges that never reset.

## v0.5.8 (2026-09-12) — RELEASED (global `cortex-mcp` CLI: install/update subcommands with no-clobber agent registration)

A single terminal command now performs the whole install/update lifecycle that
previously required hand-editing each agent's MCP configuration file.

### Added

- **Global `cortex-mcp` terminal command** — a `[project.scripts]` console entry
  (also runnable as `python -m computer_use_mcp.cli`; stdlib-only CLI) with two
  subcommands:
  - **`install`** — provisions `<repo>/.venv` and the editable install, then registers
    the Cortex MCP server into every recognized agent config file found on the machine
    (zcode, claude, cursor, codex, kimi). Every file is backed up before the first write
    (`<file>.cortex-backup-<timestamp>`; an existing backup is never overwritten). An
    existing cortex entry is never clobbered: entries that already match the target are
    left untouched (UNCHANGED); entries that differ are reported and skipped
    (SKIPPED-EXISTS-USE-FORCE) unless `--force` is given. `--dry-run` prints the full
    plan with zero writes; `--agents a,b` (or `all`) filters the targets. The served
    surface is verified with a zero-input stdio probe that expects exactly the five
    tools and no `anyOf`/`$ref` schema tokens.
  - **`update`** — `git fetch` plus a `--ff-only` merge of `origin/main` (clean abort
    when the branch is not fast-forwardable — the command never resets), then an
    editable refresh of both installs, package-version verification before/after, and
    the same stdio probe.

### Compatibility notes

- Existing cortex registrations are never modified without `--force` — manual setups
  are preserved (files whose cortex entry differs are not rewritten at all).
- When a write does happen, the config file is rewritten with standard JSON
  indentation and the original bytes are backed up first; every unrelated key is
  preserved.
- The global console-script refresh targets the system interpreter and is best-effort:
  without an elevated terminal it prints a warning (the fallback
  `python -m computer_use_mcp.cli` always works) and never changes the exit code.

## v0.5.7 (2026-09-11) — RELEASED (end-to-end speed: window-identity queue batching, half-res action JPEGs, honest alias launches and verdicts)

End-to-end latency analysis showed the driving model's turns dominate wall
time; this release removes the server-side multipliers: per-action image bytes
that grew the driver's context, batches halted by ordinary pixel change, and
turns burned decoding false failures.

### Changed

- **`follow_ups` batches continue while the attached window identity is unchanged
** —
  ordinary pixel changes from earlier queue items (drawing, typing,
  dialogs) no longer stop a batch; each item re-grounds from the fresh post-action
  capture. The queue still stops on TRUE staleness — the attached window's identity
  (hwnd/title/bounds) changed or the window closed since the queued premise — plus
  safety rejection, approval requirement, validator/grounding rejection, dispatch
  error, or a named interference event. `CORTEX_QUEUE_STRICT_DIGEST=1` restores the
  v0.5.6 whole-screen premise checks.
- **Executed-response images default to half-resolution JPEG** — the DEFAULT
  `computer_execute` response image is a 0.5x JPEG (q60, ~60KB typical at 1080p) so a
  driving model's context stops growing by a 150-230KB PNG per action. An explicit
  `include_screenshot_after=true` ships full resolution, and
  `CORTEX_ACTION_IMAGE_FULL=1` restores full-res defaults. `computer_observe` /
  `computer_screenshot` and text-mode (`image_delivery="text"`) semantics are
  unchanged.
- **Docs re-aligned** — README / ARCHITECTURE / SAFETY now teach the queue-continuation
  and half-res-image defaults (the pre-0.5.7 text described the old whole-screen
  digest stop and ~1-2 MB PNG default); an `ensure_app` docstring said `os.startfile`
  while the code uses `Popen` — fixed.

### Fixed

- **`ensure_app` bare-name resolution and honest `launched=`** — bare app
  names now resolve to `.exe` / Windows Store execution aliases before spawning, and
  the `NO_INSTANCE` payload only reports `launched=` when the process actually
  started (a failed spawn is reported as a failure, never as a launch).
- **Honest `uncertain` for evidence-free stated effects** — a
  stated-effect action with no deterministic verification signal and zero/sub-threshold
  pixel evidence now returns `uncertain` (0.4) instead of a definitive false `failed`
  ("Expected change was not observed"); the input dispatched and the driver can
  re-observe instead of retrying blindly.

### Performance

- Context bytes per action drop from a ~150-230KB PNG to a ~60KB half-res JPEG by
  default; window-identity queue batching lets one model turn cover up to 5 actions
  without false stops — directly attacking the ~97%-of-wall-time model-turn cost.

### Compatibility notes

- **Default behavior changes:** executed-response images are half-res JPEG unless
  `include_screenshot_after=true` or `CORTEX_ACTION_IMAGE_FULL=1`; queued batches
  continue through ordinary pixel change and stop on true window-identity staleness
  unless `CORTEX_QUEUE_STRICT_DIGEST=1` restores whole-screen premise checks.
  Safety semantics unchanged: zero-bypass per-item pipeline, safety rejections and
  named interference events still stop the batch, stop token still checked between
  items.

## v0.5.6 (2026-09-11) — RELEASED (truthful verification & resilient follow_ups batching)

Focus-type clicks with a stated expectation could false-fail when the intended
transition was invisible to the pixel diff; that false verdict flushed `follow_ups`
batches; validation rejections carried a generic stamp. All three classes are fixed
here. This release also ships the v0.5.5 hardening work below (desktop-safe E2E
gating, run_goal removal, 3x mechanical speed).

### Fixed

- **False "failed" verification on focus-type clicks** — a focus-type click
  whose stated `expected_effect` describes a focus/dialog transition is often
  invisible to the pixel diff; a sub-threshold visual diff now degrades to an honest
  `uncertain` instead of a definitive false `failed` (legacy semantics preserved for
  unflagged intents; uncertain is never success, and a real above-threshold change
  still verifies).
- **`follow_ups` batches flushed by a false failure** — a queued batch no longer
  stops on an executed item's verification `failed`: the input dispatched, the next
  item re-grounds from the fresh post-action capture, and per-item truthful verdicts
  (ok=False + evidence) are still returned; `CORTEX_QUEUE_STRICT_VERIFY=1` restores
  the v0.5.5 stop-on-failed behavior.
- **Generic rejection stamps** — validation rejections name the
  actual gate (allowlist / staleness / focus) instead of a generic
  "Grounding rejected." stamp, so weak drivers stop burning turns decoding `reasons`.
- **Desktop-safety gating of the real-input E2E suite** — plain
  `pytest tests` runs used to execute the real-Windows E2E tests whenever
  `CUMCP_RUN_E2E` leaked into the environment (for example when the variable leaks into the
  environment from another tooling process). The gate is now a loud, fail-closed contract pinned by
  tests: e2e-marked tests SKIP BY DEFAULT with a skip reason that states the exact
  opt-in (`CUMCP_RUN_E2E=1 python -m pytest tests/e2e`) and WARNs that opting in
  drives real keyboard/mouse input on the live desktop; only the exact variable name
  AND exact value `1` enable it (`true`/`yes`/`on`/`2` and any
  `E2E_*`/`REAL*`/`DESKTOP*` variable stay closed — proven against leakage).

### Changed

- **Served tool docs re-taught** — the `computer_execute` description, module
  docstrings, and SAFETY/ARCHITECTURE behavior text now teach the new queue semantics
  (uncertain AND executed-failed verdicts continue the batch), the gate-naming
  rejection messages, and the `CORTEX_QUEUE_STRICT_VERIFY` escape hatch.
- **Unique-window isolation in the E2E suite** — e2e app
  tests can no longer attach to anything they did not launch themselves. Notepad
  instances are launched with scratch files whose names embed a run-unique token
  (`cumcp-e2e-<pid>-<n>-<ts>`, so the window title carries it) and attach goes through
  a marker-only helper (`attach_window_by_unique_title`) that treats the user's own
  same-app windows as invisible: zero marker matches fail loudly (TimeoutError, never
  a fallback attach), ambiguous matches raise. Calculator attach is PID-scoped to the
  process the suite launched; the browser page title embeds the same run-unique token.
  Teardown is hardened to close exactly what was opened (exact-hwnd `close_window`
  instead of a class/title re-search; `kill_process_tree` stays pid-scoped). All
  pinned without real input in `tests/test_r8_pins.py` (gate fail-closed
  matrix, marker-only attach with stubbed enumeration, exact-hwnd teardown,
  marker-presence checks).

### Performance

- **Mechanical per-action speed** — the direct-action pipeline now
  runs one full capture instead of three on the steady-state path, and verification
  diffs raw frames instead of re-decoding the PNG it just watched get encoded.
  Per-action profiling showed each direct
  action paid THREE full captures (direct_request + validate + post_action,
  p50 132–174 ms each) plus a two-PNG-decode pixel diff (p50 101 ms); the honest
  live baseline was ~428 ms mechanical per action (excluding the host's own observe
  calls), ~582 ms including them — not the 125 ms figure previously reported.
  Changes, all verification-semantics-preserving:
  - **Identity-probe-guarded validate reuse** — a DIRECT action whose premise was
    captured in the same tool call and is still inside the freshness window
    (`CORTEX_VALIDATE_REUSE_MS`, default 1500; `0` restores full re-capture) runs a
    capture-free identity probe (monitors + foreground window + coordinate space —
    exactly the dimensions the validator's staleness check consumes; ~0.8 ms measured)
    instead of re-capturing the identical screen. NO drift → the premise is reused
    (audited truthfully: `phase=validate, reused=True, duration=0.0`); ANY drift →
    the probe becomes the validate observation and the P0-H `STALE_OBSERVATION`
    rejection + single re-observe recovery fire exactly as before. Queued
    follow-ups NEVER reuse (their premise is a previous item's post-action capture);
    backends without the probe keep the full validate capture.
  - **Raw-frame verification fast path** — the backend stashes the capture-time RGB
    frame on the Observation (private `_frame`, never serialized); the pixel-diff
    tier and the FocusChange digest corroboration use it directly instead of
    base64+PNG-decoding both sides (two ~20–36 ms decodes per verification → 0).
    No stash / one-sided stash / size-mismatched stash falls back to the exact
    legacy decode; verdicts and evidence are byte-identical (pinned).
  - **One-histogram diff math** — the screen-wide mean is derived from the diff's
    own 768-bin histogram instead of a second full-frame `ImageStat` pass; bit-
    identical values (pinned against the historical computation), thresholds
    (`STRONG_PIXEL_DELTA=40`, `STRONG_CHANGE_MIN_PIXELS=50`,
    `DEFAULT_DIFF_THRESHOLD=1.0`) untouched.
  - **Outbound JPEG from the raw frame** — `_bound_outbound_image` accepts the
    capture frame and skips re-decoding the just-encoded PNG; ladder outputs are
    byte-identical (pinned), internal PNG untouched.
  - **Persistent mss capture instance** — one held instance per backend (DIB stays
    allocated) instead of a fresh `mss.MSS()` per grab; any grab failure drops the
    instance fail-safe. `CORTEX_INTERNAL_FRAME_REUSE=0` disables the frame/payload
    stashes (pure legacy path).
  - **Identical-pixel payload dedupe** — a capture whose raw bytes are byte-identical
    to the previous capture reuses the previous PNG payload verbatim (PNG
    determinism: identical pixels → identical bytes; digests/staleness unaffected).
  - **`CORTEX_PNG_COMPRESS_LEVEL`** (0–9, default unset = PIL's own = today's bytes)
    — opt-in encode/size trade for mechanical throughput.
  Measured on a real desktop (real capture, input stubbed): per-action p50
  198.5 ms paced (was ~428 ms live), verification p50 101→33.7 ms, observe p50
  ~70–98 ms, identity probe 0.8 ms. The remaining floor is two GDI BitBlt
  captures (~40 ms each, hardware-bound) + two lossless PNG encodes (~27–80 ms on
  noisy content) + one full-frame diff (~30 ms) — reaching the 52 ms bar would
  require removing a mandatory capture or the lossless internal format, both
  verification-semantics changes this wave is forbidden to make. Back-to-back
  host calls still wait behind `min_screenshot_interval_ms=250` (contractual).
 

### Changed

- **The `run_goal` tool family is REMOVED** — `run_goal`, `create_subtask`,
  `list_subtasks`, `run_subtask`, and `get_session_progress` no longer exist; the
  server now exposes exactly five deterministic tools (`start_session`,
  `computer_observe`, `computer_screenshot`, `computer_execute`, `stop_session`).
  The internal autonomous loop (an internal LLM/vision decide phase deciding one
  action per model call) was removed because the server is driven exclusively by a
  host agent making direct tool calls with a real model — the loop doubled the
  failure surface and was the sole home of two confirmed defects: an unset
  `VISION_API_KEY` surfacing as an in-loop HTTP 401 instead of a clean typed error,
  and an unbounded ~500 KB screenshot payload serialized as text inside loop
  results. Both defect classes are nullified by the removal. Every direct
  `computer_execute` call passes through the identical grounding, validation,
  risk, approval, execution, and verification pipeline, so the per-action
  guarantees are unchanged; loop-only semantics (per-call approval budget,
  in-run bounded recovery/re-decide, `completion_evidence="model_declared"`
  labeling) are replaced by per-call `approved=True`, typed failure outcomes
  surfaced to the host, and host-decided completion on evidenced `verified`
  outcomes. The sealed checkpoint/resume machinery survives on
  `start_session(resume_from_checkpoint=…)`; the subtask-orchestration modules
  and their domain rules remain pinned by unit tests but are no longer reachable
  from any tool. Tests: loop-only suites removed, shared-behavior suites
  retargeted to the five-tool surface (not weakened); E2E and benchmark runner
  retargeted to direct calls.

**Compatibility notes:** BREAKING for any host that still called `run_goal`,
`create_subtask`, `list_subtasks`, `run_subtask`, or `get_session_progress` —
those calls now fail with "Unknown tool"; hosts must drive actions through
`computer_observe`/`computer_execute` (the documented direct-control pattern).
No surviving tool changed name, parameters, or response shape; the wire-schema
wins from v0.5.5 (content-block executes, flat type-array optionals, no
`outputSchema` on the block tools) are intact.

- **Docstring re-teaching for misclassification-prone models (L1-NEW-1):** live
  evidence showed the default non-vision model READ the `image_delivery` teaching,
  understood it, then self-misclassified ("my host is multimodal") and died on the
  default image mode anyway. The `start_session` tool description (and
  `computer_observe`'s cross-pointer) now teach the model to judge by what it
  RECEIVES (text-only inputs → `image_delivery="text"`, required; unsure → "text";
  text mode never crashes any model — a vision model only loses screenshot pixels)
  instead of asking it to classify itself; README/ARCHITECTURE document
  `CORTEX_IMAGE_DELIVERY=text` in the MCP server `env` block as the deterministic
  fallback for hosts whose default model cannot view images. Wording only — the
  D1 mechanism, defaults, and precedence are unchanged.

**Compatibility notes:** default `follow_ups` queue semantics changed — an
EXECUTED item whose verification outcome is `failed` no longer stops the batch (the
honest failed verdict still rides its per-item `follow_up_results` entry); set
`CORTEX_QUEUE_STRICT_VERIFY=1` to restore the strict stop-on-failed behavior.
Rejection `message`s now name the actual gate instead of the literal
"Grounding rejected." — drivers matching that exact string must match the new
gate-naming messages (`Action rejected by grounding/staleness check/validation/focus
allowlist: …`); the structured `reasons` payloads are unchanged. Unflagged
visual-change intents (drag, scroll, type, unstated expectations) keep their exact
pre-REM-B verification semantics; a flagged focus-type click can now answer
`uncertain` where it previously false-answered `failed` (never a new success).

## v0.5.5 (2026-09-09) — RELEASED (three live-usage defect classes + live-host hardening)

Mission ORVEX-CORTEX-055: root-caused from FailedLog.txt (967-line live session),
fixed, red-teamed, and validated live on Kimi Code + GLM-5V driving MS Paint.

### Fixed

- **Computer-use slowness drivers** — false "Expected change was not observed" verdicts
  for focus-type clicks (a new deterministic `FocusChangeStrategy` verifies via UIA
  focused-element / active-window identity / digest corroboration before the pixel tier);
  `follow_ups` queues no longer abort on UNCERTAIN verdicts (stop only on definitive
  failure), so batches actually batch; provider-judge image re-encoding eliminated
  (base64 reused verbatim).
- **Failed full takeover** — `ensure_app` now LAUNCHES allowlisted targets server-side by
  default (`attach_or_launch.launch` default `driver`→`server`; `CORTEX_ATTACH_OR_LAUNCH`
  env knob restores the old never-launch default), resolves Windows Store execution
  aliases (`%LOCALAPPDATA%\Microsoft\WindowsApps\<needle>`) so Store apps like Paint
  actually spawn, and an allowlisted `ensure_app` target is exempt from the
  foreground-process gate that previously made bootstrap impossible from a fresh session;
  the `process_not_allowed` rejection now names the active process and teaches the remedy.
  Launch hardening: typed `LaunchTargetError` charset validation before any spawn,
  `shell=True` removed (list argv via `shutil.which` → alias → raw name).
- **Screenshots truncated / delivered as base64 text** — `computer_execute` now returns
  MCP content blocks in parity with `computer_observe` (slim TextContent + real
  ImageContent); `include_screenshot_after=false` honored on the follow_ups path too;
  follow-up entries are slim (double serialization eliminated); outbound images on BOTH
  observe and execute bounded by `CORTEX_RESULT_IMAGE_MAX_KB` (default 180 KB, JPEG q85 +
  downscale ladder, internal PNG pipeline untouched); observe metadata `ocr_text` /
  `ui_elements` capped at 20 entries with `<field>_omitted_count`.
- **Weak-model tool-call tolerance (live GLM-5V on Kimi Code)** — integral floats, numeric
  strings, and fractional coordinates accepted for x/y/x2/y2 (rounded, clean typed errors
  otherwise); `keys` accepts a bare string or JSON-encoded array; `follow_ups` accepts a
  single dict / JSON string; `allowed_processes`/`allowed_windows` accept comma- or
  space-separated strings; **all Optional parameters advertise plain type-array JSON
  schemas (`{"type":["integer","null"]}`) instead of `anyOf` unions**, which a live vision
  host rejected wholesale; `computer_execute`'s FastMCP return annotation relaxed to `Any`
  (the `dict[str, object]` annotation crashed block-list responses with a Pydantic
  DictModel error through real hosts).

### Performance

- Measured live (Kimi Code + GLM-5V driving Paint, session metrics): observation p50
  120–164 ms, execution p50 11.6–119 ms, verification p50 94–110 ms per action;
  ensure_app + observe + click round-trips 0.14–1.4 s per tool call. Loop mechanics are no
  longer the bottleneck; model inference time dominates end-to-end step latency.

### Compatibility notes

- **Default behavior change:** a session with an allowlist can now server-LAUNCH an
  allowlisted `ensure_app` target (`launch="driver"` or `CORTEX_ATTACH_OR_LAUNCH=driver`
  restores never-launch). Non-allowlisted targets never launch — fail-closed unchanged.
- **Tool output shape:** executed `computer_execute` calls return `[TextContent,
  ImageContent]` blocks (was: dict with `screenshot_after_base64`); error/rejection shapes
  stay plain dicts. The image rides as a real image block under the 180 KB outbound budget.
- **Wire schemas:** Optional parameters advertise type-array forms instead of `anyOf`
  (runtime validation semantics unchanged; `limits` still fail-closed strict).

## v0.5.0 (2026-09-07) — RELEASED (performance & effectiveness)

Validated by the project's internal QA process: full suite green, contracts
backward-compatible.

### Performance

- **SendInput input engine** — raw Win32 `SendInput` via stdlib ctypes is now the default physical-input path (PyAutoGUI becomes the selectable fallback; `CORTEX_INPUT_BACKEND=sendinput|pyautogui`), with fail-closed injection-error detection.
- **PNG encoding `optimize=False` by default** (−201 ms/frame measured; `CORTEX_PNG_OPTIMIZE=1` restores the old behavior).
- **Observe reuse** — the post-action capture becomes the next step's loop-top observation: 3 full captures per action step instead of 4 (measured before/after).
- **Rate-gate burst exemption** — `min_screenshot_interval_ms` (default 250) now protects only FRESH observations (loop-top capture, host-driven observes); intra-step verification captures (staleness probe, post-action capture) are burst-exempt but still recorded, so session-wide capture volume stays enforced and bounded.
- **Verification ladder** — for model-judge intents: deterministic window/process/text/predicate strategies first, the pixel-diff supporting check second, and the provider model judge only when both cheap tiers are inconclusive; a deterministic verdict always skips the judge.
- **`follow_ups` batching** — `computer_execute` accepts up to 5 queued action specs; each passes the full independent pipeline; the queue stops at the first verification failure, safety rejection, approval requirement, or post-action digest surprise, with per-item results in the additive `follow_up_results` field.

### Added

- **`include_screenshot_after`** opt-out on `computer_execute` — omit the heavy `screenshot_after_base64` payload (~1–2 MB) when the verification verdict is what matters.
- **`text_summary`** — additive one-line summary on `computer_observe`/`computer_screenshot` text blocks (active window title/process, cursor position, focused-control hint when UIA data exists, changed/unchanged note versus the previous observation).
- **UIA semantic fields** — optional `ocr_text` / `ui_elements` population from UIA snapshots (focused element first) where the platform supplies them; omitted gracefully when absent.

### Changed

- **`start_session` default flip: `dry_run` now defaults to `False`** (live session). The old `dry_run=True` default silently produced no-op sessions that cost a full agent turn to discover; pass `dry_run=True` explicitly for validation-only sessions (results carry the unmistakable `DRY-RUN (no input dispatched):` banner). `require_approval` still defaults to `True`.

### Fixed

- **`find_window_by_title` implemented** — EnumWindows enumeration with root-owner resolution (`GetAncestor(GA_ROOT)`) and case-insensitive matching with exact > prefix > substring precedence; window-title resolution no longer falls through to "unavailable" on the platform backend.

**Compatibility notes:** the `dry_run` default flip is the one behavioral change callers must review (loops that relied on the accidental no-op default must pass `dry_run=True` explicitly). Everything else is additive or strictly faster: tool names, parameter positions, and response shapes are unchanged, and no limit was removed or weakened.

## v0.4.1 (2026-09-06, hotfix — vision fix)

### Fixed

- `computer_observe` / `computer_screenshot` now return the screenshot as a real MCP `ImageContent` block (alongside the metadata text block) so vision-capable client models receive an actual image; the raw base64 never travels as text.

**Compatibility notes:** bug fix only; tool signatures and the metadata payload are unchanged.

## v0.4.0 (2026-09-06) — Long-Running Sessions

### Added

- Subtask decomposition with deterministic, fail-closed plan validation (duplicate ids, unknown/self/cyclic dependencies, oversize plans, invalid statuses, unsafe content reject the whole plan); hard ceiling of 50 subtasks per session.
- Dependency-ordered sequential execution; a failed subtask moves its transitive dependents to `blocked` automatically; bounded replan (at most 3 attempts per session) with replacement entries that never depend on dead ids.
- Atomic, sealed, versioned checkpoints — schema-version-gated loading, HMAC-SHA256 integrity seal keyed by a per-installation key file, 8 MB cap, redaction-enforced (secret-like values refuse the write) — written every 50 steps / 30 minutes and at lifecycle triggers; `start_session(resume_from_checkpoint=…)` resumes as a continuation: counters restored, never reset; the current environment re-verified; approval always fresh.
- Approval epochs (default 30 min / 50 interactive actions) and shared session budgets (`max_session_seconds`, `max_session_actions`, `max_session_model_calls`, `max_session_steps`, `max_subtasks`, `context_summarize_every`, … — validated and clamped fail-closed like the nine per-task limits).
- Four new MCP tools: `create_subtask` (works without any planner/LLM key), `list_subtasks`, `run_subtask`, and `get_session_progress` (deterministic progress percentage computed from subtask-manager state, never invented).

**Compatibility notes:** strictly additive. The six existing tools keep their names, parameter positions, and response shapes; `run_goal(..., auto_subtasks=True)` and `start_session(..., resume_from_checkpoint=…)` are trailing optionals — omitting them keeps byte-identical single-goal default behavior.

## v0.3.0 (2026-09-06 — move / hotkey / focus_window actions)

### Added

- `move` (hover without click), `hotkey` (compound 2–12-key shortcut chords), and `focus_window` (foreground a named window) actions with full pipeline support — grounding, validation, risk classification, execution, and deterministic verification defaults (`move` → cursor-position predicate, ±2 px; `focus_window` → window-state check against the requested title, never pixels; `hotkey` → `keypress` semantics with `window_state` promotion for open/launch/switch effects).

### Fixed

- `focus_window` targets are resolved and checked against the `allowed_windows` title allowlist (and the process allowlist) before any foregrounding call — all four rejection paths fail closed.

**Compatibility notes:** additive action vocabulary only; `focus_window` is risk-classified at least MEDIUM (category `window_focus_change`). No existing action, parameter, or response shape changed.

## v0.2.0 (2026-09-06) — initial production build / open-source release

First published release (developed internally as 0.1.0; the version was aligned
0.1.0 → 0.2.0 before the open-source release commit).

### Added

- The closed-loop executor: OBSERVATION → GROUNDING → VALIDATION → RISK/AUTHORIZATION → EXECUTION → RE-OBSERVE → SEMANTIC VERIFICATION, with bounded recovery/replanning (12-class failure taxonomy, 2 attempts per action, 6 per task, no blind coordinate retries) and an explicit `termination_reason` on every exit.
- Six core MCP tools: `start_session`, `computer_screenshot`, `computer_observe`, `computer_execute`, `run_goal`, `stop_session`.
- Safety and limits: contextual risk engine (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`), approval gates and `CRITICAL` blocking, nine hard limits with fail-closed enforcement, thread-safe stop token checked before every physical input, Win32 window/process identity, per-monitor DPI with measured coordinate-space classification (unverifiable spaces refuse coordinate input), and the single screenshot-to-input scale transform invariant.
- Secret redaction (10 detection patterns) enforced on the audit sink, provider payloads, and tool responses; five-channel prompt-injection doctrine; redacted JSONL audit trail plus metrics (19 counters, 5 latency summaries).
- `drag` action (press-move-release with stop-checked segments, both endpoints grounded and bounds-checked); compact-change pixel verification (strong-pixel detection for thin strokes and small controls).
- Real-Windows E2E suite (gated behind `CUMCP_RUN_E2E=1`) and OSWorld-2.0-aligned benchmark scaffolding (`benchmarks/` — harness only, no published scores).

**Compatibility notes:** initial release — no prior version to be compatible with. Windows-only execution backend (PyAutoGUI + Win32); Python ≥ 3.11; OCR and UIA are deliberate extension-point stubs that refuse fail-closed.
