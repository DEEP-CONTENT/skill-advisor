# Latency reduction — design

**Date:** 2026-07-29
**Status:** approved, ready for implementation planning
**Scope:** one sub-project. Catalog refresh is a separate spec
(`2026-07-29-catalog-refresh-design.md`); the two are independent.

---

## Problem

Every prompt waits on the advisor before Claude Code sees it. Measured from the live event
log on 2026-07-29 (4,565 timed events):

```
overall            p50   9,701 ms   p95  24,803 ms
judge_used=True    p50  11,888 ms   p95  24,811 ms    n=3,727
judge_used=False   p50     261 ms   p95     308 ms    n=  838
triage skipped     p50       0 ms   p95       4 ms    n=  830
```

**The judge costs 45× the latency of the embedding path.** Cumulative wall-clock spent
waiting on the advisor: **701 minutes — 11.7 hours — across 4,565 prompts.**

None of it is local computation. Measured directly:

```
import skill_advisor modules :   83 ms
load catalog + embeddings    :    3 ms   (338 entries)
embed prompt + cosine top-15 :  281 ms   (cold, includes fastembed init)
second top_k (warm)          :  101 ms
--- local pipeline total     :  467 ms
```

The entire remainder is the `claude -p` subprocess, whose cost is Claude Code's
session-startup overhead — skill-list injection, session hooks, MCP servers — not the
model's thinking time. It cannot be optimised from our side; it can only be avoided.

### The waste is worse than the average suggests

Of 3,077 non-triage judge events:

| Duration band | produced picks | produced nothing |
|---|---|---|
| fast < 5s | 443 | 9 |
| 5–15s | 597 | 759 |
| 15–24s | 340 | 483 |
| **≥ 24s (at the 25s budget ceiling)** | **14** | **432** |

**432 prompts burned the full 25-second budget and returned nothing at all — roughly 3
hours of pure waste.** When the judge is fast it almost always produces something (443 vs
9); when it is slow it almost never does (14 vs 432).

Of the 1,683 judge events that produced no picks, 432 (26%) were budget timeouts. The other
1,251 were genuine declines — the judge deciding nothing fit. That filtering is real and is
the reason the judge exists; it is not what this spec removes.

### Why this matters now

The effort-signalling feature just shipped, and its stated purpose is making cheap tasks
feel cheap. It was deliberately built to add zero new round-trips. But it is fighting a
~10-second floor on every keystroke to deliver a recommendation about efficiency, which is
close to self-defeating.

---

## Design

> **Status 2026-07-30:** change 2 is implemented — see
> `docs/superpowers/plans/2026-07-30-latency-fast-fail.md`. Change 1 (judge
> escalation) is deferred behind the catalog refresh, because that work shrinks
> the embedding index from 338 to ~93 entries and invalidates any confidence
> threshold calibrated before it. See
> `docs/superpowers/plans/2026-07-30-latency-judge-escalation.md`.

Two changes, independent, either shippable alone.

### 1. Escalate to the judge only when the embedding result is ambiguous

Run embeddings always (261 ms). Call the judge only when the embedding ranking is not
confident enough to stand on its own.

```
prompt
  → triage (0 ms)
  → embed + cosine top-K (261 ms)          ← always
  → confident?  ─ yes → emit embedding picks, done
                └ no  → judge (≈12 s), emit judge picks
```

The judge's value is precision — its 1,251 declines are cases where the embedding path
would have surfaced a weak pick instead. That value is concentrated on ambiguous prompts.
On a prompt where one skill is an obvious match, paying 12 seconds to confirm it is waste.

#### The confidence rule is NOT yet determined — and the obvious one does not work

The natural rule is "escalate when the top two scores are close". Measured against the
committed calibration set:

```
n=10 calibration prompts
  top-1 score    p10 0.645   p50 0.698   p90 0.728
  top1−top2 gap  p10 0.008   p50 0.012   p90 0.085
```

**A median top1–top2 gap of 0.012 means a naive margin test would escalate on almost every
prompt**, defeating the entire change. BGE-small produces compressed, tightly-clustered
scores; absolute cosine values sit in a narrow band and differences between neighbours are
tiny. The calibration set is also only 10 prompts — too small to derive a threshold from.

**Implementation therefore begins with a calibration task, not with a threshold.** It must:

1. Build a corpus of at least ~200 real prompts. Telemetry hashes prompts, so these must
   come from `~/.claude/history.jsonl` or be captured going forward — not reconstructed
   from the event log.
2. For each, record the embedding top-K with scores, and the judge's verdict.
3. Find a rule that separates "judge agreed with the embedding top-1" from "judge
   disagreed or declined", and report its precision and recall.
4. If **no rule achieves a useful separation, say so and stop.** The correct outcome is then
   to turn the judge off entirely or accept its cost — not to ship a threshold that
   escalates on everything (no saving) or nothing (silent quality loss).

Candidate signals to evaluate, in rough order of promise: normalised gap between top-1 and
the mean of top-2..K; the score's z-position within that prompt's own top-K distribution;
prompt length and technical-keyword density; whether the top-1 is a lifecycle-phase
preference (already deterministic, never needs the judge).

