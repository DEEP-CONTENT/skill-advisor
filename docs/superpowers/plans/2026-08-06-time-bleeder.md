# Time Bleeder (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record how long each tool call takes inside a turn, and ship a `skill-advisor bleed` report that ranks skills and tools by the time they actually cost — with idle time flagged rather than charged.

**Architecture:** One new field (`tool_marks`) appended in a turn-state write that already happens on every `PostToolUse`; spans flushed into the `stop` event at turn end as raw deltas; a dependency-free pure module (`bleed.py`) that applies the idle threshold at *read* time; a thin CLI. Nothing touches `UserPromptSubmit` or any hot path.

**Tech Stack:** Python 3.11+, `uv`, pytest. No new dependencies — `bleed.py` is stdlib only.

## Global Constraints

- **Hook paths stay silent on error.** Nothing added here may raise out of `run_posttooluse()` or `run_stop()`. Both already wrap their work in `try/except`; new code goes inside those guards.
- **Record raw, classify at read time.** No idle threshold may be written into `events.jsonl`. The stored form commits to nothing; `bleed.py` interprets. Inverting this is the decision most likely to be regretted.
- **Tool names only.** `tool_input` contents must never reach an event — no file paths, no shell commands, no arguments. Pinned by a test, not by reviewer vigilance.
- **`bleed.py` imports nothing from `skill_advisor`.** Pure stdlib, no I/O, no config, no clock. The caller loads events and passes a threshold. This is what makes the idle rule unit-testable without hooks.
- **No silent truncation or silent drops.** Every count the report excludes (below-min-n skills, unparseable rows, `--limit` overflow) is printed as a count.
- Test suite baseline before Task 1: **589 passing in under 4 s**. No test may spawn `claude` or embed with the real model.
- Module style: docstring, then `from __future__ import annotations`, then imports.

---

## Facts established before planning — do not re-derive

| Fact | Where | Why it matters |
|---|---|---|
| `TurnState.turn_started_at: float` already exists | `lifecycle.py:472` | It is the anchor for the first span. Set at `TurnState(...)` construction — i.e. **first tool call**, not prompt submission. |
| `record_tool()` already loads/mutates/saves the turn file on every call | `lifecycle.py:521-538` | Adding a mark is one more field in an existing write. No new process. |
| `from_json` already tolerates missing keys via `data.get(...) or []` | `lifecycle.py:483-490` | Old turn files degrade to span-less, not to a crash. |
| `run_stop()` returns early when `turn is None`, **before** `record_stop` | `hook.py:460-461`, `hook.py:466-474` | A turn with zero tool calls emits **no stop event at all**. This is why 963 of 5,311 prompts are unpaired. It is expected, not a bug. |
| `telemetry.record_stop(*, session_id, tools, subagents, skills, config)` | `telemetry.py:131-165` | The signature Task 2 extends. |
| `telemetry.iter_events(*, cutoff=None, path=None) -> Iterator[dict]` | `telemetry.py:180` | The reader the CLI uses. |
| `telemetry.parse_duration(str) -> timedelta`, raises `ValueError` | used at `cli.py:556` | Parses `--since` values like `30d`. |
| Legacy event rows have **no `kind` key** (838 of them) | measured | `bleed.py` must default a missing `kind` to `"prompt"` or it silently drops the oldest history. |
| Event `ts` format is `"%Y-%m-%dT%H:%M:%SZ"` (second resolution) | `telemetry.py:152` | Sub-second precision is unavailable at turn level. Spans are ms because they come from `time.time()`, not from `ts`. |
| `SCHEMA_VERSION = 1` | `telemetry.py:34` | **Not bumped by this work** — new keys are additive and readers use `.get()`. |

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/skill_advisor/lifecycle.py` | Gains `TurnState.tool_marks` and one append in `record_tool`. | Modify |
| `src/skill_advisor/telemetry.py` | `record_stop` accepts and writes `tool_spans` + `span_anchor`. | Modify |
| `src/skill_advisor/hook.py` | `run_stop` computes deltas from marks and passes them to `record_stop`. | Modify |
| `src/skill_advisor/bleed.py` | **New.** Pure aggregation: turn pairing, the idle rule, skill/tool stats. Stdlib only. | Create |
| `src/skill_advisor/cli.py` | **New.** `bleed` subcommand: load events, call `bleed.py`, print. | Modify |
| `tests/test_lifecycle.py` | Mark collection, `from_json` back-compat, desync guard. | Modify |
| `tests/test_hook.py` | **The wiring tests** — marks collected through `run_posttooluse`, spans written through `run_stop`. | Modify |
| `tests/test_bleed.py` | **New.** The idle rule, pairing, stats, min-n. The bulk of the coverage. | Create |
| `tests/test_bleed_cli.py` | **New.** Output discipline: coverage line, below-min-n line, `+N more`, skipped rows. | Create |
| `README.md` | Document `bleed`, the idle rule, and what it cannot measure. | Modify |

---

### Task 1: Collect a timestamp per tool call

**Files:**
- Modify: `src/skill_advisor/lifecycle.py` (dataclass at 468-490, `record_tool` at 521-538)
- Test: `tests/test_lifecycle.py`, `tests/test_hook.py`

**Interfaces:**
- Produces: `TurnState.tool_marks: list[float]` — epoch seconds, one per tool call, index-parallel to `tool_names`. Consumed by Task 2.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lifecycle.py`:

