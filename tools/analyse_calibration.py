#!/usr/bin/env python3
"""Evaluate candidate judge-escalation rules against the calibration corpus.

Two framings are evaluated, separately, against the same corpus:

  A (the plan's mandate) — "is the embedding top-1 right?"
    positive/"needs judge" = judge disagreed or declined (209 of 238 usable)
    negative/"skip is free" = judge agreed with the embedding top-1 (29 of 238)

  B (an addition) — "will the judge decline?"
    positive/"skip is free" = judge declined (145 of 238)
    negative/"needs judge"  = judge picked something, agree or disagree (93)

For every (signal, framing, threshold) this prints escalation rate, precision,
recall, the "harmed" count (rows where skipping would have changed the
answer), and the fraction of real judge *disagreements* the rule would have
discarded — disagreements are the costliest miss (a worse pick ships) and are
tracked the same way regardless of which framing produced the rule.

Thresholds are never fit and scored on the same rows. Every number in the
"cv" columns comes from 5-fold cross-validation: for each fold, a threshold
is picked from the *other* folds' distribution and direction, then applied
to the held-out fold; every row contributes exactly one out-of-fold
prediction, and those are pooled into the reported confusion matrix. An
"in-sample" column is printed alongside it, fit and scored on all 238 rows,
so the overfitting gap itself is visible rather than asserted.

Writes nothing to the package — the output of this script is a written
finding, not code.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_advisor.lifecycle import PHASE_CANDIDATES  # noqa: E402

# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------


def label3(row: dict) -> str:
    """agreed | disagreed | declined | skip (judge itself never answered)."""
    if row.get("judge_failure"):
        return "skip"
    picks = row.get("judge_picks") or []
    if not picks:
        return "declined"
    return "agreed" if row["names"][0] in picks else "disagreed"


# ---------------------------------------------------------------------------
# Candidate signals
# ---------------------------------------------------------------------------

_LIFECYCLE_NAMES: set[str] = {
    name.rsplit(":", 1)[-1]
    for candidates in PHASE_CANDIDATES.values()
    for _, name in candidates
}


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
        # 1.0 if the embedding top-1 is a name lifecycle.py would also have
        # pushed for *some* phase — not that a lifecycle is active for this
        # prompt (the corpus doesn't record session state), just whether the
        # pick collides with the phase-preference vocabulary at all.
        "is_lifecycle_top1": 1.0 if row["names"][0] in _LIFECYCLE_NAMES else 0.0,
    }


SIGNAL_NAMES = (
    "norm_gap_to_mean",
    "z_of_top1",
    "abs_top1",
    "words",
    "naive_margin",
    "is_lifecycle_top1",
)

# is_lifecycle_top1 is a 0/1 flag, not a continuous score to sweep a
# percentile threshold over — CV still picks the *direction* (escalate when
# it IS a lifecycle name vs. escalate when it ISN'T) out-of-fold, at a single
# operating point.
BOOLEAN_SIGNALS = {"is_lifecycle_top1"}


# ---------------------------------------------------------------------------
# Framings
# ---------------------------------------------------------------------------

# name -> (positive-label predicate, human description of the positive class,
#          real-world "call the judge" action given a predict-positive verdict)
#
# Framing A predicts "needs the judge" directly, so predict-positive IS
# escalate. Framing B predicts "will decline" — predicting positive there
# means "skip is free", so the real escalate action is the *negation* of the
# prediction. Conflating the two (treating "predicted positive" as "escalate"
# for both) silently inverts every downstream cost number for framing B.
FRAMINGS: dict[str, tuple[Callable[[str], bool], str, Callable[[bool], bool]]] = {
    "A_top1_wrong": (
        lambda lab: lab in ("disagreed", "declined"),
        "needs judge (disagreed or declined)",
        lambda pred: pred,
    ),
    "B_will_decline": (
        lambda lab: lab == "declined",
        "judge declines (skip is free)",
        lambda pred: not pred,
    ),
}


# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    pos = q * (len(vs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    frac = pos - lo
    return vs[lo] + (vs[hi] - vs[lo]) * frac


def _stratified_folds(y: list[int], k: int, seed: int) -> list[list[int]]:
    rng = random.Random(seed)
    pos = [i for i, v in enumerate(y) if v]
    neg = [i for i, v in enumerate(y) if not v]
    rng.shuffle(pos)
    rng.shuffle(neg)
    folds: list[list[int]] = [[] for _ in range(k)]
    for i, idx in enumerate(pos):
        folds[i % k].append(idx)
    for i, idx in enumerate(neg):
        folds[i % k].append(idx)
    return folds


def _direction_and_threshold(
    train_vals: list[float], train_y: list[int], q: float, boolean: bool
) -> tuple[str, float]:
    """direction='low' means escalate when value <= threshold."""
    pos_vals = [v for v, y in zip(train_vals, train_y) if y]
    neg_vals = [v for v, y in zip(train_vals, train_y) if not y]
    mean_pos = statistics.fmean(pos_vals) if pos_vals else 0.0
    mean_neg = statistics.fmean(neg_vals) if neg_vals else 0.0
    direction = "low" if mean_pos <= mean_neg else "high"
    if boolean:
        # Escalate on the value (0/1) that is more common in the positive
        # class: 'high' -> escalate when value == 1, 'low' -> value == 0.
        return direction, 0.5
    if direction == "low":
        thresh = _quantile(train_vals, q)
    else:
        thresh = _quantile(train_vals, 1 - q)
    return direction, thresh


def _escalate(value: float, direction: str, thresh: float) -> bool:
    return value <= thresh if direction == "low" else value >= thresh


def cv_predict(
    values: list[float],
    y: list[int],
    q: float,
    k: int,
    seed: int,
    boolean: bool,
) -> list[bool]:
    """Out-of-fold escalate/skip decision for every row, threshold and
    direction picked from the other folds only."""
    folds = _stratified_folds(y, k, seed)
    pred: list[bool | None] = [None] * len(values)
    for fi in range(k):
        test_idx = set(folds[fi])
        train_idx = [i for i in range(len(values)) if i not in test_idx]
        train_vals = [values[i] for i in train_idx]
        train_y = [y[i] for i in train_idx]
        direction, thresh = _direction_and_threshold(train_vals, train_y, q, boolean)
        for i in test_idx:
            pred[i] = _escalate(values[i], direction, thresh)
    assert all(p is not None for p in pred)
    return pred  # type: ignore[return-value]


def in_sample_predict(
    values: list[float], y: list[int], q: float, boolean: bool
) -> list[bool]:
    direction, thresh = _direction_and_threshold(values, y, q, boolean)
    return [_escalate(v, direction, thresh) for v in values]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _confusion(pred: list[bool], y: list[int]) -> tuple[int, int, int, int]:
    tp = sum(1 for p, t in zip(pred, y) if p and t)
    fp = sum(1 for p, t in zip(pred, y) if p and not t)
    fn = sum(1 for p, t in zip(pred, y) if not p and t)
    tn = sum(1 for p, t in zip(pred, y) if not p and not t)
    return tp, fp, fn, tn


def _prf(tp: int, fp: int, fn: int, n: int) -> tuple[float, float, float]:
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    rate = (tp + fp) / n if n else 0.0
    return prec, rec, rate


def _universal_metrics(escalate: list[bool], labels: list[str]) -> dict:
    """Real-world cost/benefit of an escalate/skip decision, defined the same
    way regardless of which framing produced it — the 3-way label (agreed /
    disagreed / declined) is what actually determines harm, not the internal
    target a rule was thresholded against.

    - harmed:  skipped a row that was disagreed or declined — a worse pick,
      or a false recommendation, ships instead of nothing/a correction.
    - disagreement_discard: the 'disagreed' subset of harmed, tracked
      separately because it is the costliest miss (a concrete wrong pick,
      not just an absent one) and is what the brief asks be stated as the
      rule's cost regardless of framing.
    - wasted: escalated a row that didn't need it (agreed or declined) —
      cost/latency paid for nothing, but no quality loss.
    """
    n = len(labels)
    esc_rate = sum(escalate) / n if n else 0.0
    harmed = sum(
        1
        for e, lab in zip(escalate, labels)
        if not e and lab in ("disagreed", "declined")
    )
    disagreed_idx = [i for i, lab in enumerate(labels) if lab == "disagreed"]
    disc = sum(1 for i in disagreed_idx if not escalate[i])
    wasted = sum(
        1 for e, lab in zip(escalate, labels) if e and lab in ("agreed", "declined")
    )
    return {
        "esc_rate": esc_rate,
        "harmed": harmed,
        "disagree_discard": disc,
        "disagree_total": len(disagreed_idx),
        "wasted": wasted,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260730)
    ap.add_argument(
        "--step",
        type=int,
        default=10,
        help="Percentile step for the threshold sweep (10 -> deciles).",
    )
    args = ap.parse_args()

    rows = [
        json.loads(line)
        for line in Path(args.corpus).open(encoding="utf-8")
        if line.strip()
    ]
    labelled = [(r, label3(r)) for r in rows]
    usable = [(r, lab) for r, lab in labelled if lab != "skip"]
    labels = [lab for _, lab in usable]
    n = len(usable)

    n_declined = sum(1 for lab in labels if lab == "declined")
    n_agreed = sum(1 for lab in labels if lab == "agreed")
    n_disagreed = sum(1 for lab in labels if lab == "disagreed")
    print(
        f"corpus {len(rows)} rows -> usable {n} "
        f"(dropped {len(labelled) - n} where the judge itself failed)"
    )
    print(
        f"  declined={n_declined}  agreed={n_agreed}  disagreed={n_disagreed}"
        f"  needs_judge(disagreed+declined)={n_disagreed + n_declined}"
    )
    print()

    all_signals = [_signals(r) for r, _ in usable]
    for name in SIGNAL_NAMES:
        vals = sorted(s[name] for s in all_signals)
        p10 = _quantile(vals, 0.10)
        p50 = _quantile(vals, 0.50)
        p90 = _quantile(vals, 0.90)
        print(f"--- {name}  p10={p10:.4f} p50={p50:.4f} p90={p90:.4f}")
    print()

    for framing_name, (positive, desc, escalate_from_pred) in FRAMINGS.items():
        print("=" * 100)
        print(f"FRAMING {framing_name}: target = {desc}")
        y = [1 if positive(lab) else 0 for lab in labels]
        n_pos = sum(y)
        print(f"  target positives={n_pos}  target negatives={n - n_pos}  n={n}")
        print(
            "  columns below are REAL escalate/skip actions (esc_rate = fraction "
            "of prompts actually sent to the judge), not raw target predictions —\n"
            "  for framing B a 'predict decline' verdict means the real action is "
            "SKIP, so it is negated before these are computed."
        )
        print()

        for name in SIGNAL_NAMES:
            values = [s[name] for s in all_signals]
            boolean = name in BOOLEAN_SIGNALS
            qs = (
                [0.5]
                if boolean
                else [q / 100 for q in range(args.step, 100, args.step)]
            )

            print(f"--- {framing_name} / {name}")
            header = (
                f"{'q':>5} {'thresh':>9} | "
                f"{'esc%':>6} {'harmed':>7} {'wasted':>7} {'disagree_discard':>17} | "
                f"{'tgt_prec':>8} {'tgt_rec':>7} (cv)  | "
                f"{'tgt_prec':>8} {'tgt_rec':>7} (in-sample)"
            )
            print(header)
            for q in qs:
                cv_pred = cv_predict(values, y, q, args.folds, args.seed, boolean)
                tp, fp, fn, _tn = _confusion(cv_pred, y)
                prec, rec, _rate = _prf(tp, fp, fn, n)
                cv_escalate = [escalate_from_pred(p) for p in cv_pred]
                m = _universal_metrics(cv_escalate, labels)

                is_pred = in_sample_predict(values, y, q, boolean)
                itp, ifp, ifn, _itn = _confusion(is_pred, y)
                iprec, irec, _irate = _prf(itp, ifp, ifn, n)

                # threshold reported is the in-sample one (single number to
                # anchor the row to); CV folds each pick their own.
                _, thresh = _direction_and_threshold(values, y, q, boolean)
                qlabel = "bool" if boolean else f"{int(q * 100)}%"
                print(
                    f"{qlabel:>5} {thresh:>9.4f} | "
                    f"{m['esc_rate']:>5.1%} {m['harmed']:>7} {m['wasted']:>7} "
                    f"{m['disagree_discard']:>10}/{m['disagree_total']:<6} | "
                    f"{prec:>8.3f} {rec:>7.3f}       | "
                    f"{iprec:>8.3f} {irec:>7.3f}"
                )
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