**Do not ship a hardcoded threshold that was not derived from measured data.** This spec
would rather ship nothing than ship a number someone guessed.

### 2. Fail fast, and never return empty-handed

Two changes to the budget path, both small and both independent of the calibration work.

**Cut `budget_seconds` from 25 to ~8.** (Correction, 2026-07-30: 25 was never the shipped
default — it was the author's local `config.toml` override, which is what the telemetry
above was measured against. The shipped default, `MatcherConfig.budget_seconds`, was 4.0.
This change unifies both onto a single number: the shipped default is now 8.0.) The band
table shows the judge answers usefully under 15 seconds or not at all — the ≥24s band
produced 14 picks against 432 nothings. A tight budget forfeits very few real picks and
eliminates the worst of the waste.

The current 25-second default exists because the parallelization detector needs
`judge_timeout_seconds + 3`. Reducing the budget therefore requires reducing
`parallelization.judge_timeout_seconds` in step, or accepting that the parallelization check
does not run under the shorter budget. `doctor` already warns when
`budget_seconds < judge_timeout_seconds + 3`; that check must stay honest after the change.

**Fall back to embedding picks on timeout.** Today a judge overrun exits silent — the
embedding ranking was already computed and is thrown away. Emit it instead. This alone
converts 432 empty 25-second waits into 432 useful answers, and it holds regardless of what
the calibration finds.

(Implementation note, 2026-07-30: `judge.rank()` collapsed six distinct failure modes — no
candidates, `claude` missing from `PATH`, subprocess timeout, subprocess error, non-zero
exit, unparseable reply — into a single `None` return. The fallback above could not be
built against that signature: nothing distinguished "the judge failed to produce a verdict"
from "the judge ran and deliberately returned zero picks," and falling back on the latter
would silently overrule a real decline. The return contract had to change first —
`judge.rank()` now always returns a `JudgeResult`, whose `failure` field is `None` only
when the judge ran and answered.)

---

## Expected effect

If escalation proves viable at, say, a 25% escalation rate:

```
                        now          projected
p50 latency          9,701 ms       ~600 ms
p95 latency         24,803 ms      ~8,300 ms
empty timeouts           432              0
```

Precise numbers depend entirely on the calibration outcome and must be re-measured, not
assumed. **Change 2 alone** — the budget cut plus the fallback — removes the empty timeouts
and takes p95 to roughly 8 s with no quality risk at all, which is why it is worth shipping
even if change 1 proves infeasible.

---

## Failure modes

| Failure | Behaviour |
|---|---|
| Judge times out under the shorter budget | Emit the embedding picks already computed. Never silent. |
| Confidence rule cannot be calibrated | Ship change 2 only; escalate the judge decision back to the user. |
| Confidence rule mis-classifies a prompt as confident | A weaker pick is surfaced. Quality regression, not a failure — must be quantified during calibration, not discovered in use. |
| `budget_seconds` set below `judge_timeout_seconds + 3` | `doctor` warns, as today. |
| Embedding index missing | Unchanged: log and exit silent. |
| `hook.py`'s whole-hook `signal.alarm(budget_seconds)` fires | A second, independent timeout layer from the judge's own subprocess timeout — it wraps the entire `matcher.pick()` call, not just the judge. If it fires, the hook exits silent (no picks at all), unlike the judge-timeout fallback above. The judge's own subprocess timeout must stay strictly below this ceiling so the fallback is what actually fires in practice. |

---

## Testing

- **The fallback, driven by a real timeout.** Force the judge to exceed the budget and
  assert embedding picks are emitted rather than nothing. This is the change that recovers
  the 3 wasted hours; it needs a test that fails against today's code.
- **Escalation gating.** A confident prompt calls the judge zero times; an ambiguous one
  calls it once. Assert on call count with the judge stubbed, so the test is fast and
  deterministic.
- **The budget interaction.** A config with `budget_seconds` below
  `judge_timeout_seconds + 3` still produces a `doctor` warning after the default changes.
- **Latency has a regression test.** Assert the embedding-only path stays under a
  wall-clock ceiling with the judge stubbed out. Not a benchmark — a floor-level guard so a
  future change cannot silently reintroduce a subprocess into the hot path.

**Measure the outcome on real data, not fixtures.** The calibration corpus is the
deliverable that makes this spec honest; the final report must state the achieved
escalation rate, the resulting p50/p95, and the measured quality delta against the
judge-always baseline. If the quality delta is unacceptable, that is a valid result and the
change should not ship.

---

## Deliberately out of scope

- **Running the judge asynchronously** and applying its verdict to the *next* prompt. Zero
  perceived latency, but every recommendation is one prompt stale and the design grows
  substantially. Reconsider only if escalation fails to calibrate.
- **Replacing BGE-small.** A model with better score separation would make the confidence
  rule easy, but changes every stored embedding and the whole quality profile.
- **Caching judge verdicts by prompt similarity.** Plausible, but correctness is subtle
  (near-duplicate prompts are not equivalent prompts) and the escalation change should be
  measured first.
- **The parallelization detector's own cost.** It rides the same `claude -p` overhead and
  fires rarely; it is in scope only insofar as its timeout constrains `budget_seconds`.
