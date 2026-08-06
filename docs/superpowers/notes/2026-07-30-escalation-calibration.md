# Judge-escalation calibration — findings

**Verdict: no confidence rule clears the viability bar. The escalation change was not
shipped, and should not be shipped from this corpus.**

This is the Task 3 gate outcome of
`docs/superpowers/plans/2026-07-30-latency-judge-escalation.md`, which allows "no viable
rule" as a successful ending. It is recorded here so nobody rebuilds the corpus to
rediscover it — the build costs ~45 minutes of wall clock and **$9.22** of real judge calls.

Shipped from that plan: the collector (`tools/build_calibration_corpus.py`), the analyser
(`tools/analyse_calibration.py`), and the hot-path latency guard
(`tests/test_latency_guard.py`) — PR #13. Not shipped, deliberately: `confidence.py`,
`MatcherConfig.escalate_to_judge`, `escalation_threshold`. Those config fields do not exist
and no threshold was written into the code.

---

## Corpus

| | |
|---|---|
| Built | 2026-07-31 |
| Source | `~/.claude/history.jsonl` (6,941 lines), triage-skipped prompts excluded, deduped by hash |
| Rows | 250 (`tmp-calibration/corpus-final.jsonl`, gitignored — hashes only, no prompt text) |
| Usable | 238 (95.2%) — excluded: 7 `timeout`, 3 `hook_contaminated`, 2 `unparseable` |
| top-K per prompt | 15 (`max_candidates`) |
| Prompt length | p50 12 words |
| Judge cost | p50 14,820 ms · p95 46,973 ms (all 250 rows incl. failed attempts) · mean $0.0378/call · **$9.22 total** |

