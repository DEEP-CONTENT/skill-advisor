# Latency: escalate to the judge only when the embedding result is ambiguous — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the 261 ms embedding path always and pay the ~12 s judge only on prompts where the embedding ranking is not confident enough to stand alone — *if and only if* a confidence rule can be derived from measured data.

**Architecture:** This is change 1 of `docs/superpowers/specs/2026-07-29-latency-design.md`. It is **calibration-first**: no threshold is written into the code until a corpus of ≥200 real prompts has shown that some rule separates "the judge agreed with the embedding top-1" from "the judge disagreed or declined". The obvious rule — top1−top2 margin — is already known to fail (measured median gap 0.012). A finding of *no viable rule* is a legitimate, successful outcome of this plan and ends it at Task 3.

**Tech Stack:** Python 3.12, `uv`, pytest, numpy. Calibration tooling lives under `tools/` and is not shipped in the package.

## Global Constraints

- **Do not ship a hardcoded threshold that was not derived from measured data.** This spec would rather ship nothing than ship a number someone guessed. This is the single load-bearing constraint of the plan.
- Hook paths stay **silent on error**. Nothing here may raise out of `hook.run()`.
- The corpus must come from `~/.claude/history.jsonl` (6,907 entries as of 2026-07-30) or be captured going forward. Telemetry hashes prompts and records **no embedding scores** on judge events — verified: 0 of 3,728 — so the corpus cannot be reconstructed from the event log.
- Test suite baseline after the prerequisites is ~430 passing in under 2 s. No test may invoke `claude` or embed with the real model.
- `~/.claude/history.jsonl` is the user's raw prompt history. Keep the corpus and every derived artifact under `/tmp` or a git-ignored path. **Do not commit prompt text.**

---

## Prerequisites — do not start without these

| Prerequisite | Why | Status |
|---|---|---|
| `docs/superpowers/plans/2026-07-30-latency-fast-fail.md` complete | Provides `JudgeResult.failure` (so a calibration run can tell a timeout from a decline) and honest `judge_used` telemetry (so the escalation rate is measurable at all — today the field records `cfg.matcher.use_judge`, not whether the judge ran). | required |
| `docs/superpowers/plans/2026-07-30-catalog-refresh.md` complete | **Shrinks the embedding index from 338 entries to roughly 93.** Every cosine ranking changes, so any threshold calibrated before it is invalid. The two specs claim to be independent and can ship in either order; on this point they are wrong. | required |

Both specs' "Deliberately out of scope" lists still hold: no async judge, no model swap, no verdict caching.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `tools/build_calibration_corpus.py` | **New.** Sample prompts from `history.jsonl`, run the embedding path, run the judge, write one JSONL row per prompt. Slow, offline, run by hand. | Create |
| `tools/analyse_calibration.py` | **New.** Read the corpus, evaluate candidate confidence rules, print precision/recall per rule. Pure analysis — never writes to the package. | Create |
| `docs/superpowers/notes/2026-07-30-escalation-calibration.md` | **New.** The findings: which rule won, at what threshold, with what precision/recall and measured quality delta. Or: that none did. | Create |
| `src/skill_advisor/confidence.py` | **New, only if Task 3 passes.** The derived rule. One pure function over a score vector. | Create (conditional) |
| `src/skill_advisor/matcher.py` | Consults the rule before calling the judge. | Modify (conditional) |
| `src/skill_advisor/config.py` | `MatcherConfig.escalation_*`. | Modify (conditional) |

---

### Task 1: Build the calibration corpus

**Files:**
- Create: `tools/build_calibration_corpus.py`
- Modify: `.gitignore` (add `/tmp-calibration/`)

**Interfaces:**
- Produces: a JSONL corpus at a path given by `--out`. One row per prompt:

```json
{
  "prompt_sha256": "16 hex chars",
  "words": 14,
  "scores": [0.71, 0.70, 0.69, "... max_candidates floats, descending"],
  "names": ["skill-a", "skill-b", "..."],
  "judge_picks": ["skill-b"],
  "judge_declined": false,
  "judge_failure": null,
  "judge_ms": 11840
}
```

  **No prompt text.** The hash is for dedup only; every signal the analysis needs is derivable from `scores`, `names` and `words`.

- [ ] **Step 1: Write the collector**

