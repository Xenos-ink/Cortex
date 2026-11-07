# HISTORICAL — describes v0.7.0 (the R-21/R-22/R-23 red-team cycle), not current

The residual CLASSES this cycle produced are still the current accepted trades
and are kept in docs/SAFETY.md §10. This file is the per-release red-team LOG:
the cycle narrative, the executed attack rows, and the repair rounds. Nothing
here describes 0.7.6 behavior changes; the fixes themselves landed in v0.7.0
and are described as current truth in docs/SAFETY.md (§3.1 normalization,
§3.2 destructive-intent grammar, §7.1–§7.3 redaction, §11.11 re-anchor policy).

## The cycle

After the R-21/R-22/R-23 fixes landed (commit range `a36033d..9069ac2`), an
independent adversarial review re-attacked all three fixed layers
(classifier/redaction, normalization, re-anchor state machine) in the same
classes the v0.6.0 red team had revealed, followed by four differential
re-verification rounds, each round re-attacking the previous round's repairs.
Every attack row was an executed in-process probe against the real functions.
**661+ attack-row instances were executed across the rounds** (initial round:
261 new rows + the 65-row historical vector corpus re-run; re-verification
rounds: 241, 181, and 199 rows including 35 novel attack-surface probes, plus
an aggregate 377-row prior-round sweep). Full per-row results, repro drivers,
and the round reports are archived in the maintainer-local
`evidence/v07-007/redteam/` tree; they are not part of this repository. Every
finding was either fixed-and-retested in-mission or dispositioned into the
residual classes. No open HIGH/CRITICAL finding remained.

## Initial round findings (RT-D1-01…08)

Four bypass-class findings against the fresh fixes:

- RT-D1-01 — intent-window overflow (3+ function words defeated the object
  window).
- RT-D1-02 (HIGH) — nine invisible non-Cf codepoints outside the strip set
  flipped blocked payloads to allowed/LOW on both layers.
- RT-D1-03 — copula-then-colon (`password is: X`) defeated the assignment
  grammar.
- RT-D1-04 — inflected destructive verbs stayed unflagged.

Three false-positive findings: RT-D1-05 negation ignored, RT-D1-06 V1
mention-prose blocked wider than pinned, RT-D1-07 copula arm redacted benign
prose (checkpoint availability side-effect). RT-D1-08 graded the new-family
residuals (basic-auth header MEDIUM; stripe/OpenAI/Google/Slack-webhook INFO)
and confirmed the R-22 joints held (no bypass in the fixed path; seedless
commit-key residual boundary extended). Layer-3 perf probing found nothing
(no ReDoS; worst adversarial 2000-char payload 8.5 ms).

## Repair rounds R1–R5

Commit chain `a36033d` → `fa03828` → `e59f1ea` → `c4624da` → `e88cec4` →
`d433a5b` → `9069ac2`.

- **R1 (`fa03828`)** — strip set +9 invisible non-Cf splitters (S1);
  intent-window filler no longer consumes slots (S2); copula-then-colon (S3);
  bounded verb morphology (S4); copula value floor (S5); seed min-length (S6);
  mixed-position fusion scan (S7); plus hot-path restoration (gate-first
  per-view, fused bearer alternation, guard check ordering).
- **R2 (`e59f1ea` + `c4624da`)** — S5 floor refined to the secret-shape rule
  (evidence corpus intact, prose rows clean); boundary-faithful fusion
  (RT2-D1-01 — joins span strip-derived gaps only, never a real space);
  copula-arm decoy-token continuation (RT2-D1-02); accented-prose residual
  documented (RT2-D1-03).
- **R3 (`e88cec4`)** — decoy continuation bounded to one prose-token skip
  (RT3-D1-01); fused-stream phrase windows (RT3-D1-02 — `cm\u200bd\u200bexe` /
  `re\u200bg\u200bdelete` block at CRITICAL with plain-form parity); secret-run
  value redaction (RT3-D1-03).
- **R4 (`d433a5b`)** — shape-sharpened decoy/value classification (prose-shaped
  = all-lowercase ≤ 10 or Capitalized-form; secret-shaped = everything else
  len ≥ 6), skip bound raised to two, bridge words covered, evidence pin
  restored (B01 back to must-catch).
- **R5 (`9069ac2`)** — bridge-continued secret-run redaction: the maximal
  secret-shaped run continues across at most two prose-shaped bridge tokens, so
  the r4 X01–X03 leak rows ("password is ABCDEF and supersecret123") leave no
  raw survivor in any sink; constraint table extended.

## Re-verification verdicts (rounds r2–r5)

Every round's closures were re-verified differential (both-heads execution
where applicable), with zero unexpected regressions in every round: r2 closed
all first-pass findings (241 row-instances; one NEW fail-closed FP found —
RT2-D1-01, fixed by R2/R3); r3 verified R2's closures real (181 rows) but found
the unbounded decoy skip (RT3-D1-01, fixed by R3); r4 verified R3's closures
(199 rows, 35 novel probes) but found the two-decoy dodge (RT4-D1-01, fixed by
R4 and confirmed by r5) and the bridge-word member (RT4-D1-02, closed by R5);
r5 verified R4's claims (constraint table 22/22 strict, B01
differential-proven, Vault2024 ruling pinned) with the aggregate sweep showing
368/377 rows no-diff and 9 adjudicated closures, zero unexpected drift — its
single forced finding (RT5-D1-01, the fail-open X01–X03 bridge class) is the
one R5 closed.

Final status: all rounds' closures verified, zero unexpected regressions across
the whole cycle; MISSION-GRADE STABLE was pending the final r6 re-verification
of the R5 commit at documentation time (the pre-adjudicated stop rule — any
raw-secret-persists row forces FINDINGS — was satisfied by closing X01–X03).
The pinned regression corpus for everything above lives in
`tests/test_v07_redteam_repairs.py`.

## Performance adjudication (honest, as recorded for v0.7.0)

The user-facing speed gate PASSED at every official measurement: end-to-end
per-action latency within noise of the `439a043` baseline (paired adjacent
deltas −3.6%…+1.8% across type/keypress/hotkey proxies), full-suite wall time
100.2–101.9 s against the 100.26 s baseline while the suite grew by +218 tests
(1594 passed + 10 skipped at `9069ac2` vs 1376 + 8 at baseline), and captures
identical (9 per 3-action flow, zero increase). The STRICT pre-registered
per-function rule (×1.10 gate) FAILED at the release-candidate measurement on
13 of 36 gated functions: the dominant cluster is the dual-view obfuscated-input
path (`classify.type_obfuscated` ×1.85, `evaluate.type_obfuscated` ×1.76 —
µs-scale absolutes, 20–63 µs), the security-mandated cost of the R2–R4
fusion-scan repairs; removing the dual-view scan is prohibited (it is what
catches zero-width obfuscation the baseline missed). The remaining rows are
long-text redaction scans (×1.12–1.17 on 2 KB inputs) and µs-scale guard rows —
all invisible at the ~300 ms end-to-end action floor, and disclosed with raw
numbers in the maintainer-local `perf-release-verdict.md` artifact of the
mission workspace.
