"""Format picks (and lifecycle state, if any) into the additionalContext string."""
from __future__ import annotations

from . import lifecycle
from .matcher import PickResult, ResolvedPick


_HEADER = (
    "<skill-advisor>\n"
    "Based on the user's prompt, the following catalog entries are strong matches. "
    "Before responding, invoke the top match via the Skill tool (or the Agent tool "
    "for subagents) unless clearly inappropriate.\n"
)
_FOOTER = "</skill-advisor>"


def _format_picks(picks: list[ResolvedPick]) -> list[str]:
    lines = []
    for idx, p in enumerate(picks, start=1):
        reason = p.reason or "matches the prompt"
        # Must be the string the Skill tool actually accepts (the directory
        # name, namespaced `<plugin>:<dir>` for plugin skills) — NOT
        # `entry.name` (the frontmatter `name:`), which can differ for both
        # plugin skills (always) and any skill whose frontmatter `name:`
        # diverges from its directory (7 on the author's machine, e.g.
        # `xlsx` declaring `name: xlsx-official`). Emitting `entry.name` here
        # recommends a string the model cannot invoke. See
        # `cli._print_proposal_lines`, which already uses this idiom.
        invoke_name = p.entry.invoke_name or p.entry.name
        lines.append(f"  {idx}. {invoke_name} ({p.entry.kind}) — {reason}")
    return lines


def _lifecycle_banner(state: lifecycle.LifecycleState) -> list[str]:
    phases = [
        lifecycle.PLANNING,
        lifecycle.PARALLELIZATION_CHECK,
        lifecycle.IMPLEMENTATION,
        lifecycle.REVIEW,
        lifecycle.CORRECTION,
        lifecycle.COMPLETE,
    ]
    rendered = []
    for p in phases:
        rendered.append(f"[{p}]" if p == state.phase else p)
    chain = " → ".join(rendered)
    cycle_info = ""
    if state.cycles.get(lifecycle.CORRECTION, 0):
        # Rendered without the cap here — the cap lives in config and may vary per user.
        cycle_info = f" (correction cycle {state.cycles[lifecycle.CORRECTION]})"
    next_hint = lifecycle.phase_next_description(state.phase)

    lines = [
        f"Lifecycle: {chain}{cycle_info}",
        f"Original task: {state.original_prompt[:200]}",
        f"After this step completes, the next phase is: {next_hint}.",
    ]
    if state.phase == lifecycle.REVIEW:
        lines.append(
            "Reviewer: surface any issues explicitly in the response so the user can "
            "reply 'fix these' to advance to correction, or 'looks good' to complete."
        )
    if state.phase == lifecycle.PARALLELIZATION_CHECK:
        lines.append(
            "Parallelization: if the picks above include dispatching-parallel-agents "
            "or using-git-worktrees, run the task groups as parallel subagents in "
            "isolated worktrees; rebase each worktree onto the working branch before "
            "fast-forward merging, then remove the worktrees."
        )
    if state.phase == lifecycle.COMPLETE:
        lines.append("This lifecycle is complete. Commit / open PR as appropriate.")
    return lines


def format(result: "PickResult") -> str:
    lines = [_HEADER.rstrip()]
    if result.state is not None and result.state.phase != lifecycle.CANCELLED:
        lines.append("")
        lines.extend(_lifecycle_banner(result.state))
    lines.append("")
    lines.extend(_format_picks(result.picks))
    lines.append(_FOOTER)
    return "\n".join(lines)
