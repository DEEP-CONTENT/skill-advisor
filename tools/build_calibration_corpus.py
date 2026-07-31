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
    print(
        f"{len(pool)} unique non-triaged prompts available; sampling {len(sample)}",
        flush=True,
    )

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
            fh.write(
                json.dumps(
                    {
                        "prompt_sha256": hashlib.sha256(text.encode()).hexdigest()[:16],
                        "words": len(text.split()),
                        "scores": [round(s, 6) for _, s in ranked],
                        "names": [e.name for e, _ in ranked],
                        "judge_picks": [p.name for p in verdict.picks],
                        "judge_declined": verdict.failure is None and not verdict.picks,
                        "judge_failure": verdict.failure,
                        "judge_ms": judge_ms,
                    }
                )
                + "\n"
            )
            fh.flush()
            print(
                f"[{i}/{len(sample)}] {judge_ms:>6} ms  "
                f"{'DECLINE' if not verdict.picks else verdict.picks[0].name}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
