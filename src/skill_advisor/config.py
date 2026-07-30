"""User-facing config (`~/.config/skill-advisor/config.toml`) with defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class MatcherConfig:
    model: str = "claude-haiku-4-5-20251001"
    max_candidates: int = 15
    max_picks: int = 3
    # Whole-hook budget. hook.py arms SIGALRM at int(budget_seconds + 0.5) around
    # the entire matcher; judge.py gives its subprocess budget_seconds - 0.5, so
    # the judge always loses the race and matcher.py's embedding fallback is
    # reachable. Measured 2026-07-29: the judge answers usefully under 15 s or
    # not at all (the >=24 s band produced 14 picks against 432 nothings), so a
    # tight budget forfeits almost nothing.
    budget_seconds: float = 8.0
    # When False (default) the embedding top-K is used directly as picks —
    # ~50-200 ms per prompt. When True, `claude -p` re-ranks the shortlist
    # for higher precision at the cost of 5-15 s of session-startup overhead.
    use_judge: bool = False
    # Minimum cosine score to surface a pick in embedding-only mode.
    min_embedding_score: float = 0.35


@dataclass(frozen=True)
class CatalogConfig:
    extra_roots: tuple[str, ...] = ()
    exclude_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriageConfig:
    skip_if_shorter_than: int = 6
    extra_skip_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutoAdvanceConfig:
    # Master toggle. When false, the Stop hook never advances the state machine.
    enabled: bool = False
    # PLANNING → IMPLEMENTATION when a Plan subagent completed in the last turn.
    on_plan_subagent_done: bool = True
    # IMPLEMENTATION → REVIEW when the last turn ended with Edit/Write/NotebookEdit.
    on_edit_stop: bool = True


@dataclass(frozen=True)
class LifecycleConfig:
    # Master toggle. When false, the advisor never starts or advances a lifecycle.
    enabled: bool = True
    # Max correction cycles before the advisor forces `complete`.
    max_correction_cycles: int = 3
    # Extra regexes (case-insensitive) that should be treated as lifecycle triggers.
    extra_trigger_patterns: tuple[str, ...] = ()
    # Extra regexes that suppress triggering, even when a built-in verb matches.
    extra_disable_patterns: tuple[str, ...] = ()
    # REPLACE the built-in preference list for a phase. Entries are ("kind", "name") tuples.
    # Empty dict keeps built-in defaults for every phase.
    phase_candidates: dict[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict
    )
    # PREPEND to the built-in list for a phase (team-specific picks bubble to the top).
    phase_additions: dict[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict
    )
    # Auto-advance driven by Stop/PostToolUse hooks. Off by default.
    auto_advance: AutoAdvanceConfig = field(default_factory=AutoAdvanceConfig)


@dataclass(frozen=True)
class TelemetryConfig:
    # Master toggle. Off by default — when off, no events.jsonl is ever written.
    events_enabled: bool = False
    # Informational; retention is enforced manually via `skill-advisor report --purge-older-than`.
    retain_days: int = 90
    # Hash salt for prompt + session id SHAs. Empty → auto-generated on first write,
    # stored at ~/.cache/skill-advisor/telemetry.salt with mode 0600.
    prompt_hash_salt: str = ""


@dataclass(frozen=True)
class ParallelizationConfig:
    # Master toggle. When false, PostToolUse never records TodoWrite payloads
    # for parallelization purposes and the Stop hook never transitions into
    # parallelization_check. Off by default. Enabling requires matcher.budget_seconds >=
    # judge_timeout_seconds + 3.
    enabled: bool = False
    # Minimum TodoWrite task count to consider the check worthwhile.
    min_tasks: int = 3
    # Must stay <= matcher.budget_seconds - 3. The detector makes its own
    # `claude -p` call from inside the hook's SIGALRM window, so a timeout
    # longer than the budget means the alarm kills the whole hook — silent, no
    # picks at all — instead of the detector merely giving up. At 5.0 under an
    # 8 s budget the detector will often time out and return None; that
    # degrades gracefully (no parallel picks, lifecycle still advances) and is
    # the accepted trade for the budget cut.
    judge_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class EffortConfig:
    # Master toggle. When false, nothing in this feature runs: no classification,
    # no status line registration, no nudge, no write-back. Off by default so
    # existing installs are untouched until the user opts in.
    enabled: bool = False
    # Register a `statusLine` command in claudeskill-settings.json.
    statusline: bool = True
    # Emit a systemMessage when the recommendation disagrees with observed effort.
    nudge: bool = True
    # Allow occasional writes of `effortLevel` into claudeskill-settings.json.
    write_back: bool = True
    # Consecutive qualifying sessions of disagreement before a write happens.
    write_back_after_sessions: int = 5
    # Sessions to suppress write-back for after the user manually overrides.
    veto_cooldown_sessions: int = 10
    # Recommend `ultracode` when the parallelization detector says yes.
    # Never persisted — Claude Code treats ultracode as session-only by design.
    ultracode_nudge: bool = True


@dataclass(frozen=True)
class Config:
    matcher: MatcherConfig = field(default_factory=MatcherConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    triage: TriageConfig = field(default_factory=TriageConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    parallelization: ParallelizationConfig = field(
        default_factory=ParallelizationConfig
    )
    effort: EffortConfig = field(default_factory=EffortConfig)


def _as_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


_VALID_KINDS = {"skill", "subagent", "command"}


def _parse_kind_name_entries(value) -> tuple[tuple[str, str], ...]:
    """Parse a TOML list of 'kind:name' strings into ((kind, name), ...) tuples."""
    if not value:
        return ()
    out: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, str):
            continue
        if ":" not in item:
            continue
        kind, _, name = item.partition(":")
        kind = kind.strip()
        name = name.strip()
        if not kind or not name or kind not in _VALID_KINDS:
            continue
        out.append((kind, name))
    return tuple(out)


def _parse_auto_advance(value) -> AutoAdvanceConfig:
    if not isinstance(value, dict):
        return AutoAdvanceConfig()
    return AutoAdvanceConfig(
        enabled=bool(value.get("enabled", AutoAdvanceConfig.enabled)),
        on_plan_subagent_done=bool(
            value.get("on_plan_subagent_done", AutoAdvanceConfig.on_plan_subagent_done)
        ),
        on_edit_stop=bool(value.get("on_edit_stop", AutoAdvanceConfig.on_edit_stop)),
    )


def _parse_phase_map(value) -> dict[str, tuple[tuple[str, str], ...]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, tuple[tuple[str, str], ...]] = {}
    for phase, entries in value.items():
        parsed = _parse_kind_name_entries(entries)
        if parsed:
            out[str(phase)] = parsed
    return out


def load(path: Path | None = None) -> Config:
    target = path or paths.config_file()
    if not target.is_file():
        return Config()
    raw = tomllib.loads(target.read_text(encoding="utf-8"))

    matcher = raw.get("matcher", {}) or {}
    catalog = raw.get("catalog", {}) or {}
    triage = raw.get("triage", {}) or {}
    lifecycle = raw.get("lifecycle", {}) or {}
    telemetry = raw.get("telemetry", {}) or {}
    parallelization = raw.get("parallelization", {}) or {}
    effort = raw.get("effort", {}) or {}

    return Config(
        matcher=MatcherConfig(
            model=str(matcher.get("model", MatcherConfig.model)),
            max_candidates=int(
                matcher.get("max_candidates", MatcherConfig.max_candidates)
            ),
            max_picks=int(matcher.get("max_picks", MatcherConfig.max_picks)),
            budget_seconds=float(
                matcher.get("budget_seconds", MatcherConfig.budget_seconds)
            ),
            use_judge=bool(matcher.get("use_judge", MatcherConfig.use_judge)),
            min_embedding_score=float(
                matcher.get("min_embedding_score", MatcherConfig.min_embedding_score)
            ),
        ),
        catalog=CatalogConfig(
            extra_roots=_as_tuple(catalog.get("extra_roots")),
            exclude_names=_as_tuple(catalog.get("exclude_names")),
        ),
        triage=TriageConfig(
            skip_if_shorter_than=int(
                triage.get("skip_if_shorter_than", TriageConfig.skip_if_shorter_than)
            ),
            extra_skip_patterns=_as_tuple(triage.get("extra_skip_patterns")),
        ),
        lifecycle=LifecycleConfig(
            enabled=bool(lifecycle.get("enabled", LifecycleConfig.enabled)),
            max_correction_cycles=int(
                lifecycle.get(
                    "max_correction_cycles", LifecycleConfig.max_correction_cycles
                )
            ),
            extra_trigger_patterns=_as_tuple(lifecycle.get("extra_trigger_patterns")),
            extra_disable_patterns=_as_tuple(lifecycle.get("extra_disable_patterns")),
            phase_candidates=_parse_phase_map(lifecycle.get("phase_candidates")),
            phase_additions=_parse_phase_map(lifecycle.get("phase_additions")),
            auto_advance=_parse_auto_advance(lifecycle.get("auto_advance")),
        ),
        telemetry=TelemetryConfig(
            events_enabled=bool(
                telemetry.get("events_enabled", TelemetryConfig.events_enabled)
            ),
            retain_days=int(telemetry.get("retain_days", TelemetryConfig.retain_days)),
            prompt_hash_salt=str(
                telemetry.get("prompt_hash_salt", TelemetryConfig.prompt_hash_salt)
            ),
        ),
        parallelization=ParallelizationConfig(
            enabled=bool(parallelization.get("enabled", ParallelizationConfig.enabled)),
            min_tasks=int(
                parallelization.get("min_tasks", ParallelizationConfig.min_tasks)
            ),
            judge_timeout_seconds=float(
                parallelization.get(
                    "judge_timeout_seconds",
                    ParallelizationConfig.judge_timeout_seconds,
                )
            ),
        ),
        effort=EffortConfig(
            enabled=bool(effort.get("enabled", EffortConfig.enabled)),
            statusline=bool(effort.get("statusline", EffortConfig.statusline)),
            nudge=bool(effort.get("nudge", EffortConfig.nudge)),
            write_back=bool(effort.get("write_back", EffortConfig.write_back)),
            write_back_after_sessions=int(
                effort.get(
                    "write_back_after_sessions", EffortConfig.write_back_after_sessions
                )
            ),
            veto_cooldown_sessions=int(
                effort.get(
                    "veto_cooldown_sessions", EffortConfig.veto_cooldown_sessions
                )
            ),
            ultracode_nudge=bool(
                effort.get("ultracode_nudge", EffortConfig.ultracode_nudge)
            ),
        ),
    )
