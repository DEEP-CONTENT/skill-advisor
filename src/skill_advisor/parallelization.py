"""Judge-based parallelizability detector.

Given a list of task titles (collected from the model's TodoWrite/TaskCreate
calls during a planning turn), asks `claude -p` whether the tasks can be
executed independently in parallel (subagents / worktrees) or must run
sequentially. Mirrors the hallucination-guarded JSON parsing of judge.py.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

from .config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParallelizationResult:
    parallel: bool
    groups: list[list[int]]  # indices into the input tasks list
    reason: str


_PROMPT_TEMPLATE = """You are an execution planner. Given a list of tasks a coding agent is about to perform, decide whether they can be executed in parallel (by dispatching subagents into separate git worktrees) or must run sequentially.

Return ONLY JSON matching this schema (no prose, no code fences):

{{"parallel": <bool>, "groups": [[<task_index>, ...], ...], "reason": "<<=20 words>"}}

Rules:
- Task indices are 0-based into the list below.
- `parallel: true` only when task groups touch disjoint files/modules and have no data dependency on each other.
- `groups` partitions task indices into sets that can each be worked on in its own worktree. One group = one subagent. A single-task group is fine.
- `parallel: false` means run sequentially in the listed order; return `groups: []` in that case.
- When in doubt, prefer sequential.

Tasks:
{tasks}
"""


def _render_tasks(tasks: list[str]) -> str:
    return "\n".join(f"{i}. {t}" for i, t in enumerate(tasks))


def detect(
    tasks: list[str],
    config: Config,
    *,
    timeout: float | None = None,
) -> ParallelizationResult | None:
    """Ask the judge whether `tasks` are parallelizable. Returns None on any failure."""
    if not tasks:
        return None
    if shutil.which("claude") is None:
        log.warning("claude CLI not on PATH; parallelization.detect skipped")
        return None

    judge_prompt = _PROMPT_TEMPLATE.format(tasks=_render_tasks(tasks))
    budget = timeout if timeout is not None else config.parallelization.judge_timeout_seconds

    try:
        completed = subprocess.run(
            ["claude", "-p", "--model", config.matcher.model, "--output-format", "json"],
            input=judge_prompt,
            capture_output=True,
            text=True,
            timeout=budget,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.info("parallelization judge timed out after %.2fs", budget)
        return None
    except (OSError, ValueError) as exc:
        log.warning("parallelization judge subprocess failed: %s", exc)
        return None

    if completed.returncode != 0:
        log.warning(
            "parallelization judge exit %s: %s",
            completed.returncode,
            completed.stderr[:200],
        )
        return None

    return _parse_reply(completed.stdout, len(tasks))


def _parse_reply(stdout: str, n_tasks: int) -> ParallelizationResult | None:
    stdout = stdout.strip()
    if not stdout:
        return None
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("parallelization judge non-JSON envelope: %r", stdout[:200])
        return None

    inner_text = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(inner_text, str):
        log.warning("parallelization judge envelope missing string 'result'")
        return None

    inner = _extract_json_object(inner_text)
    if inner is None:
        return None

    parallel = bool(inner.get("parallel", False))
    reason = str(inner.get("reason", "")).strip()
    raw_groups = inner.get("groups") if parallel else []
    if not isinstance(raw_groups, list):
        raw_groups = []

    groups: list[list[int]] = []
    for g in raw_groups:
        if not isinstance(g, list):
            continue
        # Hallucination guard: only keep indices within range, dedupe, preserve order.
        cleaned: list[int] = []
        seen: set[int] = set()
        for idx in g:
            if not isinstance(idx, int):
                continue
            if 0 <= idx < n_tasks and idx not in seen:
                cleaned.append(idx)
                seen.add(idx)
        if cleaned:
            groups.append(cleaned)

    # If parallel=true but all groups were dropped by the guard, downgrade to sequential.
    if parallel and not groups:
        parallel = False

    return ParallelizationResult(parallel=parallel, groups=groups, reason=reason)


def _extract_json_object(text: str) -> dict | None:
    """Best-effort extraction of the first JSON object in a model reply."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
