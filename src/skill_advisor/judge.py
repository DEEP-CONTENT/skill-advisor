"""`claude -p` judge — ranks the embedding prefilter's shortlist.

Uses `--output-format json` to get a structured envelope. Validates the model's
inner JSON against a tiny schema and rejects names that weren't in the candidate
list (hallucination guard — known `claude -p` rough edge).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

from .catalog import CatalogEntry
from .config import Config
from . import effort as effort_mod

log = logging.getLogger(__name__)

# Why the judge produced no verdict. `None` means it ran and answered —
# including answering "nothing fits", which is a verdict, not a failure.
FAILURE_NO_CANDIDATES = "no_candidates"
FAILURE_CLI_MISSING = "cli_missing"
FAILURE_TIMEOUT = "timeout"
FAILURE_SUBPROCESS = "subprocess_error"
FAILURE_EXIT = "exit_nonzero"
FAILURE_UNPARSEABLE = "unparseable"
# A nested `claude -p` inherits the CALLING session's hooks. When one fires
# (observed: a Stop hook demanding a code review), it forces extra turns and
# `result` becomes the nested session's reply to the hook instead of the
# judge's verdict. That reply is not a parse failure — the session was
# hijacked — so without this it gets misdiagnosed as FAILURE_UNPARSEABLE,
# which implies a malformed reply or a parser gap when neither is true.
# `num_turns` in the envelope is the discriminator: every clean verdict
# observed is num_turns == 1, every contaminated reply is > 1, with zero
# overlap across the 250-row calibration corpus (see
# tools/build_calibration_corpus.py and task-2-report.md).
FAILURE_HOOK_CONTAMINATED = "hook_contaminated"

# Not produced by rank() itself — set by hook.py when the whole-hook SIGALRM
# fires before the judge (or anything else downstream of it) returns a
# verdict. Defined here, alongside the judge's own FAILURE_* values, so every
# string that can land in the `judge_failure` telemetry field lives in one
# place and is guaranteed not to collide.
FAILURE_BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True)
class Pick:
    name: str
    reason: str


@dataclass(frozen=True)
class JudgeResult:
    picks: list[Pick]
    effort: str | None = None
    # None ⟺ the judge ran and returned a usable verdict. Callers use this to
    # tell a 24-second timeout apart from a deliberate "nothing fits" — the
    # first should fall back to the embedding ranking, the second must not.
    failure: str | None = None

    @property
    def ran(self) -> bool:
        return self.failure is None


_JUDGE_TEMPLATE = """You are a skill router for Claude Code. Given the user's message and a candidate list, pick 0-3 catalog entries that best apply. Return ONLY JSON matching this schema (no prose, no code fences):

{{"picks": [{{"name": "<exact catalog name>", "reason": "<<=12 words>"}}], "skip": <bool>, "effort": "<low|medium|high|xhigh|ultracode>"}}

Rules:
- Use exact names from the candidate list. Do not invent or rename.
- If nothing is a strong fit, return {{"picks": [], "skip": true}}.
- Prefer skills over subagents when both match; prefer subagents for heavy exploration/planning work.
- `effort` is how much reasoning depth this task warrants: `low` for trivial edits and lookups, `medium` for routine changes, `high` for multi-file work, `xhigh` for design or debugging that needs sustained reasoning, `ultracode` only when the task decomposes into several independent sub-tasks that could run in parallel.

User message:
<<<
{prompt}
>>>