```python
#!/usr/bin/env python3
"""Build the escalation calibration corpus.

Runs the real embedding path and the real judge over prompts sampled from
`~/.claude/history.jsonl`, and records enough per prompt to evaluate candidate
confidence rules offline. Slow by design — ~12 s of judge per prompt, so 200
prompts is roughly 40 minutes. Run it once, in the background.

Writes no prompt text. The hash is for dedup only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_advisor import index as index_mod  # noqa: E402
from skill_advisor import judge, triage  # noqa: E402
from skill_advisor.config import load as load_config  # noqa: E402


def _iter_history(path: Path):
    for line in path.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = row.get("display") or row.get("prompt") or row.get("text")
        if isinstance(text, str) and text.strip():
            yield text.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=str(Path.home() / ".claude" / "history.jsonl"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=250)
    ap.add_argument("--seed", type=int, default=20260730)
    args = ap.parse_args()

    cfg = load_config()
    idx = index_mod.load()

    seen: set[str] = set()
    pool: list[str] = []
    for text in _iter_history(Path(args.history)):
        if triage.should_skip(text, cfg):
            continue  # triage-skipped prompts never reach the judge; not calibration data
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        if h in seen:
            continue
        seen.add(h)
        pool.append(text)

    random.Random(args.seed).shuffle(pool)
    sample = pool[: args.n]
    print(f"{len(pool)} unique non-triaged prompts available; sampling {len(sample)}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for i, text in enumerate(sample, 1):
            ranked = index_mod.top_k(text, idx, cfg.matcher.max_candidates)
            if not ranked:
                continue
            t0 = time.monotonic()
            verdict = judge.rank(text, [e for e, _ in ranked], cfg)
            judge_ms = int((time.monotonic() - t0) * 1000)
            fh.write(json.dumps({
                "prompt_sha256": hashlib.sha256(text.encode()).hexdigest()[:16],
                "words": len(text.split()),
                "scores": [round(s, 6) for _, s in ranked],
                "names": [e.name for e, _ in ranked],
                "judge_picks": [p.name for p in verdict.picks],
                "judge_declined": verdict.failure is None and not verdict.picks,
                "judge_failure": verdict.failure,
                "judge_ms": judge_ms,
            }) + "\n")
            fh.flush()
            print(f"[{i}/{len(sample)}] {judge_ms:>6} ms  "
                  f"{'DECLINE' if not verdict.picks else verdict.picks[0].name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify the history format before the long run**

`history.jsonl` field names are not guaranteed. Check first:

```bash
head -1 ~/.claude/history.jsonl | python3 -m json.tool
```

If the prompt is not under `display`, `prompt` or `text`, add the correct key to `_iter_history`.

- [ ] **Step 3: Confirm the judge is actually enabled**

```bash
grep -A3 '\[matcher\]' ~/.config/skill-advisor/config.toml
```

`use_judge` must be `true`, or every row records a no-op. It is `true` in the live config; the shipped default is `False`.

- [ ] **Step 4: Do a 5-prompt smoke run**

```bash
mkdir -p /tmp-calibration
uv run python tools/build_calibration_corpus.py --out /tmp-calibration/smoke.jsonl --n 5
python3 -m json.tool < <(head -1 /tmp-calibration/smoke.jsonl)
```

Confirm `scores` has `max_candidates` entries in descending order, and that at least one row has a non-empty `judge_picks`. If every row is `judge_failure: "cli_missing"`, `claude` is not on PATH for this shell.

- [ ] **Step 5: Run the full corpus in the background**

```bash
uv run python tools/build_calibration_corpus.py \
  --out /tmp-calibration/corpus.jsonl --n 250 > /tmp-calibration/build.log 2>&1 &