**Index it was built against.** The post-catalog-refresh index (PR #9), as the plan
required — a threshold calibrated against the old 338-entry index would have been invalid.
A `doctor` snapshot was not captured at build time; the corpus itself is the evidence:
**91 distinct skill names** appear across the 250 top-15 rankings, consistent with the
~93 pickable entries the refresh produced and impossible against a 338-entry pool.

### Label distribution (238 usable)

| Label | n | share | meaning |
|---|---|---|---|
| `declined` | 145 | 60.9% | judge ran and deliberately returned zero picks — skipping it surfaces a pick the judge would have suppressed |
| `disagreed` | 64 | 26.9% | judge picked something other than the embedding top-1 |
| `agreed` | 29 | 12.2% | judge confirmed the embedding top-1 — skipping it costs nothing |

**needs-judge = declined + disagreed = 209 of 238 (87.8%).** Only 12.2% of prompts are ones
where the judge is provably wasted, which is the headline result: there is very little to
save even with a perfect rule. The judge disagrees with the embedding top-1 on 64 of the 93
prompts where it picked anything at all (68.8%).

---

## The bar, set before the numbers were seen

From the plan: a rule is viable when at a **single threshold** it achieves
**recall ≥ 0.85** on needs-judge rows *and* an **escalation rate ≤ 0.60**.

**Nothing came close.** Every signal's recall and escalation rate move together almost
one-for-one, so recall ≥ 0.85 was only ever reached at escalation rates of 89–100%:

| signal | best recall at esc ≤ 0.60 | esc rate needed for recall ≥ 0.85 |
|---|---|---|
| `norm_gap_to_mean` | 0.498 @ 49.2% | 89.9% |
| `z_of_top1` | 0.531 @ 50.4% | 89.9% |
| `abs_top1` | 0.536 @ 48.7% | 89.1% |
| `words` | 0.440 @ 46.2% | 94.1% |
| `naive_margin` (control) | 0.517 @ 49.2% | 100.0% |

---

## Best attempt, recorded in full

`norm_gap_to_mean` — the spec's most-promising candidate. `harmed` is the count of prompts
that would have been answered worse; `lift` is against a random skip of the same size,
which already scores recall ≈ escalation rate.

| q | thresh | esc% | harmed | wasted | disagreements discarded | lift | prec | rec |
|---|---|---|---|---|---|---|---|---|
| 10% | 0.0393 | 10.1% | 188 | 16 | 56/64 (88%) | +2.4% | 0.875 | 0.100 |
| 30% | 0.0554 | 29.0% | 147 | 53 | 48/64 (75%) | −4.0% | 0.899 | 0.297 |
| 50% | 0.0676 | 49.2% | 105 | 85 | 32/64 (50%) | +0.8% | 0.889 | 0.498 |
| 70% | 0.0868 | 69.7% | 61 | 123 | 21/64 (33%) | −2.5% | 0.892 | 0.708 |
| 90% | 0.1173 | 89.9% | 19 | 157 | 7/64 (11%) | −0.8% | 0.888 | 0.909 |

Baseline — "escalate everything", i.e. today's shipped behaviour: precision 0.878,
recall 1.000, **harmed 0**, escalation 100%.

**Read the `lift` column.** It oscillates around zero at every operating point. A signal
that carried real information would show consistent positive lift; this one is
statistically indistinguishable from choosing at random which prompts to skip. The other
three framing-A signals behave the same way (full sweep regenerable — see below).

**The known-dead control stayed dead.** `naive_margin` (top1 − top2) re-measures at a median
gap of **0.0115** on the refreshed index, against 0.012 on the old one. The compressed
BGE-small score distribution is a property of the model, not of the index size.

### The one non-random finding

Under a second framing the analyser added — *predict the decline*, rather than *is the
top-1 right* — two signals showed real, cross-validation-reproducible lift of **+7 to +18
points**: `abs_top1` and `is_lifecycle_top1`. These are the only results in the whole
analysis that beat random.

They still do not clear the bar. At every operating point saving ≥30% of judge calls,
30–63% of true needs-judge rows are harmed and 20–59% of real disagreements are discarded.
There is no elbow where cost falls sharply and harm stays low. Treat them as **the starting
point for a larger corpus**, not as a rule to tune — and specifically do not synthesise a
threshold out of them to force a "yes".

---

## What the live config is actually doing

Measured from `~/.cache/skill-advisor/advisor.events.jsonl`, restricted to the post-fast-fail
era (rows carrying a `judge_failure` key) and to non-triaged prompts — **n = 590**:

```
p50   14,781 ms      judge_used      142  (24.1%)
p95   14,887 ms      judge_failure=timeout  343  (58.1%)
max   15,012 ms      judge_failure=budget_exceeded  5
                     judge_failure=unparseable      2
```

**The judge times out more than twice as often as it answers.** The cause is a config
interaction, not the escalation question. The judge's subprocess timeout is *derived*, not
configured — `judge.py:127` uses `max(budget_seconds - 0.5, 0.5)`, so at the live
`budget_seconds = 15.0` it is **14.5 s**, while the judge's measured p50 cost in this corpus
is **14.8 s**. The timeout sits below the median cost, so the median call cannot finish.

(Note for anyone reaching for the obvious knob: `matcher.judge_timeout_seconds` does not
exist. `judge_timeout_seconds` is a **`ParallelizationConfig`** field, default 5.0,
governing the parallelization detector — a different subprocess. The judge's budget can only
be moved via `budget_seconds`.)

Every one of those 343 prompts pays the full ~14.8 s and then falls back to the embedding
picks it could have had in 261 ms. The fallback is working exactly as designed — this is not
a correctness bug, and no prompt returns empty — but it means the judge is currently
*mostly* a 14.8-second delay in front of the embedding answer.

This reframes the decision below: the realistic choice is not "12 s of judge quality vs.
261 ms" but "24% chance of judge quality for a guaranteed ~15 s". Raising the budget so the
judge usually completes is a third option, and it makes prompts slower, not faster.

## The two remaining options — both the user's call

The plan is explicit that with no viable rule, these are decisions for the user, not for
the plan:

1. **Turn the judge off** (`use_judge = false`, the shipped default). The embedding path is
   ~261 ms. Cost: the 60.9% decline rate is lost, so weak picks the judge currently
   suppresses would be surfaced — but note that today it only gets to suppress them on the
   24.1% of prompts where it beats its own timeout.
2. **Accept its cost.** Live config sits here (`use_judge = true`, `budget_seconds = 15.0`),
   at the measured p50/p95 above. If this is the choice, raise **`budget_seconds`** to at
   least ~16 s so the derived judge timeout (`budget_seconds − 0.5`) clears the 14.8 s median
   and the calls being paid for actually land. Today most of them do not.

Neither is recorded as a deliberate choice anywhere. Change 2 of the spec — the embedding
fallback, PR #2 + PR #7 — removed the empty-handed returns independently of this outcome,
which is why it was sequenced first. Its projected p95 improvement did not materialise,
because that projection assumed a budget *cut* and the live budget was raised instead.

---

## Reproducing

The corpus and sweep live under the gitignored `tmp-calibration/`. Both tools ship:

```bash
uv run python tools/build_calibration_corpus.py --out tmp-calibration/corpus.jsonl --n 250
uv run python tools/analyse_calibration.py --corpus tmp-calibration/corpus.jsonl
```

Before spending the $9 again, note what a *useful* re-run would need: a materially larger
corpus (the 12.2% `agreed` class is only 29 rows here, which is what limits every estimate),
and a feature combining `abs_top1` with a lifecycle-aware signal rather than any single
score-shape statistic. Re-running the same design at n=250 will reproduce this note.
