"""Lifecycle state machine — drives planning → implementation → review → (correction)* → complete.

Session state is persisted per Claude Code session under
`~/.cache/skill-advisor/sessions/<session_id>.json`. The hook consults this state
on every UserPromptSubmit and advances phases based on continuation signals from
the user's next prompt.

Design choices:
- Trigger detection is opt-in-by-heuristic: prompts with build/implement/refactor
  verbs start a lifecycle; the magic prefix `[no-lifecycle]` disables it.
- Advancement is explicit: the user typing "go" / "continue" / "next" pushes the
  state machine forward. Off-topic prompts cancel the lifecycle silently.
- Max 3 correction cycles before the advisor forces `complete`.
- Phase → skill preferences are ordered lists; the first few that actually exist
  in the user's catalog become the picks. If none match, we fall back to the
  normal embedding matcher so the advisor keeps recommending *something*.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from . import paths
from .catalog import CatalogEntry
from .config import LifecycleConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase state machine
# ---------------------------------------------------------------------------

PLANNING = "planning"
PARALLELIZATION_CHECK = "parallelization_check"
IMPLEMENTATION = "implementation"
REVIEW = "review"
CORRECTION = "correction"
COMPLETE = "complete"
CANCELLED = "cancelled"

ACTIVE_PHASES = (PLANNING, PARALLELIZATION_CHECK, IMPLEMENTATION, REVIEW, CORRECTION)
TERMINAL_PHASES = (COMPLETE, CANCELLED)

MAX_CORRECTION_CYCLES = 3


def next_phase(current: str, had_issues: bool = False) -> str:
    """Advance the state machine. `had_issues` only matters when leaving REVIEW."""
    if current == PLANNING:
        return IMPLEMENTATION
    if current == PARALLELIZATION_CHECK:
        return IMPLEMENTATION
    if current == IMPLEMENTATION:
        return REVIEW
    if current == REVIEW:
        return CORRECTION if had_issues else COMPLETE
    if current == CORRECTION:
        return REVIEW
    return current


# ---------------------------------------------------------------------------
# Phase → skill preferences
# ---------------------------------------------------------------------------

# Ordered lists. Each entry is (kind, name). We take the first N that exist in
# the catalog. Names are intentionally generous so most installs have fallbacks.
PHASE_CANDIDATES: dict[str, list[tuple[str, str]]] = {
    PLANNING: [
        ("subagent", "Plan"),
        ("skill", "superpowers:write-plan"),
        ("skill", "writing-plans"),
        ("skill", "plan-writing"),
        ("skill", "brainstorming"),
        ("skill", "superpowers:brainstorming"),
        ("skill", "concise-planning"),
        ("skill", "planning-with-files"),
        ("skill", "feature-design-assistant"),
    ],
    PARALLELIZATION_CHECK: [
        ("skill", "superpowers:dispatching-parallel-agents"),
        ("skill", "dispatching-parallel-agents"),
        ("skill", "superpowers:using-git-worktrees"),
        ("skill", "using-git-worktrees"),
        ("skill", "parallel-agents"),
        ("skill", "superpowers:subagent-driven-development"),
        ("skill", "subagent-driven-development"),
    ],
    IMPLEMENTATION: [
        ("skill", "superpowers:execute-plan"),
        ("skill", "plan-executor"),
        ("skill", "executing-plans"),
        ("skill", "superpowers:executing-plans"),
        ("skill", "subagent-driven-development"),
        ("skill", "backend-dev-guidelines"),
        ("skill", "frontend-dev-guidelines"),
        ("skill", "test-driven-development"),
        ("skill", "superpowers:test-driven-development"),
    ],
    REVIEW: [
        ("subagent", "feature-dev:code-reviewer"),
        ("subagent", "superpowers:code-reviewer"),
        ("skill", "code-reviewer"),
        ("skill", "code-review"),
        ("skill", "code-review-excellence"),
        ("skill", "code-review-checklist"),
        ("skill", "review"),
        ("skill", "superpowers:requesting-code-review"),
    ],
    CORRECTION: [
        ("skill", "fix-review"),
        ("skill", "address-github-comments"),
        ("skill", "iterate-pr"),
        ("skill", "debugger"),
        ("skill", "error-detective"),
        ("skill", "superpowers:systematic-debugging"),
        ("skill", "systematic-debugging"),
    ],
    COMPLETE: [
        ("skill", "commit"),
        ("skill", "create-pr"),
        ("skill", "pr-creator"),
        ("skill", "finishing-a-development-branch"),
        ("skill", "superpowers:finishing-a-development-branch"),
        ("skill", "git-pushing"),
    ],
}


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

_NO_LIFECYCLE_PREFIX = re.compile(r"^\s*\[no[-_ ]lifecycle\]\s*", re.IGNORECASE)

# Matches intent verbs that typically open a multi-step task.
_TRIGGER_PATTERN = re.compile(
    r"\b("
    r"build|implement|create\s+(a|an|the)|add\s+(a|an|the|feature|support)|"
    r"refactor|migrate|rewrite|port|scaffold|wire\s+up|design\s+(a|an|the)|"
    r"develop|ship|introduce|extend\s+(the|this)|set\s+up"
    r")\b",
    re.IGNORECASE,
)
# Things that LOOK like lifecycle triggers but really aren't meaningful tasks.
_TRIGGER_ANTIPATTERN = re.compile(
    r"\b(just\s+|quick\s+|one[- ]off|trivial|small\s+fix|fix\s+(a\s+)?typo|tweak|nudge)\b",
    re.IGNORECASE,
)

_CONTINUE_PATTERN = re.compile(
    r"^\s*("
    r"go|go\s+ahead|next|next\s+step|continue|proceed|yes|yep|ok|okay|"
    r"do\s+it|let'?s\s+(go|continue|do\s+it)|ship\s+it|ready"
    r")\b",
    re.IGNORECASE,
)
_CANCEL_PATTERN = re.compile(
    r"\b(cancel|abort|stop|nevermind|never\s*mind|new\s+topic|different\s+thing|forget\s+(it|that))\b",
    re.IGNORECASE,
)
_COMPLETE_PATTERN = re.compile(
    r"\b(done|complete[d]?|finished|looks\s+good|lgtm|all\s+good|that'?s\s+it|ship\s+it)\b",
    re.IGNORECASE,
)
# Issues found during review — heuristic tell for CORRECTION vs COMPLETE next.
_ISSUES_PATTERN = re.compile(
    r"\b(issues?|bugs?|problems?|broken|failing|fix\s+these|address|correct)\b",
    re.IGNORECASE,
)


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            log.info("ignoring invalid lifecycle regex: %r", pattern)
            continue
    return False


def is_trigger(prompt: str, config: LifecycleConfig | None = None) -> bool:
    cfg = config or LifecycleConfig()
    if not cfg.enabled:
        return False
    text = prompt.strip()
    if _NO_LIFECYCLE_PREFIX.match(text):
        return False
    if _TRIGGER_ANTIPATTERN.search(text):
        return False
    if _matches_any(text, cfg.extra_disable_patterns):
        return False
    if len(text.split()) < 4:
        return False
    if _TRIGGER_PATTERN.search(text):
        return True
    return _matches_any(text, cfg.extra_trigger_patterns)


def strip_markers(prompt: str) -> str:
    return _NO_LIFECYCLE_PREFIX.sub("", prompt.strip())


def is_continue_signal(prompt: str) -> bool:
    return bool(_CONTINUE_PATTERN.match(prompt.strip()))


def is_cancel_signal(prompt: str) -> bool:
    return bool(_CANCEL_PATTERN.search(prompt))


def is_complete_signal(prompt: str) -> bool:
    return bool(_COMPLETE_PATTERN.search(prompt))


def mentions_issues(prompt: str) -> bool:
    return bool(_ISSUES_PATTERN.search(prompt))


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------


@dataclass
class LifecycleState:
    session_id: str
    original_prompt: str
    phase: str
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cycles: dict[str, int] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    pending_todos: list[str] = field(default_factory=list)

    def is_active(self) -> bool:
        return self.phase in ACTIVE_PHASES

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "LifecycleState":
        # Tolerate older on-disk state files that pre-date pending_todos.
        known = {
            "session_id", "original_prompt", "phase", "started_at", "updated_at",
            "cycles", "history", "pending_todos",
        }
        filtered = {k: v for k, v in data.items() if k in known}
        filtered.setdefault("pending_todos", [])
        return cls(**filtered)


def load(session_id: str) -> LifecycleState | None:
    path = paths.session_file(session_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LifecycleState.from_json(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def save(state: LifecycleState) -> None:
    paths.ensure_dirs()
    state.updated_at = time.time()
    paths.session_file(state.session_id).write_text(
        json.dumps(state.to_json(), indent=2), encoding="utf-8"
    )


def delete(session_id: str) -> bool:
    path = paths.session_file(session_id)
    if path.is_file():
        path.unlink()
        return True
    return False


def list_sessions() -> list[LifecycleState]:
    d = paths.sessions_dir()
    if not d.is_dir():
        return []
    out: list[LifecycleState] = []
    for path in sorted(d.glob("*.json")):
        try:
            out.append(LifecycleState.from_json(json.loads(path.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def start(session_id: str, prompt: str) -> LifecycleState:
    state = LifecycleState(
        session_id=session_id,
        original_prompt=strip_markers(prompt),
        phase=PLANNING,
    )
    state.history.append({"phase": PLANNING, "at": state.started_at, "note": "lifecycle started"})
    save(state)
    return state


def advance(
    state: LifecycleState,
    *,
    had_issues: bool = False,
    note: str = "",
    source: str = "user",
    config: LifecycleConfig | None = None,
) -> LifecycleState:
    cfg = config or LifecycleConfig()
    current = state.phase
    max_cycles = cfg.max_correction_cycles
    # Cap correction cycles: after max_correction_cycles corrections, force COMPLETE.
    if current == REVIEW and had_issues and state.cycles.get(CORRECTION, 0) >= max_cycles:
        state.phase = COMPLETE
        state.history.append(
            {"phase": COMPLETE, "at": time.time(), "note": "max correction cycles reached", "source": source}
        )
        save(state)
        return state

    new_phase = next_phase(current, had_issues=had_issues)
    state.phase = new_phase
    if new_phase == CORRECTION:
        state.cycles[CORRECTION] = state.cycles.get(CORRECTION, 0) + 1
    elif new_phase == REVIEW:
        state.cycles[REVIEW] = state.cycles.get(REVIEW, 0) + 1
    state.history.append({"phase": new_phase, "at": time.time(), "note": note, "source": source})
    save(state)
    return state


def enter_parallelization_check(
    state: LifecycleState,
    todos: list[str],
    *,
    note: str = "",
    source: str = "auto",
) -> LifecycleState:
    """Transition planning → parallelization_check with captured todos."""
    state.phase = PARALLELIZATION_CHECK
    state.pending_todos = [str(t).strip() for t in todos if str(t).strip()]
    state.history.append(
        {
            "phase": PARALLELIZATION_CHECK,
            "at": time.time(),
            "note": note or f"entered with {len(state.pending_todos)} todos",
            "source": source,
        }
    )
    save(state)
    return state


def cancel(state: LifecycleState, note: str = "cancelled") -> LifecycleState:
    state.phase = CANCELLED
    state.history.append({"phase": CANCELLED, "at": time.time(), "note": note})
    save(state)
    return state


def force_complete(state: LifecycleState, note: str = "user marked complete") -> LifecycleState:
    state.phase = COMPLETE
    state.history.append({"phase": COMPLETE, "at": time.time(), "note": note})
    save(state)
    return state


# ---------------------------------------------------------------------------
# Phase → picks resolution
# ---------------------------------------------------------------------------


def _resolve_phase_prefs(phase: str, config: LifecycleConfig | None) -> list[tuple[str, str]]:
    """Merge config overrides/additions with the built-in phase preference list."""
    builtin = list(PHASE_CANDIDATES.get(phase, []))
    if config is None:
        return builtin
    if phase in config.phase_candidates:
        # Full replacement semantics.
        return list(config.phase_candidates[phase])
    additions = list(config.phase_additions.get(phase, ()))
    return additions + builtin


def pick_candidates_for_phase(
    phase: str,
    catalog: Iterable[CatalogEntry],
    limit: int = 3,
    config: LifecycleConfig | None = None,
    *,
    pickable_only: bool = True,
) -> list[CatalogEntry]:
    """Pick preferred catalog entries for a phase, honoring config overrides.

    Disabled skills are skipped rather than consuming a slot, so the ordered
    preference list falls through to the next enabled alternative — which is
    what the list was always for. Measured 2026-07-29: without this filter the
    PLANNING list stopped at `plan-writing` (disabled) and never reached
    `brainstorming`, producing 490 un-invocable recommendations.

    Looked up by invocable identity (`invoke_name or name`), matching how
    `PHASE_CANDIDATES` itself spells plugin entries (namespaced,
    `<plugin>:<dir>`, e.g. "superpowers:brainstorming") and matching
    `catalog._accept()`'s own dedup key. Keying on `entry.name` — the
    frontmatter `name:` — instead broke both directions: a namespaced
    preference could never match, because no catalog entry's frontmatter
    `name:` is ever namespaced; and a *bare* preference (e.g.
    "brainstorming") collided across namespaces whenever a user skill and a
    plugin skill share a frontmatter name, since plugin-cache roots are
    scanned after skills/ — the dict comprehension's last-wins semantics
    silently resolved the bare preference to the plugin entry, whose
    `enabled` is always True regardless of what the user actually muted.
    `seen` is tracked by the same invocable identity, not `entry.name`, for
    the same reason: two DIFFERENT entries (a user skill and a plugin skill)
    can share a frontmatter name while being distinct, independently
    pickable catalog entries.

    Deliberately NOT cross-matching a bare preference to a namespaced entry
    or vice versa — `PHASE_CANDIDATES` already lists both spellings as
    separate, explicitly-ordered preferences wherever that fallback matters
    (see PLANNING's "brainstorming" / "superpowers:brainstorming" pair), so
    implicit cross-matching would just reintroduce the same collision this
    fixes.
    """
    prefs = _resolve_phase_prefs(phase, config)
    by_invocable: dict[str, CatalogEntry] = {
        (e.invoke_name or e.name): e for e in catalog
    }
    picks: list[CatalogEntry] = []
    seen: set[str] = set()
    for kind, name in prefs:
        entry = by_invocable.get(name)
        if entry is None or entry.kind != kind:
            continue
        invocable = entry.invoke_name or entry.name
        if invocable in seen:
            continue
        if pickable_only and not entry.enabled:
            continue
        picks.append(entry)
        seen.add(invocable)
        if len(picks) >= limit:
            break
    return picks


# ---------------------------------------------------------------------------
# Per-turn state — consumed by the Stop hook to decide auto-advance.
# ---------------------------------------------------------------------------


# Tools that clearly indicate the model did implementation work this turn.
# See design decision 3: we use a positive allow-list rather than a readonly
# deny-list so ambiguous tools (Task, WebFetch, …) don't trigger phase changes.
MUTATING_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit", "Bash"})


@dataclass
class TurnState:
    session_id: str
    turn_started_at: float = field(default_factory=time.time)
    tool_names: list[str] = field(default_factory=list)
    subagents_invoked: list[str] = field(default_factory=list)
    skills_invoked: list[str] = field(default_factory=list)
    todo_write: dict | None = None  # {"count": int, "titles": list[str]} or None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "TurnState":
        return cls(
            session_id=str(data.get("session_id") or ""),
            turn_started_at=float(data.get("turn_started_at") or time.time()),
            tool_names=list(data.get("tool_names") or []),
            subagents_invoked=list(data.get("subagents_invoked") or []),
            skills_invoked=list(data.get("skills_invoked") or []),
            todo_write=data.get("todo_write") if isinstance(data.get("todo_write"), dict) else None,
        )

    def has_mutating_tool(self) -> bool:
        return any(t in MUTATING_TOOLS for t in self.tool_names)


def load_turn(session_id: str) -> TurnState | None:
    path = paths.turn_file(session_id)
    if not path.is_file():
        return None
    try:
        return TurnState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def save_turn(turn: TurnState) -> None:
    paths.ensure_dirs()
    paths.turn_file(turn.session_id).write_text(
        json.dumps(turn.to_json(), indent=2), encoding="utf-8"
    )


def delete_turn(session_id: str) -> bool:
    path = paths.turn_file(session_id)
    if path.is_file():
        path.unlink()
        return True
    return False


def record_tool(
    session_id: str,
    tool_name: str,
    *,
    subagent_type: str | None = None,
    skill_name: str | None = None,
) -> TurnState:
    """Accumulate tool usage into the current turn's state file."""
    turn = load_turn(session_id) or TurnState(session_id=session_id)
    turn.tool_names.append(tool_name)
    # Claude Code's `Task` tool exposes the chosen subagent via tool_input.subagent_type.
    if tool_name == "Task" and subagent_type:
        turn.subagents_invoked.append(subagent_type)
    # Claude Code's `Skill` tool exposes the invoked skill via tool_input.skill.
    if tool_name == "Skill" and skill_name:
        turn.skills_invoked.append(skill_name)
    save_turn(turn)
    return turn


def record_todo_write(session_id: str, todos: list[str]) -> TurnState:
    """Persist the TodoWrite task titles onto the current turn's state.

    Last-write-wins semantics: if the model issues multiple TodoWrites in one
    turn (e.g. update a previous list), only the final snapshot matters for
    the parallelization decision.
    """
    turn = load_turn(session_id) or TurnState(session_id=session_id)
    # Defensive copy + cap length so oversize payloads don't blow up the JSON.
    clean = [str(t).strip() for t in todos if str(t).strip()][:50]
    turn.todo_write = {"count": len(clean), "titles": clean}
    save_turn(turn)
    return turn


_TURN_TITLES_CAP = 50


def append_todo_title(session_id: str, title: str) -> TurnState:
    """Append a single task title to the current turn's todo_write list.

    Used by the per-call `TaskCreate` tool (the post-TodoWrite Claude Code
    task system, where each call adds exactly one task). Creates the
    todo_write dict on first call; caps total titles at 50 to mirror
    `record_todo_write` and keep turn-state JSON small.
    """
    turn = load_turn(session_id) or TurnState(session_id=session_id)
    cleaned = title.strip()
    if not cleaned:
        return turn
    existing = turn.todo_write if isinstance(turn.todo_write, dict) else None
    titles: list[str] = list(existing.get("titles") or []) if existing else []
    if len(titles) >= _TURN_TITLES_CAP:
        return turn
    titles.append(cleaned)
    turn.todo_write = {"count": len(titles), "titles": titles}
    save_turn(turn)
    return turn


def phase_next_description(phase: str) -> str:
    """Human-readable 'what's coming next' hint for the additionalContext."""
    if phase == PLANNING:
        return "implementation (when the user says 'go' / 'next')"
    if phase == PARALLELIZATION_CHECK:
        return "implementation (parallel execution recommended where possible)"
    if phase == IMPLEMENTATION:
        return "review"
    if phase == REVIEW:
        return "correction (if review flags issues) or complete"
    if phase == CORRECTION:
        return "review (re-run on the applied fixes)"
    if phase == COMPLETE:
        return "—"
    return "—"
