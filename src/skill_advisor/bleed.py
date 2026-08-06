"""Time-bleeder analysis — what skills and tools actually cost.

Pure functions over telemetry event rows. No I/O, no config, no clock, and no
imports from the rest of the package: the caller loads events and passes a
threshold. That is what makes the idle rule unit-testable without hooks.

The threshold is applied HERE, at read time, rather than baked into the
recorded data — so changing it re-interprets all history instead of only
affecting turns recorded afterwards.

The load-bearing limitation, stated rather than engineered around: PostToolUse
reports only the END of each tool call, so one long span cannot be told apart
between "the user was away from the keyboard" and "that test run really did
take four minutes". Idle is therefore never hidden. A skill whose time is
mostly idle reads as UNMEASURED, not as fast or slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(text: object) -> datetime | None:
    """Public: the CLI needs it too, for windowing under `--since`."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class Turn:
    session: str | None
    start: datetime
    stop: datetime
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    spans: list[tuple[str, int]] | None = None

    @property
    def duration_s(self) -> float:
        return (self.stop - self.start).total_seconds()


@dataclass
class Attribution:
    per_tool: dict[str, int]
    idle_ms: int
    idle_gaps: int


def attribute(spans: Sequence[tuple[str, int]], threshold_ms: int) -> Attribution:
    """Charge each span to its tool, capping anything over the threshold.

    Cap-and-spill rather than all-or-nothing: a genuinely slow tool should not
    drop to zero attributed time merely for crossing the line.
    """
    per_tool: dict[str, int] = {}
    idle_ms = 0
    idle_gaps = 0
    for name, raw in spans:
        delta = max(0, int(raw))  # clock adjustments must not go negative
        if delta > threshold_ms:
            idle_ms += delta - threshold_ms
            idle_gaps += 1
            delta = threshold_ms
        per_tool[name] = per_tool.get(name, 0) + delta
    return Attribution(per_tool=per_tool, idle_ms=idle_ms, idle_gaps=idle_gaps)


def pair_turns(events: Iterable[dict]) -> tuple[list[Turn], int, int]:
    """Pair each prompt with the next stop in the same session.

    Returns (turns, unpaired_prompt_count, malformed_row_count).

    A prompt with no following stop is normal, not an error: `run_stop` returns
    early when the turn used no tools, so a purely conversational turn writes no
    stop event at all. It is counted so the report can disclose it instead of
    silently shrinking the corpus.

    A row whose timestamp will not parse is data damage, and is counted
    SEPARATELY rather than folded into `unpaired` — conflating the two would
    make a corrupted log read as heavy conversational use.
    """
    by_session: dict[str | None, list[dict]] = {}
    for row in events:
        # Rows written before the `kind` key exists are prompts.
        kind = row.get("kind") or "prompt"
        if kind not in ("prompt", "stop"):
            continue
        by_session.setdefault(row.get("session_sha256"), []).append(row)

    turns: list[Turn] = []
    unpaired = 0
    malformed = 0
    for session, rows in by_session.items():
        rows.sort(key=lambda r: str(r.get("ts") or ""))
        pending: datetime | None = None
        for row in rows:
            ts = parse_ts(row.get("ts"))
            if ts is None:
                malformed += 1
                continue
            if (row.get("kind") or "prompt") == "prompt":
                if pending is not None:
                    unpaired += 1
                pending = ts
                continue
            if pending is None:
                continue  # a stop with no prompt before it
            raw_spans = row.get("tool_spans")
            spans = (
                [(str(n), int(ms)) for n, ms in raw_spans]
                if isinstance(raw_spans, list) and raw_spans
                else None
            )
            turns.append(
                Turn(
                    session=session,
                    start=pending,
                    stop=ts,
                    skills=[str(s) for s in (row.get("skills") or [])],
                    tools=[str(t) for t in (row.get("tools") or [])],
                    spans=spans,
                )
            )
            pending = None
        if pending is not None:
            unpaired += 1
    return turns, unpaired, malformed


def _p50(values: Sequence[float]) -> float:
    """Upper median. Deterministic on even counts and never interpolates a
    value that no observation actually had."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


@dataclass
class SkillStat:
    name: str
    n: int
    p50_turn_s: float
    attributed_ms: int
    idle_ms: int
    turns_with_idle: int


@dataclass
class ToolStat:
    name: str
    calls: int
    p50_ms: int
    attributed_ms: int


def span_coverage(turns: Sequence[Turn]) -> tuple[int, int]:
    """(turns carrying per-tool spans, total turns). Printed always: turn-level
    and per-tool tables are different populations and must never look like one."""
    return sum(1 for t in turns if t.spans), len(turns)


def skill_stats(
    turns: Sequence[Turn], *, threshold_ms: int, min_n: int
) -> tuple[list[SkillStat], int]:
    """Rank skills by total attributed time. Returns (ranked, below_min_n_count).

    A turn invoking two skills counts fully toward both — this is attribution,
    not a division of blame, and splitting it would understate every skill that
    is usually combined with another.
    """
    durations: dict[str, list[float]] = {}
    attributed: dict[str, int] = {}
    idle: dict[str, int] = {}
    idle_turns: dict[str, int] = {}

    for turn in turns:
        a = attribute(turn.spans or [], threshold_ms)
        charged = sum(a.per_tool.values())
        for name in set(turn.skills):
            durations.setdefault(name, []).append(turn.duration_s)
            attributed[name] = attributed.get(name, 0) + charged
            idle[name] = idle.get(name, 0) + a.idle_ms
            if a.idle_gaps:
                idle_turns[name] = idle_turns.get(name, 0) + 1

    ranked = [
        SkillStat(
            name=name,
            n=len(durations[name]),
            p50_turn_s=_p50(durations[name]),
            attributed_ms=attributed.get(name, 0),
            idle_ms=idle.get(name, 0),
            turns_with_idle=idle_turns.get(name, 0),
        )
        for name in durations
    ]
    below = sum(1 for s in ranked if s.n < min_n)
    kept = [s for s in ranked if s.n >= min_n]
    kept.sort(key=lambda s: (-s.attributed_ms, s.name))
    return kept, below


def tool_stats(turns: Sequence[Turn], *, threshold_ms: int) -> list[ToolStat]:
    """Rank tools by total attributed time across every turn that carries spans.

    Both columns are post-idle-rule: `p50_ms` is the median of CAPPED spans,
    not raw ones, so one row never mixes two meanings. A tool whose calls are
    all idle-contaminated therefore shows `p50_ms == threshold_ms`, which reads
    correctly as "we stopped counting here" rather than as a real duration.
    """
    samples: dict[str, list[float]] = {}
    attributed: dict[str, int] = {}
    for turn in turns:
        for name, raw in turn.spans or []:
            charged = attribute([(name, raw)], threshold_ms).per_tool[name]
            samples.setdefault(name, []).append(float(charged))
            attributed[name] = attributed.get(name, 0) + charged

    stats = [
        ToolStat(
            name=name,
            calls=len(samples[name]),
            p50_ms=int(_p50(samples[name])),
            attributed_ms=attributed.get(name, 0),
        )
        for name in samples
    ]
    stats.sort(key=lambda s: (-s.attributed_ms, s.name))
    return stats