```

Roughly 45 minutes. **The corpus must be built against the post-catalog-refresh index** — confirm `skill-advisor doctor` reports a pickable count near 93, not 338, before starting.

- [ ] **Step 6: Commit the tool only**

```bash
echo "/tmp-calibration/" >> .gitignore
git add tools/build_calibration_corpus.py .gitignore
git commit -m "tools: collector for the judge-escalation calibration corpus"
```

---

### Task 2: Evaluate candidate confidence rules

**Files:**
- Create: `tools/analyse_calibration.py`

**Interfaces:**
- Produces: a printed table, one row per (rule, threshold): escalation rate, precision, recall, and the count of prompts where skipping the judge would have changed the answer.

**Ground truth.** For each corpus row, the judge is treated as correct and the question is whether the embedding path alone would have matched it:

- `agreed` — the embedding top-1 name is in `judge_picks`. Skipping the judge here costs nothing.
- `disagreed` — `judge_picks` is non-empty and does not contain the embedding top-1. Skipping the judge surfaces a worse pick.
- `declined` — `judge_picks` is empty and `judge_declined` is true. Skipping the judge surfaces a pick where the judge would have shown none. **This is the judge's main value** (1,251 of 1,683 empty verdicts on live data were genuine declines, not timeouts) and the cost of getting it wrong is a false recommendation.

A rule is useful when it escalates on most `disagreed` + `declined` rows and few `agreed` rows.

**Candidate signals**, in the spec's rough order of promise:

1. Normalised gap between top-1 and the mean of top-2..K — `(s0 - mean(s1..sK)) / s0`.
2. The top-1's z-position within that prompt's own top-K distribution — `(s0 - mean(s)) / std(s)`.
3. Absolute top-1 score.
4. Prompt length in words.
5. The naive top1−top2 margin — **included as the known-failing control.** Median gap 0.012 on the old index; if it now looks good, suspect the corpus.

- [ ] **Step 1: Write the analyser**

```python
#!/usr/bin/env python3
"""Evaluate candidate escalation rules against the calibration corpus.

Prints, per rule and threshold: escalation rate, precision, recall, and how
many prompts would have been answered worse. Writes nothing to the package —
the output of this script is a written finding, not code.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def _label(row: dict) -> str:
    picks = row.get("judge_picks") or []
    if row.get("judge_failure"):
        return "skip"          # the judge never answered; no ground truth here
    if not picks:
        return "declined"
    return "agreed" if row["names"][0] in picks else "disagreed"


def _signals(row: dict) -> dict[str, float]:
    s = row["scores"]
    s0 = s[0]
    rest = s[1:] or [0.0]
    mean_all = statistics.fmean(s)
    stdev = statistics.pstdev(s) or 1e-9
    return {
        "norm_gap_to_mean": (s0 - statistics.fmean(rest)) / (s0 or 1e-9),
        "z_of_top1": (s0 - mean_all) / stdev,
        "abs_top1": s0,
        "words": float(row.get("words", 0)),
        "naive_margin": s0 - (s[1] if len(s) > 1 else 0.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.corpus).open(encoding="utf-8") if l.strip()]
    labelled = [(r, _label(r)) for r in rows]
    usable = [(r, lab) for r, lab in labelled if lab != "skip"]

    n_needs = sum(1 for _, lab in usable if lab in ("disagreed", "declined"))
    print(f"corpus {len(rows)} rows · usable {len(usable)} · "
          f"needs-judge {n_needs} ({n_needs / max(len(usable), 1):.1%})")
    print(f"dropped {len(labelled) - len(usable)} rows where the judge itself failed\n")

    for name in ("norm_gap_to_mean", "z_of_top1", "abs_top1", "words", "naive_margin"):
        vals = sorted(_signals(r)[name] for r, _ in usable)
        print(f"--- {name}  p10={vals[len(vals)//10]:.4f} "
              f"p50={vals[len(vals)//2]:.4f} p90={vals[9*len(vals)//10]:.4f}")
        print(f"{'thresh':>10} {'escalate%':>10} {'precision':>10} {'recall':>8} {'harmed':>7}")
        for q in range(5, 100, 5):
            thresh = vals[int(len(vals) * q / 100)]
            tp = fp = fn = harmed = 0
            for r, lab in usable:
                # Escalate when the signal is LOW — low separation means ambiguous.
                escalate = _signals(r)[name] < thresh
                needs = lab in ("disagreed", "declined")
                if escalate and needs:
                    tp += 1
                elif escalate and not needs:
                    fp += 1
                elif not escalate and needs:
                    fn += 1
                    harmed += 1
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec = tp / (tp + fn) if (tp + fn) else 0.0
            rate = (tp + fp) / len(usable)
            print(f"{thresh:>10.4f} {rate:>9.1%} {prec:>10.3f} {rec:>8.3f} {harmed:>7}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it**

```bash
uv run python tools/analyse_calibration.py --corpus /tmp-calibration/corpus.jsonl \
  | tee /tmp-calibration/analysis.txt
```

- [ ] **Step 3: Commit the tool**

```bash
git add tools/analyse_calibration.py
git commit -m "tools: evaluate candidate judge-escalation rules against the corpus"
```

---

### Task 3: The viability gate

**This task decides whether the rest of the plan happens.** The spec: "If no rule achieves a useful separation, say so and stop."

**Files:**
- Create: `docs/superpowers/notes/2026-07-30-escalation-calibration.md`

**Interfaces:** none. This task produces a written finding.

**Viability bar**, set now so it cannot be rationalised after seeing the numbers:

A rule is viable when, at a single threshold, it achieves **recall ≥ 0.85** on needs-judge rows *and* an **escalation rate ≤ 0.60**. Rationale: recall below 0.85 means more than 15% of the prompts where the judge would have changed the answer now get a worse one, and the spec is explicit that quality loss must be quantified rather than discovered in use. An escalation rate above 0.60 saves too little to justify the added branch and the added risk — at 60% escalation, p50 moves from ~9.7 s to only ~7 s.

- [ ] **Step 1: Read the analysis against the bar**

Find the best (rule, threshold) pair by recall subject to escalation rate ≤ 0.60.

- [ ] **Step 2: Write the finding**

Create `docs/superpowers/notes/2026-07-30-escalation-calibration.md` containing: corpus size and build date; the index it was built against (pool and pickable counts from `doctor`); the label distribution; the full table for the winning rule; the chosen threshold; and the measured `harmed` count — the number of prompts that would have been answered worse.

If **no rule clears the bar**, say so plainly and record the best attempt anyway, so the next person does not repeat the run. Then state the two remaining options from the spec: turn the judge off entirely and live on the embedding path, or accept its cost. Both are decisions for the user, not for this plan.

- [ ] **Step 3: Commit and branch**

```bash
git add docs/superpowers/notes/2026-07-30-escalation-calibration.md
git commit -m "docs: judge-escalation calibration findings"
```

**If viable → continue to Task 4.**
**If not viable → stop here.** Report the finding. The fast-fail plan already delivered the p95 improvement and removed the empty timeouts; that stands on its own, which is exactly why it was sequenced first.

---

### Task 4: Implement the derived rule

**Only if Task 3 passed.** Everywhere below, `<SIGNAL>` and `<THRESHOLD>` are the values recorded in the calibration note — not placeholders to invent.

**Files:**
- Create: `src/skill_advisor/confidence.py`
- Modify: `src/skill_advisor/config.py` (`MatcherConfig`), `src/skill_advisor/matcher.py`
- Test: `tests/test_confidence.py` (create), `tests/test_matcher.py`

**Interfaces:**
- Produces:
  - `confidence.score(scores: Sequence[float]) -> float` — the winning signal, computed from one prompt's descending top-K cosine scores. Pure; no config, no I/O.
  - `confidence.should_escalate(scores: Sequence[float], threshold: float) -> bool` — `True` when the ranking is *not* confident. Fewer than 2 scores always escalates: no separation can be measured from one number.
  - `config.MatcherConfig.escalate_to_judge: bool = True` and `escalation_threshold: float = <THRESHOLD>`, with a docstring naming the calibration note, the corpus size and the measured recall.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_confidence.py`:

```python
import pytest

from skill_advisor import confidence


def test_a_clear_winner_is_confident():
    assert confidence.should_escalate([0.90, 0.30, 0.28, 0.25], threshold=0.20) is False


def test_a_flat_ranking_escalates():
    """Median top1-top2 gap on real data is 0.012. The naive margin test was
    rejected for exactly this reason; whatever rule replaced it must still
    escalate here."""
    assert confidence.should_escalate([0.701, 0.700, 0.699, 0.698], threshold=0.20) is True


def test_fewer_than_two_scores_always_escalates():
    assert confidence.should_escalate([0.9], threshold=0.20) is True
    assert confidence.should_escalate([], threshold=0.20) is True


def test_score_is_monotonic_in_separation():
    tight = confidence.score([0.70, 0.699, 0.698])
    loose = confidence.score([0.90, 0.30, 0.28])
    assert loose > tight


def test_score_handles_a_zero_top_score_without_dividing_by_zero():
    assert confidence.score([0.0, 0.0, 0.0]) == pytest.approx(0.0, abs=1e-6)
```

Append to `tests/test_matcher.py`:

```python
def test_a_confident_prompt_does_not_call_the_judge(isolated_paths):
    from skill_advisor.config import Config, MatcherConfig

    cfg = Config(matcher=MatcherConfig(use_judge=True, escalate_to_judge=True,
                                       escalation_threshold=0.20))
    stub = _prime_stateless_index([0.95, 0.20, 0.10])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        result = matcher.pick_stateless("q", cfg, top_k=3, candidates=3, threshold=0.0)

    mock_rank.assert_not_called()
    assert [p.entry.name for p in result.picks] == ["alpha", "beta", "gamma"]


def test_an_ambiguous_prompt_calls_the_judge_exactly_once(isolated_paths):
    from skill_advisor.config import Config, MatcherConfig
    from skill_advisor.judge import JudgeResult
    from skill_advisor.judge import Pick as JudgePick

    cfg = Config(matcher=MatcherConfig(use_judge=True, escalate_to_judge=True,
                                       escalation_threshold=0.20))
    stub = _prime_stateless_index([0.701, 0.700, 0.699])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        mock_rank.return_value = JudgeResult(picks=[JudgePick(name="beta", reason="fits")])
        result = matcher.pick_stateless("q", cfg, top_k=3, candidates=3, threshold=0.0)

    assert mock_rank.call_count == 1
    assert [p.entry.name for p in result.picks] == ["beta"]


def test_escalation_can_be_switched_off(isolated_paths):
    """escalate_to_judge=False must restore judge-always behaviour exactly."""
    from skill_advisor.config import Config, MatcherConfig
    from skill_advisor.judge import JudgeResult

    cfg = Config(matcher=MatcherConfig(use_judge=True, escalate_to_judge=False))
    stub = _prime_stateless_index([0.95, 0.20, 0.10])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        mock_rank.return_value = JudgeResult(picks=[])
        matcher.pick_stateless("q", cfg, top_k=3, candidates=3, threshold=0.0)

    mock_rank.assert_called_once()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_confidence.py tests/test_matcher.py -q -k "confiden or escalat or ambiguous"
```

Expected: FAIL — `ModuleNotFoundError: No module named 'skill_advisor.confidence'`.

- [ ] **Step 3: Create `confidence.py`**

Implement `score()` as the winning signal from the calibration note. If it was `norm_gap_to_mean`:

```python
"""Whether an embedding ranking is confident enough to skip the judge.