```python
def test_record_tool_appends_a_mark_parallel_to_the_name(isolated_paths):
    from skill_advisor import lifecycle

    lifecycle.record_tool("sess-marks", "Read")
    lifecycle.record_tool("sess-marks", "Bash")
    turn = lifecycle.load_turn("sess-marks")

    assert turn.tool_names == ["Read", "Bash"]
    assert len(turn.tool_marks) == 2
    assert turn.tool_marks[1] >= turn.tool_marks[0]
    assert turn.tool_marks[0] >= turn.turn_started_at


def test_from_json_without_tool_marks_yields_empty_list(isolated_paths):
    """A turn file written before this feature must load, not crash."""
    from skill_advisor.lifecycle import TurnState

    turn = TurnState.from_json({
        "session_id": "old",
        "turn_started_at": 1000.0,
        "tool_names": ["Read", "Bash"],
    })

    assert turn.tool_names == ["Read", "Bash"]
    assert turn.tool_marks == []


def test_tool_marks_survive_a_save_load_round_trip(isolated_paths):
    from skill_advisor import lifecycle

    lifecycle.record_tool("sess-rt", "Grep")
    first = lifecycle.load_turn("sess-rt").tool_marks
    lifecycle.record_tool("sess-rt", "Edit")
    second = lifecycle.load_turn("sess-rt").tool_marks

    assert second[0] == first[0]  # the earlier mark was not rewritten
    assert len(second) == 2
```

Append to `tests/test_hook.py` — **this is the load-bearing one**:

```python
def test_posttooluse_wires_the_mark_into_turn_state(monkeypatch, isolated_paths):
    """Goes RED if record_tool stops appending the mark.

    The aggregator can be perfectly unit-tested and still report nothing if this
    call is missing, and an empty report reads as "no time bleeders found"
    rather than as a bug. Drive the real hook, not lifecycle directly.
    """
    import io
    import json as _json
    from skill_advisor import hook, lifecycle

    for tool in ("Read", "Bash"):
        monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({
            "session_id": "sess-wire",
            "tool_name": tool,
            "tool_input": {},
        })))
        assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-wire")
    assert turn.tool_names == ["Read", "Bash"]
    assert len(turn.tool_marks) == 2, "PostToolUse did not record a timestamp"
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_lifecycle.py tests/test_hook.py -q -k "mark or tool_marks"
```

Expected: FAIL with `AttributeError: 'TurnState' object has no attribute 'tool_marks'`.

- [ ] **Step 3: Add the field**

In `src/skill_advisor/lifecycle.py`, in the `TurnState` dataclass after `tool_names`:

```python
    tool_names: list[str] = field(default_factory=list)
    # One epoch-second mark per tool call, index-parallel to `tool_names`.
    # Written here rather than derived later because PostToolUse is the only
    # place that knows when a call finished. Raw on purpose: the idle threshold
    # is applied at read time by bleed.py, so it stays retroactively tunable.
    tool_marks: list[float] = field(default_factory=list)
```

and in `from_json`, after the `tool_names=` line:

```python
            tool_marks=[float(m) for m in (data.get("tool_marks") or [])],
```

- [ ] **Step 4: Append the mark**

In `record_tool`, immediately after the existing `turn.tool_names.append(tool_name)`:

```python
    turn.tool_names.append(tool_name)
    turn.tool_marks.append(time.time())
```

`time` is already imported in this module (`turn_started_at` uses `time.time`).

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS, 589 + 4 = 593.

- [ ] **Step 6: Mutation-check the wiring test**

Delete the `turn.tool_marks.append(time.time())` line, then:

```bash
uv run pytest tests/test_hook.py -q -k wires_the_mark
```

Expected: **FAIL** with "PostToolUse did not record a timestamp". Restore the line with the inverse edit (retype it — do not `git restore`, which would also wipe the Step 3 field), then re-run and confirm GREEN.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src/skill_advisor/lifecycle.py tests/test_lifecycle.py tests/test_hook.py
uv run ruff format --check src/skill_advisor/lifecycle.py tests/test_lifecycle.py tests/test_hook.py
git add src/skill_advisor/lifecycle.py tests/test_lifecycle.py tests/test_hook.py
git commit -m "feat(lifecycle): record a timestamp per tool call

One float appended in a turn-state write that already happens on every
PostToolUse. No new process, no new hook."
```

---

### Task 2: Flush spans into the stop event

**Files:**
- Modify: `src/skill_advisor/telemetry.py` (`record_stop` at 131-165)
- Modify: `src/skill_advisor/hook.py` (`run_stop`, the `record_stop` call at 466-476)
- Test: `tests/test_telemetry.py`, `tests/test_hook.py`

**Interfaces:**
- Consumes: `TurnState.tool_marks` and `TurnState.turn_started_at` from Task 1.
- Produces: `stop` event keys `tool_spans: list[[str, int]]` (name, delta ms) and `span_anchor: str`. Consumed by Task 3.
- Produces: `telemetry.record_stop(..., tool_spans: list[tuple[str, int]] = (), span_anchor: str = "first_tool")`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_telemetry.py`:

```python
def test_record_stop_writes_tool_spans(isolated_paths):
    import json
    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s1",
        tools=["Read", "Bash"],
        subagents=[],
        skills=["ai-code-review"],
        tool_spans=[("Read", 412), ("Bash", 138204)],
        config=TelemetryConfig(events_enabled=True),
    )

    row = json.loads(paths.events_file().read_text(encoding="utf-8").splitlines()[-1])
    assert row["tool_spans"] == [["Read", 412], ["Bash", 138204]]
    assert row["span_anchor"] == "first_tool"


def test_record_stop_omits_spans_when_there_are_none(isolated_paths):
    """A turn from before the feature must not gain a misleading empty key."""
    import json
    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s2", tools=["Read"], subagents=[], skills=[],
        config=TelemetryConfig(events_enabled=True),
    )

    row = json.loads(paths.events_file().read_text(encoding="utf-8").splitlines()[-1])
    assert "tool_spans" not in row
    assert "span_anchor" not in row


def test_record_stop_never_writes_tool_input(isolated_paths):
    """Privacy: names only. No paths, no commands, no arguments."""
    import json
    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s3",
        tools=["Bash"], subagents=[], skills=[],
        tool_spans=[("Bash", 900)],
        config=TelemetryConfig(events_enabled=True),
    )

    raw = paths.events_file().read_text(encoding="utf-8")
    assert "tool_input" not in raw
    assert "/home/" not in raw
```

Append to `tests/test_hook.py`:

```python
def test_stop_wires_marks_into_spans(monkeypatch, isolated_paths):
    """Goes RED if run_stop stops converting marks into spans."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    config_path = paths.config_file()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[telemetry]\nevents_enabled = true\n", encoding="utf-8")

    turn = lifecycle.TurnState(session_id="sess-spans", turn_started_at=1000.0)
    turn.tool_names = ["Read", "Bash"]
    turn.tool_marks = [1000.5, 1003.5]
    lifecycle.save_turn(turn)

    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"session_id": "sess-spans"})))
    assert hook.run_stop() == 0

    rows = [_json.loads(l) for l in paths.events_file().read_text().splitlines() if l.strip()]
    stop = [r for r in rows if r.get("kind") == "stop"][-1]
    assert stop["tool_spans"] == [["Read", 500], ["Bash", 3000]]


def test_stop_drops_spans_when_marks_desync(monkeypatch, isolated_paths):
    """A torn write must produce NO spans, never a guessed alignment."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    config_path = paths.config_file()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[telemetry]\nevents_enabled = true\n", encoding="utf-8")

    turn = lifecycle.TurnState(session_id="sess-desync", turn_started_at=1000.0)
    turn.tool_names = ["Read", "Bash", "Edit"]
    turn.tool_marks = [1000.5, 1003.5]  # one short
    lifecycle.save_turn(turn)

    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"session_id": "sess-desync"})))
    assert hook.run_stop() == 0

    rows = [_json.loads(l) for l in paths.events_file().read_text().splitlines() if l.strip()]
    stop = [r for r in rows if r.get("kind") == "stop"][-1]
    assert "tool_spans" not in stop
    assert stop["tools"] == ["Read", "Bash", "Edit"]  # names still recorded
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_telemetry.py tests/test_hook.py -q -k "span or tool_input"
```

Expected: FAIL — `record_stop() got an unexpected keyword argument 'tool_spans'`.

- [ ] **Step 3: Extend `record_stop`**

In `src/skill_advisor/telemetry.py`, change the signature and event construction:

```python
def record_stop(
    *,
    session_id: str | None,
    tools: Iterable[str],
    subagents: Iterable[str],
    skills: Iterable[str] = (),
    tool_spans: Iterable[tuple[str, int]] = (),
    span_anchor: str = "first_tool",
    config: TelemetryConfig,
) -> None:
```

and after the existing `"skills": [...]` line, before `paths.ensure_dirs()`:

```python
    spans = [[str(name), int(ms)] for name, ms in tool_spans]
    if spans:
        event["tool_spans"] = spans
        # Constant today. Written anyway so a later anchor change (e.g. to
        # prompt submission) is distinguishable in historical rows instead of
        # silently redefining what a span means.
        event["span_anchor"] = span_anchor
```

Keys are omitted entirely when there are no spans, so a span-less turn is not confusable with a turn whose tools all took 0 ms.

- [ ] **Step 4: Compute the deltas in `run_stop`**

In `src/skill_advisor/hook.py`, immediately before the `telemetry.record_stop(` call:

```python
        # Marks are index-parallel to tool_names. A desync means a torn write —
        # emit no spans rather than guessing an alignment.
        spans: list[tuple[str, int]] = []
        if len(turn.tool_marks) == len(turn.tool_names):
            previous = turn.turn_started_at
            for name, mark in zip(turn.tool_names, turn.tool_marks):
                # Clamp: a clock adjustment mid-turn must never yield a negative.
                spans.append((name, max(0, int((mark - previous) * 1000))))
                previous = mark
```

and pass it through:

```python
            telemetry.record_stop(
                session_id=session_id,
                tools=turn.tool_names,
                subagents=turn.subagents_invoked,
                skills=turn.skills_invoked,
                tool_spans=spans,
                config=cfg.telemetry,
            )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS, 593 + 7 = 600.

- [ ] **Step 6: Mutation-check**

Change the desync guard from `==` to `>=`, then:

```bash
uv run pytest tests/test_hook.py -q -k desync
```

Expected: **FAIL** — `zip` would silently truncate to the shorter list, which is exactly the guessed alignment the guard exists to prevent. Restore `==`, re-run, confirm GREEN.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src/skill_advisor/telemetry.py src/skill_advisor/hook.py tests/test_telemetry.py tests/test_hook.py
uv run ruff format --check src/skill_advisor/telemetry.py src/skill_advisor/hook.py tests/test_telemetry.py tests/test_hook.py
git add src/skill_advisor/telemetry.py src/skill_advisor/hook.py tests/test_telemetry.py tests/test_hook.py
git commit -m "feat(telemetry): flush per-tool spans into the stop event

Raw deltas in ms, no idle classification at write time — the threshold is
applied at read time so it stays retroactively tunable. Desynced marks emit
no spans rather than a guessed alignment."
```

---

### Task 3: The pure aggregator — pairing and the idle rule

**Files:**
- Create: `src/skill_advisor/bleed.py`
- Test: `tests/test_bleed.py` (create)

**Interfaces:**
- Consumes: raw event dicts as written by Tasks 1-2.
- Produces, all importable from `skill_advisor.bleed`:
  - `@dataclass Turn`: `session: str | None`, `start: datetime`, `stop: datetime`, `skills: list[str]`, `tools: list[str]`, `spans: list[tuple[str, int]] | None`
  - `Turn.duration_s -> float`
  - `parse_ts(text: object) -> datetime | None` — public; the CLI windows with it
  - `pair_turns(events: Iterable[dict]) -> tuple[list[Turn], int]` — returns turns and the count of unpaired prompts
  - `@dataclass Attribution`: `per_tool: dict[str, int]`, `idle_ms: int`, `idle_gaps: int`
  - `attribute(spans: Sequence[tuple[str, int]], threshold_ms: int) -> Attribution`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bleed.py`:

```python
from datetime import datetime, timezone

import pytest

from skill_advisor import bleed


def _prompt(session="s", ts="2026-08-06T10:00:00Z", **kw):
    return {"kind": "prompt", "session_sha256": session, "ts": ts, **kw}


def _stop(session="s", ts="2026-08-06T10:05:00Z", **kw):
    return {"kind": "stop", "session_sha256": session, "ts": ts,
            "tools": [], "subagents": [], "skills": [], **kw}