Candidates:
{candidates}
"""


def _render_candidates(candidates: list[CatalogEntry]) -> str:
    lines = []
    for e in candidates:
        # Clip description so the prompt doesn't explode on verbose skills.
        desc = e.description.replace("\n", " ").strip()
        if len(desc) > 220:
            desc = desc[:217] + "..."
        lines.append(f"- {e.name} ({e.kind}) — {desc}")
    return "\n".join(lines)


def rank(
    prompt: str,
    candidates: list[CatalogEntry],
    config: Config,
    timeout: float | None = None,
) -> JudgeResult:
    """Rank `candidates` with `claude -p`.

    Never returns None. A result with `failure is None` means the judge ran and
    answered; `picks == []` in that case is a deliberate decline and callers
    must respect it. A non-None `failure` means no verdict was obtained and the
    caller should fall back to whatever it already has.
    """
    if not candidates:
        return JudgeResult(picks=[], failure=FAILURE_NO_CANDIDATES)
    if shutil.which("claude") is None:
        log.warning("claude CLI not on PATH; judge skipped")
        return JudgeResult(picks=[], failure=FAILURE_CLI_MISSING)

    judge_prompt = _JUDGE_TEMPLATE.format(
        prompt=prompt.strip(),
        candidates=_render_candidates(candidates),
    )
    budget = (
        timeout
        if timeout is not None
        else max(config.matcher.budget_seconds - 0.5, 0.5)
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
            timeout=budget,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.info("judge timed out after %.2fs", budget)
        return JudgeResult(picks=[], failure=FAILURE_TIMEOUT)
    except (OSError, ValueError) as exc:
        log.warning("judge subprocess failed: %s", exc)
        return JudgeResult(picks=[], failure=FAILURE_SUBPROCESS)

    if completed.returncode != 0:
        log.warning("judge exit %s: %s", completed.returncode, completed.stderr[:200])
        return JudgeResult(picks=[], failure=FAILURE_EXIT)

    parsed = _parse_judge_reply(completed.stdout, candidates)
    if parsed is None:
        return JudgeResult(picks=[], failure=FAILURE_UNPARSEABLE)
    return parsed


def _parse_judge_reply(
    stdout: str, candidates: list[CatalogEntry]
) -> JudgeResult | None:
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("judge returned non-JSON envelope: %r", stdout[:200])
        return None

    # Check for hook contamination before ever looking at `result` — a
    # hijacked session isn't a parse failure, so it must not be classified by
    # what the (irrelevant) reply text happens to contain. See
    # FAILURE_HOOK_CONTAMINATED above.
    num_turns = envelope.get("num_turns") if isinstance(envelope, dict) else None
    if isinstance(num_turns, (int, float)) and num_turns > 1:
        log.warning(
            "judge session hijacked by an inherited hook (num_turns=%s); "
            "not a parse failure",
            num_turns,
        )
        return JudgeResult(picks=[], failure=FAILURE_HOOK_CONTAMINATED)

    # `claude -p --output-format json` puts the assistant message in `result`.
    inner_text = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(inner_text, str):
        log.warning("judge envelope missing string 'result'")
        return None

    inner = _extract_json_object(inner_text)
    if inner is None:
        return None

    picks_raw = inner.get("picks")
    if not isinstance(picks_raw, list):
        return None

    raw_effort = inner.get("effort")
    parsed_effort = raw_effort if raw_effort in effort_mod.RECOMMENDABLE else None
    if raw_effort is not None and parsed_effort is None:
        log.info("rejected out-of-enum effort: %r", raw_effort)

    skip = bool(inner.get("skip", False))
    if skip and not picks_raw:
        return JudgeResult(picks=[], effort=parsed_effort)

    valid_names = {e.name for e in candidates}
    picks: list[Pick] = []
    for item in picks_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        reason = item.get("reason", "")
        if not isinstance(name, str) or not isinstance(reason, str):
            continue
        if name not in valid_names:
            log.info("rejected hallucinated pick: %r", name)
            continue
        picks.append(Pick(name=name.strip(), reason=reason.strip()))
    return JudgeResult(picks=picks, effort=parsed_effort)


def _extract_json_object(text: str) -> dict | None:
    """Best-effort extraction of the first JSON object in a model reply."""
    text = text.strip()
    # Strip code fences if the model wrapped the JSON.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: find the first {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
