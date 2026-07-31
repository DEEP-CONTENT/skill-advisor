#!/usr/bin/env python3
"""Build the escalation calibration corpus.

Runs the real embedding path and the real judge over prompts sampled from
`~/.claude/history.jsonl`, and records enough per prompt to evaluate candidate
confidence rules offline. Slow by design — ~12 s of judge per prompt, so 200
prompts is roughly 40 minutes. Run it once, in the background.

The judge is given its own `--judge-timeout-seconds` (default 60s), passed
straight through to the subprocess call rather than sourced from the live
config's `matcher.budget_seconds` (15.0s in production). Production sizes that
budget to lose the SIGALRM race against the hook's own deadline on purpose;
the collector has no such race and a matching cutoff would silently convert
roughly half of all verdicts into timeouts (measured: 131/250 at the
production budget) instead of the "what would the judge decide" answer this
corpus exists to capture. The loaded config is never mutated or replaced —
only an explicit `timeout=` argument overrides it.

Hook contamination: a nested `claude -p` inherits the CALLING session's hooks.
On a dev machine with a Stop hook that demands a code review
(`code-review-gate.sh`), that hook fires inside the judge's nested session,
turning its single-turn structured-JSON reply into a multi-turn conversational
one — the judge never gets to answer, and the parser correctly rejects the
reply as unparseable. `num_turns` in the `claude -p --output-format json`
envelope is the discriminator: every clean verdict is 1 turn; every observed
contaminated reply was >1. This is intermittent (the hook fires on its own
conditions), not constant, and is an artifact of collecting on a dev machine
with extra ambient hooks — not a property of the judge itself. The collector
detects it via `num_turns` and retries once (bounded, so a pathological
prompt can't loop); if still contaminated after retrying, the row is recorded
as `judge_failure: "hook_contaminated"` rather than folded into a genuine
`unparseable`, and the contamination rate is written to stderr at the end so
it doesn't require re-deriving from the corpus.

Writes no prompt text. The hash is for dedup only.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skill_advisor import index as index_mod  # noqa: E402
from skill_advisor import judge, triage  # noqa: E402
from skill_advisor.catalog import CatalogEntry  # noqa: E402
from skill_advisor.config import Config, load as load_config  # noqa: E402

# Distinct from judge.FAILURE_* — this is a collector-only diagnosis (a
# property of how the data was collected, not of the judge), so it does not
# belong in judge.py's own failure taxonomy.
FAILURE_HOOK_CONTAMINATED = "hook_contaminated"

# "Retry once" per the observed diagnosis, capped so a prompt that reliably
# triggers the hook (rather than hitting it by chance) can't loop forever.
_DEFAULT_MAX_JUDGE_ATTEMPTS = 2


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


@dataclasses.dataclass
class _RawCall:
    """One `claude -p` subprocess call, with the envelope fields judge.rank()
    doesn't expose (num_turns, total_cost_usd) needed to detect hook
    contamination and to record judge cost for Task 6."""

    verdict: judge.JudgeResult
    num_turns: int | None
    cost_usd: float | None
    elapsed_ms: int


def _rank_raw(
    prompt: str,
    candidates: list[CatalogEntry],
    config: Config,
    timeout: float,
) -> _RawCall:
    """Mirrors judge.rank()'s subprocess call exactly (same template, same
    reply parser — imported from judge.py, not reimplemented, so there is no
    behavioral drift), but also surfaces `num_turns` / `total_cost_usd` from
    the envelope. Duplicating the subprocess invocation is unavoidable: the
    public judge.rank() API discards the envelope after extracting `result`.
    """
    t0 = time.monotonic()
    if not candidates:
        return _RawCall(
            judge.JudgeResult(picks=[], failure=judge.FAILURE_NO_CANDIDATES),
            None,
            None,
            0,
        )
    if shutil.which("claude") is None:
        return _RawCall(
            judge.JudgeResult(picks=[], failure=judge.FAILURE_CLI_MISSING),
            None,
            None,
            0,
        )

    judge_prompt = judge._JUDGE_TEMPLATE.format(
        prompt=prompt.strip(),
        candidates=judge._render_candidates(candidates),
    )
    try:
        completed = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                config.matcher.model,
                "--output-format",
                "json",
            ],
            input=judge_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return _RawCall(
            judge.JudgeResult(picks=[], failure=judge.FAILURE_TIMEOUT),
            None,
            None,
            elapsed_ms,
        )
    except (OSError, ValueError):
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return _RawCall(
            judge.JudgeResult(picks=[], failure=judge.FAILURE_SUBPROCESS),
            None,
            None,
            elapsed_ms,
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    num_turns = None
    cost_usd = None
    if completed.returncode == 0:
        try:
            envelope = json.loads(completed.stdout.strip())
            if isinstance(envelope, dict):
                num_turns = envelope.get("num_turns")
                cost_usd = envelope.get("total_cost_usd")
        except json.JSONDecodeError:
            pass

    if completed.returncode != 0:
        verdict = judge.JudgeResult(picks=[], failure=judge.FAILURE_EXIT)
    else:
        parsed = judge._parse_judge_reply(completed.stdout, candidates)
        verdict = (
            parsed
            if parsed is not None
            else judge.JudgeResult(picks=[], failure=judge.FAILURE_UNPARSEABLE)
        )
    return _RawCall(verdict, num_turns, cost_usd, elapsed_ms)


def judge_with_contamination_guard(
    prompt: str,
    candidates: list[CatalogEntry],
    config: Config,
    timeout: float,
    max_attempts: int = _DEFAULT_MAX_JUDGE_ATTEMPTS,
) -> dict:
    """Calls the judge, retrying once (bounded by `max_attempts`) whenever
    `num_turns > 1` signals a hook-hijacked nested session. Returns the
    verdict to record plus bookkeeping (attempts made, how many were
    contaminated, and total cost across all attempts — the wasted
    contaminated call still cost real money and belongs in the total).
    """
    attempts: list[_RawCall] = []
    for _ in range(max_attempts):
        call = _rank_raw(prompt, candidates, config, timeout)
        attempts.append(call)
        if call.num_turns is None or call.num_turns <= 1:
            break

    final = attempts[-1]
    contaminated_attempts = sum(
        1 for c in attempts if c.num_turns is not None and c.num_turns > 1
    )
    costs = [c.cost_usd for c in attempts if c.cost_usd is not None]
    total_cost = round(sum(costs), 6) if costs else None

    if final.num_turns is not None and final.num_turns > 1:
        # Exhausted retries and still contaminated — don't trust picks that
        # may have parsed by coincidence out of a hijacked session's reply.
        verdict = judge.JudgeResult(picks=[], failure=FAILURE_HOOK_CONTAMINATED)
    else:
        verdict = final.verdict

    return {
        "verdict": verdict,
        "num_turns": final.num_turns,
        "cost_usd": total_cost,
        "judge_ms": final.elapsed_ms,
        "attempts": len(attempts),
        "contaminated_attempts": contaminated_attempts,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=str(Path.home() / ".claude" / "history.jsonl"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=250)
    ap.add_argument("--seed", type=int, default=20260730)
    ap.add_argument(
        "--judge-timeout-seconds",
        type=float,
        default=60.0,
        help=(
            "Subprocess timeout for each judge call, independent of the live "
            "config's matcher.budget_seconds (which is sized for the hook's "
            "SIGALRM race, not for calibration data collection)."
        ),
    )
    ap.add_argument(
        "--max-judge-attempts",
        type=int,
        default=_DEFAULT_MAX_JUDGE_ATTEMPTS,
        help="Bound on retries for hook-contaminated (num_turns > 1) replies.",
    )
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
    rows_written = 0
    rows_contaminated_final = 0
    rows_retried = 0
    with out.open("w", encoding="utf-8") as fh:
        for i, text in enumerate(sample, 1):
            ranked = index_mod.top_k(text, idx, cfg.matcher.max_candidates)
            if not ranked:
                continue
            candidates = [e for e, _ in ranked]
            result = judge_with_contamination_guard(
                text,
                candidates,
                cfg,
                args.judge_timeout_seconds,
                args.max_judge_attempts,
            )
            verdict = result["verdict"]
            rows_written += 1
            if result["attempts"] > 1:
                rows_retried += 1
            if verdict.failure == FAILURE_HOOK_CONTAMINATED:
                rows_contaminated_final += 1
            fh.write(
                json.dumps(
                    {
                        "prompt_sha256": hashlib.sha256(text.encode()).hexdigest()[:16],
                        "words": len(text.split()),
                        "scores": [round(s, 6) for _, s in ranked],
                        # `e.name` is the frontmatter identity, and is exactly what
                        # judge.rank() both shows the model and matches replies
                        # against — this is the space judge_picks lives in, so
                        # names[0] in judge_picks stays a valid same-space
                        # comparison. It is NOT always the invocable identity
                        # (Skill tool argument); 61 live catalog entries diverge
                        # (e.g. "docx-official" -> invoke_name "docx"). Keep both:
                        # `names` for judge-space comparisons, `invoke_names` for
                        # anything that joins this corpus back against the catalog.
                        "names": [e.name for e in candidates],
                        "invoke_names": [(e.invoke_name or e.name) for e in candidates],
                        "judge_picks": [p.name for p in verdict.picks],
                        "judge_declined": verdict.failure is None and not verdict.picks,
                        "judge_failure": verdict.failure,
                        "judge_ms": result["judge_ms"],
                        "judge_attempts": result["attempts"],
                        "judge_contaminated_attempts": result["contaminated_attempts"],
                        "num_turns": result["num_turns"],
                        "cost_usd": result["cost_usd"],
                    }
                )
                + "\n"
            )
            fh.flush()
            tag = "DECLINE" if not verdict.picks else verdict.picks[0].name
            if result["attempts"] > 1:
                tag += f" (retried {result['attempts'] - 1}x, contaminated={result['contaminated_attempts']})"
            print(
                f"[{i}/{len(sample)}] {result['judge_ms']:>6} ms  {tag}",
                flush=True,
            )

    if rows_written:
        print(
            f"\ncontamination: {rows_retried}/{rows_written} rows needed a retry, "
            f"{rows_contaminated_final}/{rows_written} still contaminated after "
            f"exhausting {args.max_judge_attempts} attempt(s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