# --- the idle rule -------------------------------------------------------

def test_a_short_span_is_charged_in_full():
    a = bleed.attribute([("Read", 5_000)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 5_000}
    assert a.idle_ms == 0
    assert a.idle_gaps == 0


def test_a_span_exactly_at_the_threshold_is_still_charged_in_full():
    """The boundary is <=, not <. A rule nobody pinned is a rule nobody keeps."""
    a = bleed.attribute([("Read", 120_000)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 120_000}
    assert a.idle_ms == 0


def test_a_long_span_is_capped_and_the_excess_spills_to_idle():
    a = bleed.attribute([("Bash", 300_000)], threshold_ms=120_000)
    assert a.per_tool == {"Bash": 120_000}
    assert a.idle_ms == 180_000
    assert a.idle_gaps == 1


def test_a_negative_span_is_clamped_to_zero():
    """A clock adjustment mid-turn must never produce a negative total."""
    a = bleed.attribute([("Read", -4_000)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 0}
    assert a.idle_ms == 0


def test_repeated_tools_accumulate():
    a = bleed.attribute([("Read", 100), ("Read", 250)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 350}


# --- turn pairing --------------------------------------------------------

def test_a_prompt_pairs_with_the_next_stop_in_its_session():
    turns, unpaired = bleed.pair_turns([_prompt(), _stop()])
    assert unpaired == 0
    assert len(turns) == 1
    assert turns[0].duration_s == 300.0


def test_a_legacy_row_without_a_kind_counts_as_a_prompt():
    """838 rows predate the `kind` key. Dropping them loses the oldest history."""
    legacy = {"session_sha256": "s", "ts": "2026-08-06T10:00:00Z"}
    turns, _ = bleed.pair_turns([legacy, _stop()])
    assert len(turns) == 1


def test_a_prompt_with_no_stop_is_counted_unpaired_not_dropped_silently():
    """run_stop returns early when the turn used no tools, so no stop event is
    written at all. Expected, but it must be visible in the count."""
    turns, unpaired = bleed.pair_turns([_prompt()])
    assert turns == []
    assert unpaired == 1


def test_turns_do_not_pair_across_sessions():
    turns, unpaired = bleed.pair_turns([_prompt(session="a"), _stop(session="b")])
    assert turns == []
    assert unpaired == 1


def test_a_second_prompt_before_a_stop_orphans_the_first():
    turns, unpaired = bleed.pair_turns([
        _prompt(ts="2026-08-06T10:00:00Z"),
        _prompt(ts="2026-08-06T10:01:00Z"),
        _stop(ts="2026-08-06T10:02:00Z"),
    ])
    assert unpaired == 1
    assert len(turns) == 1
    assert turns[0].duration_s == 60.0


def test_spans_are_carried_onto_the_turn_and_absent_ones_are_none():
    with_spans, _ = bleed.pair_turns([_prompt(), _stop(tool_spans=[["Read", 412]])])
    without, _ = bleed.pair_turns([_prompt(), _stop()])
    assert with_spans[0].spans == [("Read", 412)]
    assert without[0].spans is None


def test_an_unparseable_timestamp_does_not_crash_the_pairing():
    turns, unpaired = bleed.pair_turns([_prompt(ts="not-a-date"), _stop()])
    assert turns == []
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_bleed.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'skill_advisor.bleed'`.

- [ ] **Step 3: Create `bleed.py`**

```python
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


def pair_turns(events: Iterable[dict]) -> tuple[list[Turn], int]:
    """Pair each prompt with the next stop in the same session.

    Returns (turns, unpaired_prompt_count). A prompt with no following stop is
    normal, not an error: `run_stop` returns early when the turn used no tools,
    so a purely conversational turn writes no stop event at all. It is counted
    so the report can disclose it instead of silently shrinking the corpus.
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
    for session, rows in by_session.items():
        rows.sort(key=lambda r: str(r.get("ts") or ""))
        pending: datetime | None = None
        for row in rows:
            ts = parse_ts(row.get("ts"))
            if ts is None:
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
            turns.append(Turn(
                session=session,
                start=pending,
                stop=ts,
                skills=[str(s) for s in (row.get("skills") or [])],
                tools=[str(t) for t in (row.get("tools") or [])],
                spans=spans,
            ))
            pending = None
        if pending is not None:
            unpaired += 1
    return turns, unpaired
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_bleed.py -q
```

Expected: PASS, 12 tests (600 -> 612).

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/skill_advisor/bleed.py tests/test_bleed.py
uv run ruff format --check src/skill_advisor/bleed.py tests/test_bleed.py
git add src/skill_advisor/bleed.py tests/test_bleed.py
git commit -m "feat(bleed): pure turn pairing and the cap-and-spill idle rule

Threshold applied at read time, so it stays retroactively tunable. Stdlib
only and no package imports — the idle rule is unit-testable without hooks."
```

---

### Task 4: Skill and tool statistics

**Files:**
- Modify: `src/skill_advisor/bleed.py`
- Test: `tests/test_bleed.py`

**Interfaces:**
- Consumes: `Turn`, `Attribution`, `attribute()` from Task 3.
- Produces:
  - `@dataclass SkillStat`: `name: str`, `n: int`, `p50_turn_s: float`, `attributed_ms: int`, `idle_ms: int`, `turns_with_idle: int`
  - `@dataclass ToolStat`: `name: str`, `calls: int`, `p50_ms: int`, `attributed_ms: int`
  - `skill_stats(turns, *, threshold_ms, min_n) -> tuple[list[SkillStat], int]` — ranked descending by `attributed_ms`, plus the count of skills below `min_n`
  - `tool_stats(turns, *, threshold_ms) -> list[ToolStat]` — ranked descending by `attributed_ms`
  - `span_coverage(turns) -> tuple[int, int]` — `(turns_with_spans, total_turns)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bleed.py`. **Extend the existing import line** at the top of that file
to `from datetime import datetime, timedelta, timezone` — `timedelta` is new here.

```python
def _turn(seconds, skills=(), spans=None):
    """Build a Turn directly — these tests exercise the statistics, not pairing."""
    start = datetime(2026, 8, 6, 10, 0, 0, tzinfo=timezone.utc)
    return bleed.Turn(
        session="s",
        start=start,
        stop=start + timedelta(seconds=seconds),
        skills=list(skills),
        tools=[n for n, _ in (spans or [])],
        spans=spans,
    )


def test_skill_stats_rank_by_total_attributed_time():
    turns = [
        _turn(600, skills=["slow"], spans=[("Bash", 300_000)]),
        _turn(60, skills=["fast"], spans=[("Read", 10_000)]),
        _turn(60, skills=["fast"], spans=[("Read", 10_000)]),
    ]
    stats, below = skill_stats_of(turns, min_n=1)
    assert [s.name for s in stats] == ["slow", "fast"]
    assert stats[0].attributed_ms == 120_000   # capped at the threshold
    assert stats[0].idle_ms == 180_000
    assert stats[1].attributed_ms == 20_000
    assert below == 0


def test_a_skill_below_min_n_is_counted_not_dropped():
    """Task 13 of the catalog-refresh branch shipped a dry run that silently
    hid rows. Counted-but-not-ranked is the rule here."""
    turns = [
        _turn(60, skills=["rare"], spans=[("Read", 1_000)]),
        _turn(60, skills=["common"], spans=[("Read", 1_000)]),
        _turn(60, skills=["common"], spans=[("Read", 1_000)]),
    ]
    stats, below = skill_stats_of(turns, min_n=2)
    assert [s.name for s in stats] == ["common"]
    assert below == 1


def test_a_turn_invoking_two_skills_counts_toward_both():
    turns = [_turn(60, skills=["a", "b"], spans=[("Read", 4_000)])]
    stats, _ = skill_stats_of(turns, min_n=1)
    assert {s.name for s in stats} == {"a", "b"}
    assert all(s.attributed_ms == 4_000 for s in stats)


def test_turns_with_idle_counts_turns_not_gaps():
    turns = [_turn(600, skills=["s"], spans=[("Bash", 300_000), ("Bash", 300_000)])]
    stats, _ = skill_stats_of(turns, min_n=1)
    assert stats[0].turns_with_idle == 1


def test_a_span_less_turn_still_counts_toward_n_but_adds_no_time():
    turns = [
        _turn(600, skills=["s"], spans=None),
        _turn(60, skills=["s"], spans=[("Read", 5_000)]),
    ]
    stats, _ = skill_stats_of(turns, min_n=1)
    assert stats[0].n == 2
    assert stats[0].attributed_ms == 5_000


def test_tool_stats_aggregate_calls_and_p50():
    turns = [_turn(60, spans=[("Read", 100), ("Read", 300), ("Bash", 50_000)])]
    stats = bleed.tool_stats(turns, threshold_ms=120_000)
    by_name = {s.name: s for s in stats}
    assert by_name["Read"].calls == 2
    assert by_name["Read"].p50_ms == 300      # upper median of [100, 300]
    assert by_name["Bash"].attributed_ms == 50_000
    assert [s.name for s in stats] == ["Bash", "Read"]


def test_tool_p50_uses_capped_values_not_raw():
    """One row, one meaning. Without this the fixture spans all sit under the
    threshold and nothing discriminates capped from raw."""
    turns = [_turn(600, spans=[("Bash", 300_000), ("Bash", 300_000)])]
    stats = bleed.tool_stats(turns, threshold_ms=120_000)
    assert stats[0].p50_ms == 120_000
    assert stats[0].attributed_ms == 240_000


def test_span_coverage_reports_both_populations():
    turns = [_turn(60, spans=[("Read", 1)]), _turn(60, spans=None), _turn(60, spans=None)]
    assert bleed.span_coverage(turns) == (1, 3)


def skill_stats_of(turns, *, min_n):
    return bleed.skill_stats(turns, threshold_ms=120_000, min_n=min_n)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_bleed.py -q -k "stat or coverage"
```

Expected: FAIL — `module 'skill_advisor.bleed' has no attribute 'skill_stats'`.

- [ ] **Step 3: Implement the statistics**

Append to `src/skill_advisor/bleed.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS, 612 + 8 = 620.

- [ ] **Step 5: Mutation-check the min-n guard**

Change `below = sum(1 for s in ranked if s.n < min_n)` to `below = 0`, then:

```bash
uv run pytest tests/test_bleed.py -q -k below_min_n
```

Expected: **FAIL**. Restore with the inverse edit, re-run, confirm GREEN.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/skill_advisor/bleed.py tests/test_bleed.py
uv run ruff format --check src/skill_advisor/bleed.py tests/test_bleed.py
git add src/skill_advisor/bleed.py tests/test_bleed.py
git commit -m "feat(bleed): skill and tool statistics with a counted min-n guard

Below-min-n skills are counted and reported, never silently dropped."
```

---

### Task 5: The `bleed` command

**Files:**
- Modify: `src/skill_advisor/cli.py`
- Test: `tests/test_bleed_cli.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 3-4, plus `telemetry.iter_events` and `telemetry.parse_duration`.
- Produces: `_cmd_bleed(args) -> int` and the `bleed` subparser.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bleed_cli.py`:

**Invocation pattern — follow the repo, do not call `cli.main`.** `main()` ends in
`sys.exit(args.func(args))`, so calling it from a test raises `SystemExit` rather than
returning a code. `tests/test_report_cli.py` builds an `argparse.Namespace` and calls the
`_cmd_*` function directly, which returns an `int`. Do the same.

```python
"""Tests for the `skill-advisor bleed` CLI subcommand."""
from __future__ import annotations

import json
from argparse import Namespace

from skill_advisor import cli, paths


def _ns(**overrides) -> Namespace:
    base = dict(
        since=None,
        min_n=5,
        idle_threshold=120.0,
        by="both",
        limit=20,
        json=False,
    )
    base.update(overrides)
    return Namespace(**base)


def _write_events(rows):
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _pair(ts_a, ts_b, skills, spans=None, session="s"):
    stop = {"kind": "stop", "session_sha256": session, "ts": ts_b,
            "tools": [n for n, _ in (spans or [])], "subagents": [], "skills": skills}
    if spans:
        stop["tool_spans"] = [[n, ms] for n, ms in spans]
    return [{"kind": "prompt", "session_sha256": session, "ts": ts_a}, stop]


def test_bleed_reports_a_skill_ranking(isolated_paths, capsys):
    _write_events(
        _pair("2026-08-06T10:00:00Z", "2026-08-06T10:10:00Z", ["slow"], [("Bash", 300_000)])
        + _pair("2026-08-06T11:00:00Z", "2026-08-06T11:01:00Z", ["fast"], [("Read", 9_000)], "t")
    )
    assert cli._cmd_bleed(_ns(min_n=1)) == 0
    out = capsys.readouterr().out

    assert "slow" in out and "fast" in out
    assert out.index("slow") < out.index("fast")


def test_bleed_always_prints_span_coverage(isolated_paths, capsys):
    """Turn-level and per-tool tables are different populations. Never let them
    look like one."""
    _write_events(_pair("2026-08-06T10:00:00Z", "2026-08-06T10:10:00Z", ["s"]))
    cli._cmd_bleed(_ns(min_n=1))
    assert "spans:" in capsys.readouterr().out


def test_bleed_discloses_below_min_n_skills(isolated_paths, capsys):
    _write_events(_pair("2026-08-06T10:00:00Z", "2026-08-06T10:01:00Z", ["rare"], [("Read", 5)]))
    cli._cmd_bleed(_ns(min_n=5))
    out = capsys.readouterr().out
    assert "below n=5" in out
    assert "not ranked" in out


def test_bleed_limit_prints_an_explicit_overflow_count(isolated_paths, capsys):
    rows = []
    for i in range(5):
        rows += _pair(f"2026-08-06T1{i}:00:00Z", f"2026-08-06T1{i}:05:00Z",
                      [f"skill-{i}"], [("Read", (i + 1) * 1000)], session=f"s{i}")
    _write_events(rows)
    cli._cmd_bleed(_ns(min_n=1, limit=2))
    assert "+3 more" in capsys.readouterr().out


def test_bleed_counts_unparseable_rows_instead_of_hiding_them(isolated_paths, capsys):
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        fh.write("{not json\n")
        for row in _pair("2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z", ["s"], [("Read", 10)]):
            fh.write(json.dumps(row) + "\n")
    cli._cmd_bleed(_ns(min_n=1))
    assert "skipped 1" in capsys.readouterr().out


def test_bleed_counts_unparseable_rows_under_a_since_window_too(isolated_paths, capsys):
    """Global constraint: every excluded count is printed. A windowed read must
    not report 0 skipped just because subtraction would be meaningless there."""
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        fh.write("{not json\n")
        for row in _pair("2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z", ["s"], [("Read", 10)]):
            fh.write(json.dumps(row) + "\n")
    cli._cmd_bleed(_ns(min_n=1, since="3650d"))
    assert "skipped 1" in capsys.readouterr().out


def test_bleed_threshold_flag_changes_the_attribution(isolated_paths, capsys):
    """The whole reason the stored form is raw: history is re-readable at a
    different threshold."""
    _write_events(_pair("2026-08-06T10:00:00Z", "2026-08-06T10:10:00Z", ["s"], [("Bash", 200_000)]))

    cli._cmd_bleed(_ns(min_n=1, idle_threshold=300.0, json=True))
    wide = json.loads(capsys.readouterr().out)
    cli._cmd_bleed(_ns(min_n=1, idle_threshold=60.0, json=True))
    tight = json.loads(capsys.readouterr().out)

    assert wide["skills"][0]["attributed_ms"] == 200_000
    assert tight["skills"][0]["attributed_ms"] == 60_000
    assert tight["skills"][0]["idle_ms"] == 140_000


def test_bleed_with_no_events_exits_zero_with_an_explanation(isolated_paths, capsys):
    assert cli._cmd_bleed(_ns()) == 0
    assert "no telemetry events" in capsys.readouterr().out


def test_bleed_is_registered_as_a_subcommand(isolated_paths):
    """The parser wiring is a separate failure mode from the handler."""
    args = cli._build_parser().parse_args(["bleed", "--min-n", "3"])
    assert args.func is cli._cmd_bleed
    assert args.min_n == 3
    assert args.idle_threshold == 120.0
    assert args.by == "both"
```

Note the `args.json` attribute: argparse maps `--json` to `args.json`, which shadows the
`json` module only inside the `Namespace`, not in the module. `_cmd_bleed` uses the module
freely — but do not name a local variable `json` in that function.

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_bleed_cli.py -q
```

Expected: FAIL — `argument command: invalid choice: 'bleed'`.

- [ ] **Step 3: Implement the command**

Add to `src/skill_advisor/cli.py`, near `_cmd_report`:

```python
def _cmd_bleed(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from . import bleed as bleed_mod

    if not paths.events_file().is_file():
        print("no telemetry events recorded.")
        print("enable with `events_enabled = true` under [telemetry] in config.toml.")
        return 0

    cutoff = None
    if args.since:
        try:
            cutoff = datetime.now(timezone.utc) - telemetry.parse_duration(args.since)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    events, skipped = _load_events_counting_failures(cutoff)
    if not events:
        print("no telemetry events recorded.")
        return 0

    threshold_ms = int(args.idle_threshold * 1000)
    turns, unpaired = bleed_mod.pair_turns(events)
    skills, below = bleed_mod.skill_stats(turns, threshold_ms=threshold_ms, min_n=args.min_n)
    tools = bleed_mod.tool_stats(turns, threshold_ms=threshold_ms)
    with_spans, total = bleed_mod.span_coverage(turns)

    if args.json:
        print(json.dumps({
            "turns": total,
            "turns_with_spans": with_spans,
            "unpaired_prompts": unpaired,
            "skipped_rows": skipped,
            "skills_below_min_n": below,
            "idle_threshold_s": args.idle_threshold,
            "skills": [vars(s) for s in skills],
            "tools": [vars(t) for t in tools],
        }, indent=2))
        return 0

    print(f"turns: {total} · spans: {with_spans} of {total} "
          f"({(with_spans / total * 100) if total else 0:.1f}%) · "
          f"idle threshold: {args.idle_threshold:.0f}s")
    if unpaired:
        print(f"unpaired prompts: {unpaired} (turns that used no tools write no stop event)")
    if skipped:
        print(f"skipped {skipped} unparseable rows")
    print()

    if args.by in ("skill", "both"):
        _print_bleed_skills(skills, below, args)
    if args.by in ("tool", "both"):
        _print_bleed_tools(tools, args)
    return 0


def _load_events_counting_failures(cutoff) -> tuple[list[dict], int]:
    """Load events, counting rows that failed to parse.

    `telemetry.iter_events` swallows bad lines, so the count has to happen
    here. Parse directly rather than subtracting `len(iter_events())` from a
    line count: under a `--since` window an in-range row legitimately dropped
    by the cutoff is indistinguishable from a parse failure, and a number that
    is silently wrong under one flag is worse than no number.
    """
    from . import bleed as bleed_mod

    events: list[dict] = []
    skipped = 0
    with open(paths.events_file(), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(row, dict):
                skipped += 1
                continue
            if cutoff is not None:
                ts = bleed_mod.parse_ts(row.get("ts"))
                if ts is None or ts < cutoff:
                    continue
            events.append(row)
    return events, skipped


def _print_bleed_skills(skills, below, args) -> None:
    print(f"{'SKILL':42} {'n':>4} {'p50 turn':>9} {'attributed':>11} {'idle':>9} {'idle turns':>11}")
    for s in skills[: args.limit]:
        print(f"{s.name[:42]:42} {s.n:>4} {s.p50_turn_s:>8.0f}s "
              f"{s.attributed_ms / 3_600_000:>10.2f}h {s.idle_ms / 3_600_000:>8.2f}h "
              f"{s.turns_with_idle:>11}")
    if len(skills) > args.limit:
        print(f"  +{len(skills) - args.limit} more")
    if below:
        print(f"  {below} skill(s) below n={args.min_n} (not ranked)")
    print()


def _print_bleed_tools(tools, args) -> None:
    print(f"{'TOOL':24} {'calls':>7} {'p50':>9} {'attributed':>11}")
    for t in tools[: args.limit]:
        print(f"{t.name[:24]:24} {t.calls:>7} {t.p50_ms / 1000:>8.1f}s "
              f"{t.attributed_ms / 3_600_000:>10.2f}h")
    if len(tools) > args.limit:
        print(f"  +{len(tools) - args.limit} more")
```

Register the subparser next to `report`:

```python
    p_bleed = sub.add_parser(
        "bleed",
        help="rank skills and tools by the time they cost, with idle flagged",
    )
    p_bleed.add_argument("--since", default=None,
                         help="time window (e.g. 7d, 24h; default: all history)")
    p_bleed.add_argument("--min-n", dest="min_n", type=int, default=5,
                         help="minimum turns before a skill is ranked (default: 5)")
    p_bleed.add_argument("--idle-threshold", dest="idle_threshold", type=float, default=120.0,
                         help="seconds above which a span is treated as idle (default: 120)")
    p_bleed.add_argument("--by", choices=("skill", "tool", "both"), default="both",
                         help="which tables to print (default: both)")
    p_bleed.add_argument("--limit", type=int, default=20,
                         help="rows per table; overflow is reported (default: 20)")
    p_bleed.add_argument("--json", action="store_true",
                         help="emit machine-readable JSON instead of tables")
    p_bleed.set_defaults(func=_cmd_bleed)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS, 620 + 9 = 629.

- [ ] **Step 5: Run it against the real log**

```bash
uv run python -m skill_advisor bleed --min-n 5 --limit 10
```

Expected: a skills table (turn-level history is already there), `spans: 0 of N (0.0%)` because no turn has yet been recorded with the new collector, and a non-zero `unpaired prompts` line. **`spans: 0` is the correct output at this point, not a failure** — confirm by running a few real prompts through `claudeskill` afterwards and re-running.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/skill_advisor/cli.py tests/test_bleed_cli.py
uv run ruff format --check src/skill_advisor/cli.py tests/test_bleed_cli.py
git add src/skill_advisor/cli.py tests/test_bleed_cli.py
git commit -m "feat(cli): skill-advisor bleed

Ranks skills and tools by attributed time with idle flagged separately.
Span coverage, unpaired prompts, below-min-n skills, skipped rows and
--limit overflow are all printed as counts — nothing is dropped silently."
```

---

### Task 6: Verify the subagent zero, then document

**Files:**
- Modify: `README.md`
- Test: none new (this task resolves an open question and documents)

**Interfaces:** none.

The spec flags this deliberately: `subagents_invoked` is wired at `lifecycle.py:533` yet appears on **0 of 4,348** turns. A zero meaning "broken" and a zero meaning "unused" look identical in a table, and `bleed` must not imply it covers subagent time if the capture is dead.

- [ ] **Step 1: Determine which zero it is**

```bash
uv run pytest tests/test_hook.py -q -k subagent
```

Then drive the hook directly with a realistic `Task` payload:

```bash
uv run python - <<'PY'
import io, json, sys
sys.argv = ["x"]
from skill_advisor import hook, lifecycle
sys.stdin = io.StringIO(json.dumps({
    "session_id": "probe-subagent",
    "tool_name": "Task",
    "tool_input": {"subagent_type": "Explore", "prompt": "..."},
}))
hook.run_posttooluse()
print(lifecycle.load_turn("probe-subagent"))
PY
```

If `subagents_invoked == ["Explore"]`, the capture works and the live zero means subagent use is genuinely absent (consistent with the user's "don't call the Agent tool unless requested" rule) — record that. If it is empty, the payload key differs from `tool_input.subagent_type` and the capture is dead.

- [ ] **Step 2: Record the answer — and if it is dead, fix it**

If the capture **works**: add one line to the README's `bleed` section stating that subagent time is captured but historically unused, so an empty subagent column means "not used", not "not measured". Then go to Step 3.

If the capture is **dead** (controller ruling 2026-08-06: fix it inside this plan rather than defer it — a `bleed` report must never imply coverage it does not have, and a one-key payload fix is smaller than the disclaimer it would otherwise need):

1. Print the real payload shape to find the actual key:

```bash
uv run python - <<'PY'
import io, json, sys
from skill_advisor import hook
sys.stdin = io.StringIO(json.dumps({
    "session_id": "probe-shape", "tool_name": "Task",
    "tool_input": {"subagent_type": "Explore", "description": "d", "prompt": "p"},
}))
# Print what run_posttooluse actually reads, not what we assume it reads.
import skill_advisor.lifecycle as lc
orig = lc.record_tool
def spy(session_id, tool_name, *, subagent_type=None, skill_name=None):
    print("record_tool got:", tool_name, "subagent_type=", repr(subagent_type))
    return orig(session_id, tool_name, subagent_type=subagent_type, skill_name=skill_name)
lc.record_tool = spy
hook.run_posttooluse()
PY
```

2. Write a failing test in `tests/test_hook.py` that drives `run_posttooluse` with a realistic `Task` payload and asserts `turn.subagents_invoked == ["Explore"]`.
3. Fix the extraction in `hook.py`'s `run_posttooluse` (the `tool_input.get("subagent_type")` read at approximately `hook.py:384`) to use the key the probe actually revealed.
4. Re-run the full suite; commit as a separate commit with a message naming the measured evidence (`0 of 4,348 turns`) and the key that was wrong.

Either way, the README must state plainly what subagent coverage exists.

- [ ] **Step 3: Document `bleed` in the README**

Add a section after the `report` documentation covering: the command and every flag with its default; that the idle threshold is applied at read time and can be re-run over the same history; that a turn using no tools writes no stop event and therefore cannot be measured; and — most importantly — that a long span cannot distinguish an absent user from a genuinely slow tool, so a skill dominated by idle is **unmeasured**, not fast or slow.

- [ ] **Step 4: Verify the docs match the shipped flags**

```bash
uv run python -m skill_advisor bleed --help
```

Compare every flag and default against what the README claims. This repo has shipped a `doctor` NOTE recommending a command that did not exist; a defaults table that drifts from `--help` is the same defect.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document the bleed command and what it cannot measure"
```

---

## Self-Review

**Spec coverage.** Collector (`tool_marks`, no new process, `PreToolUse` rejected) → Task 1. Stored form (raw deltas, `span_anchor`, desync guard) → Task 2. Idle rule (cap-and-spill, negative clamp, threshold at read time) → Task 3. `bleed` command with all six flags and their stated defaults → Task 5. Output discipline (span coverage always printed, below-min-n counted, `+N more`, skipped rows counted) → Tasks 4-5, each with its own test. Retroactive behaviour → Task 5 Step 5, which explicitly names `spans: 0` as the correct first output. Privacy (names only) → Task 2 Step 1, `test_record_stop_never_writes_tool_input`. Failure-modes table: silent-on-error → covered by the existing `try/except` blocks the new code sits inside (Tasks 1, 2 Step 4); torn write → Task 2 desync test; clock skew → Task 3 negative-clamp test; old turn file → Task 1 `from_json` test; corrupt events → Task 5 skipped-rows test; skill in a tool-less turn → Task 4 span-less test. The open question → Task 6.

**The load-bearing test is present and mutation-checked.** Task 1 Step 6 deletes the `tool_marks.append(...)` call and requires `test_posttooluse_wires_the_mark_into_turn_state` to go RED. Task 2 Step 6 and Task 4 Step 5 do the same for the desync guard and the min-n counter. Restores are specified as inverse edits, never `git restore` — which in this repo has previously wiped the uncommitted fix alongside the mutation.

**Placeholder scan.** No TBDs. Every code step carries runnable code. Task 6 is the one task whose outcome is genuinely unknown before it runs, and it therefore specifies both branches and what to do in each, rather than deferring the decision.

**Type consistency.** `Turn`, `Attribution`, `SkillStat`, `ToolStat` are defined in Tasks 3-4 and consumed with identical field names in Task 5's printers (`s.name`, `s.n`, `s.p50_turn_s`, `s.attributed_ms`, `s.idle_ms`, `s.turns_with_idle`; `t.name`, `t.calls`, `t.p50_ms`, `t.attributed_ms`). `attribute(spans, threshold_ms)` is positional-then-keyword in Task 3 and called that way in Task 4. `skill_stats(turns, *, threshold_ms, min_n)` and `tool_stats(turns, *, threshold_ms)` are keyword-only in both their definition and Task 5's call site. `record_stop`'s new `tool_spans` parameter is spelled identically in Task 2's signature, its call site in `run_stop`, and all three telemetry tests. `tool_marks` is spelled identically across Tasks 1, 2 and their tests.

**Corrections made during this self-review**, both caught by checking the codebase rather than trusting the draft:

- The CLI tests originally called `cli.main(["bleed", ...])` and asserted a return code. `main()` ends in `sys.exit(args.func(args))`, so that raises `SystemExit` and never returns. Rewritten to the repo's actual pattern from `tests/test_report_cli.py` — build an `argparse.Namespace` via a `_ns()` helper and call `cli._cmd_bleed(ns)`, which does return an `int`. A separate test now covers parser registration, since handler correctness and subparser wiring are different failure modes.
- Task 4's `_turn` helper used `__import__("datetime").timedelta`. Replaced with a proper `timedelta` import, with an explicit instruction to extend the existing import line in `tests/test_bleed.py` rather than add a second one.

**Known rough edge, deliberate.** `_load_events_counting_failures` can only count skipped rows when `--since` is absent, because a windowed read legitimately drops in-range rows. That is why it reports `0` under a cutoff rather than a wrong number — noted in the code comment so the next reader does not "fix" it into a lie.