The threshold in `MatcherConfig.escalation_threshold` was derived from a corpus
of real prompts, not chosen — see
`docs/superpowers/notes/2026-07-30-escalation-calibration.md` for the corpus
size, the rules evaluated, and the measured precision/recall. Do not adjust it
by intuition; rebuild the corpus and re-run `tools/analyse_calibration.py`.

The obvious rule — the top1-top2 margin — was measured and rejected: BGE-small
produces tightly-clustered scores with a median neighbour gap of 0.012, so a
margin test escalates on nearly every prompt and saves nothing.
"""
from __future__ import annotations

import statistics
from typing import Sequence


def score(scores: Sequence[float]) -> float:
    """Separation of the top-1 from the rest of its own top-K, normalised.

    Normalising by the top-1 makes the value comparable across prompts, which
    raw cosine differences are not: BGE-small's absolute scores sit in a narrow
    band that shifts with prompt length.
    """
    if len(scores) < 2:
        return 0.0
    top = float(scores[0])
    if top <= 0.0:
        return 0.0
    rest = statistics.fmean(float(s) for s in scores[1:])
    return (top - rest) / top


def should_escalate(scores: Sequence[float], threshold: float) -> bool:
    """True when the ranking is ambiguous and the judge is worth ~12 seconds.

    Fewer than two scores always escalates — no separation can be measured
    from a single number, and guessing 'confident' there would be the exact
    silent quality loss this design is trying to avoid.
    """
    if len(scores) < 2:
        return True
    return score(scores) < threshold
```

- [ ] **Step 4: Add the config knobs**

```python
    # Call the judge only when the embedding ranking is ambiguous. The threshold
    # was DERIVED from a corpus of <N> real prompts at recall <R>, escalation
    # rate <E> — see docs/superpowers/notes/2026-07-30-escalation-calibration.md.
    # Set escalate_to_judge = false to restore judge-always behaviour.
    escalate_to_judge: bool = True
    escalation_threshold: float = <THRESHOLD>
```

and parse both in the `[matcher]` loader alongside `use_judge`.

- [ ] **Step 5: Gate the judge in the matcher**

In `pick_stateless`, replace the `if use_judge:` line's condition:

```python
    escalate = True
    if use_judge and cfg.matcher.escalate_to_judge:
        escalate = confidence.should_escalate(
            [s for _, s in ranked], cfg.matcher.escalation_threshold
        )
        if not escalate:
            log.debug("embedding ranking confident; judge skipped")

    if use_judge and escalate:
        ...  # unchanged body from the fast-fail plan
```

The confident path falls through to the existing `return StatelessResult(picks=_embedding_picks(ranked, k_picks, min_score))`. Add `from . import confidence` to the imports.

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src/skill_advisor/confidence.py src/skill_advisor/config.py src/skill_advisor/matcher.py tests/test_confidence.py tests/test_matcher.py && uv run ruff format --check src/skill_advisor/confidence.py src/skill_advisor/config.py src/skill_advisor/matcher.py tests/test_confidence.py tests/test_matcher.py
git add src/skill_advisor/confidence.py src/skill_advisor/config.py src/skill_advisor/matcher.py tests/test_confidence.py tests/test_matcher.py
git commit -m "perf(matcher): escalate to the judge only on ambiguous embedding results

Threshold derived from a <N>-prompt corpus; see the calibration note."
```

---

### Task 5: Guard the hot path against a future subprocess

The spec asks for "a floor-level guard so a future change cannot silently reintroduce a subprocess into the hot path". This only becomes meaningful now, when the embedding-only path is the *common* case rather than a fallback.

**Files:**
- Test: `tests/test_latency_guard.py` (create)

**Interfaces:** none.

- [ ] **Step 1: Write the test**

```python
"""Floor-level guard, not a benchmark.

The ceiling is loose on purpose — this must not go flaky on a loaded CI box.
It exists to catch a whole extra `claude -p` round-trip (~12 s) sneaking back
into the confident path, which is a 100x violation, not a 2x one.
"""
import time
from unittest.mock import patch

import numpy as np
import pytest

from skill_advisor import catalog as catalog_mod
from skill_advisor import index as index_mod, matcher
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config, MatcherConfig

CEILING_SECONDS = 1.0


def test_the_confident_path_spawns_no_subprocess(isolated_paths):
    entries = [
        CatalogEntry(kind="skill", name="alpha", namespace="user", description="d",
                     path="/s/alpha/SKILL.md", enabled=True),
        CatalogEntry(kind="skill", name="beta", namespace="user", description="d",
                     path="/s/beta/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.2, 0.98]], dtype=np.float32)
    catalog_mod.save(entries)
    index_mod.save(entries, emb, "h")

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    cfg = Config(matcher=MatcherConfig(use_judge=True, escalate_to_judge=True,
                                       escalation_threshold=0.20))

    def _explode(*a, **k):
        raise AssertionError("a subprocess was spawned on the confident path")

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()), \
         patch("skill_advisor.judge.subprocess.run", _explode), \
         patch("skill_advisor.parallelization.subprocess.run", _explode):
        started = time.monotonic()
        result = matcher.pick_stateless("q", cfg, top_k=2, candidates=2, threshold=0.0)
        elapsed = time.monotonic() - started

    assert result.picks
    assert elapsed < CEILING_SECONDS, f"confident path took {elapsed:.2f}s"
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_latency_guard.py -q -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_latency_guard.py
git commit -m "test: guard the confident embedding path against a reintroduced subprocess"
```

---

### Task 6: Measure the outcome on real data

The spec: "the final report must state the achieved escalation rate, the resulting p50/p95, and the measured quality delta against the judge-always baseline. If the quality delta is unacceptable, that is a valid result and the change should not ship."

**Files:**
- Modify: `docs/superpowers/notes/2026-07-30-escalation-calibration.md`
- Modify: `docs/superpowers/specs/2026-07-29-latency-design.md`, `README.md`

- [ ] **Step 1: Snapshot the pre-change baseline**

Do this **before** the new default reaches the live config:

```bash
python3 - <<'PY'
import json, statistics
from pathlib import Path
rows = [json.loads(l) for l in (Path.home()/'.cache/skill-advisor/advisor.events.jsonl').open() if l.strip()]
p = [r for r in rows if r.get('kind') == 'prompt' and not r.get('triage_skipped')]
d = sorted(r['duration_ms'] for r in p)
print('n', len(d), 'p50', d[len(d)//2], 'p95', d[int(len(d)*0.95)])
print('judge ran, zero picks:', sum(1 for r in p if r.get('judge_used') and not r.get('picks')))
PY
```

Note that `judge_used` is only trustworthy for events written after the fast-fail plan landed. Filter by `ts` to that date.

- [ ] **Step 2: Run for a week, then re-measure**

Re-run the same snippet. Report: escalation rate (`judge_used=true` as a fraction of non-triaged prompts), p50, p95, and the count of judge-ran-zero-picks.

- [ ] **Step 3: Measure the quality delta**

Escalation rate and latency are not the deliverable on their own — the quality delta is. Re-run the corpus collector against the *current* index, then compare each row's shipped behaviour to its judge-always verdict:

```bash
uv run python tools/build_calibration_corpus.py --out /tmp-calibration/verify.jsonl --n 100 --seed 99
uv run python tools/analyse_calibration.py --corpus /tmp-calibration/verify.jsonl
```

The `harmed` column at the shipped threshold is the quality delta. Record it as an absolute count and a percentage.

- [ ] **Step 4: Decide, and say so**

Append a "Measured outcome" section to the calibration note with all four numbers. **If the quality delta is worse than the calibration predicted, set `escalate_to_judge = false` in the live config and record why.** That is a valid outcome, not a failure — the flag exists precisely so this decision does not require a revert.

- [ ] **Step 5: Update the spec and README**

Replace the spec's "Expected effect" projection with the measured numbers — the projection was explicitly labelled "must be re-measured, not assumed". Document `escalate_to_judge` and `escalation_threshold` in the README, including that the threshold is derived and must not be hand-tuned.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/notes/2026-07-30-escalation-calibration.md docs/superpowers/specs/2026-07-29-latency-design.md README.md
git commit -m "docs: measured outcome of judge escalation"
```

---

## Self-Review

**Spec coverage (change 1 only).** "Escalate only when ambiguous" → Task 4. "The confidence rule is NOT yet determined" → Tasks 1-3, with an explicit stop at Task 3. All four numbered calibration requirements → Task 1 (≥200 prompts from `history.jsonl`, embedding top-K with scores plus the judge's verdict per prompt), Task 2 (a rule separating agreed from disagreed-or-declined, with precision and recall), Task 3 (say so and stop). "Do not ship a hardcoded threshold that was not derived from measured data" → the Task 3 gate and the `<THRESHOLD>` placeholder in Task 4, which is deliberately un-fillable until the note exists. "Escalation gating" test with the judge stubbed and asserting call count → Task 4 Step 1. "Latency has a regression test" → Task 5. "Measure the outcome on real data" → Task 6. Change 2 is in `2026-07-30-latency-fast-fail.md`.

**Placeholder scan.** `<SIGNAL>`, `<THRESHOLD>`, `<N>`, `<R>`, `<E>` in Task 4 are the one intentional exception, and they are the point: the plan is structured so those values cannot be invented, only measured. Everything else carries real code. Task 1 Step 2 flags that `history.jsonl`'s field names must be confirmed rather than assumed.

**Type consistency.** `confidence.score` / `confidence.should_escalate` have the same signatures in Task 4 Steps 1, 3 and 5 and in the Task 5 guard. `MatcherConfig.escalate_to_judge` / `escalation_threshold` are spelled identically in Steps 4, 5 and both test files. The corpus row schema in Task 1 is exactly what `_label` and `_signals` read in Task 2 — `scores`, `names`, `judge_picks`, `judge_declined`, `judge_failure`, `words`. `judge.rank()` returning a `JudgeResult` with `.failure` (never `None`) is relied on by the Task 1 collector and comes from the fast-fail plan's Task 1.
