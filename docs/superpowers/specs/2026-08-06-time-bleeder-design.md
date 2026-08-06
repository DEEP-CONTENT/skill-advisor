# Time bleeder — design

**Date:** 2026-08-06
**Status:** approved, ready for implementation planning
**Scope:** Phase 1 only — the collector, the aggregator, and the `bleed` command. The
recommend-time nudge and the statusline surface are Phase 2 and get their own spec.

---

## Problem

The advisor knows which skills it recommended and which were invoked. It does not know what
any of them **cost**. There is no way to answer "which skills eat my day, and where does the
time inside a turn actually go" — so there is no way to tell a skill that earns its ten
minutes from one that merely takes them.

### What is already measurable, today, with no new code

`prompt` events carry a timestamp at `UserPromptSubmit`; `stop` events carry one when the
turn ends. Pairing them within a session yields turn duration retroactively. Measured
2026-08-06 against the live event log:

```
4,348 paired turns · 651 hours of wall clock · back to 2026-04-28
p50   204 s      p90  1,132 s      p99  5,727 s      max  21,328 s
1,739 turns over 5 minutes
```

A first-cut ranking already separates skills by an order of magnitude:

| skill | n | p50 | total |
|---|---|---|---|
| `superpowers:finishing-a-development-branch` | 8 | 2,110 s | 6.4 h |
| `superpowers:subagent-driven-development` | 12 | 1,732 s | 5.3 h |
| `ai-code-review` | 35 | 1,153 s | 12.2 h |
| `fix-review` | 33 | 584 s | 12.8 h |
| `address-github-comments` | 20 | 332 s | 2.3 h |

### Why turn-level alone is not enough

The table above is correlation. It says turns invoking `ai-code-review` run ~19 minutes; it
cannot say whether that is the skill's own structure, one pathological `Bash` loop, or the
model re-reading the same files. Optimising a skill requires knowing which part of it is
expensive.

### Two measurement hazards, both real in the existing data

**Idle time.** The p99 of 5,727 s and max of 21,328 s are not work. They are a person away
from the keyboard, or an unanswered permission prompt. Charged naively, the longest gaps
land on whichever tool happened to be next, and that tool becomes the permanent #1 "bleeder".

**Thin skill attribution.** Only 131 of 4,348 turns (3.0%) record an invoked skill, because
that capture landed with PR #9. It grows from here, but any ranking must guard against
declaring a winner on n=2.

---

## Design

Four units, split so that nothing runs on the hot path and each piece is testable alone.

```
PostToolUse ──> lifecycle.record_tool()        [+ tool_marks: one epoch float per call]
                      │
                 turn-state file
                      │
Stop ─────────> flush ──────────> events.jsonl  stop event
                                    + tool_spans: [[name, delta_ms], ...]
                      │
                      ▼
                 bleed.py   (pure: event rows -> stats; idle rule lives here)
                      │
                      ▼
                 cli.py     `skill-advisor bleed`
```

`UserPromptSubmit` is not touched. The whole feature is a reader plus one extra field in a
write that already happens.

### 1. The collector

`lifecycle.record_tool()` already loads, mutates and saves the turn-state file on **every**
tool call. It gains one parallel list:

```python
tool_marks: list[float] = field(default_factory=list)   # epoch seconds, parallel to tool_names
```

appended in the same call that appends `tool_names`, with the matching
`list(data.get("tool_marks") or [])` in `from_json`. **Cost is one float in an existing
write — no new process, no new hook.** A `PreToolUse` hook was considered and rejected: it
would cost a process spawn per tool call, and the only thing it buys is separating model
thinking from tool execution, which this spec does not attempt.

Anchoring: a span is **time since the previous tool call finished**, so the loop starts at the
second tool. The turn's first tool has no predecessor and therefore gets **no span at all**;
a one-tool turn emits an empty list and honestly drops out of span coverage rather than being
counted as covered by a meaningless zero.

`TurnState.turn_started_at` was the original anchor for the first span, and that was wrong:
`record_tool` sets `turn_started_at` (a `default_factory=time.time`) and appends
`tool_marks[0]` two statements later, so on every real turn they are the *same instant* — the
first tool was charged a structural 0 ms. Measured on the live log: 4,780 calls (2.4%)
permanently zero, and 457 turns (9.6%) whose entire span list was a single `[["X", 0]]`.
The prompt→first-tool interval is deliberately not part of any tool's span.

### 2. The stored form

At `Stop`, spans are flushed into the event as deltas in milliseconds:

```json
"tool_spans": [["Bash", 138204], ["Edit", 890]],
"span_anchor": "previous_tool"
```

The turn above used three tools — `Read`, then `Bash`, then `Edit`. `Read` is first and
carries no span. Both keys are omitted entirely when the list is empty, so a one-tool turn
looks identical to a pre-feature turn: span-less, not zero.

`span_anchor` is a constant today. It is written anyway so that a later change of anchor
(e.g. to prompt submission, if `UserPromptSubmit` ever stamps the turn) is distinguishable in
historical rows instead of silently changing what a span means.

**Record raw, classify at read time.** No idle threshold is baked into the stored data. The
threshold is applied by `bleed.py` when the report runs, so changing it re-interprets all
history rather than only affecting turns recorded afterwards. This is the single design
decision most likely to be regretted if inverted.

Desync guard: a turn where `len(tool_marks) != len(tool_names)` is treated as **span-less**,
not repaired by guessing. That covers both pre-feature turn files and a torn write.

### 3. The idle rule

Pure, in `bleed.py`, threshold default 120 s:

```
delta <= threshold   -> charge fully to that tool
delta >  threshold   -> charge `threshold`, spill the excess into idle_ms
delta <  0           -> clamp to 0        (clock adjustment)
```

Cap-and-spill rather than all-or-nothing, so a genuinely slow tool does not drop to zero
attributed time merely for crossing the line.

**The limitation is stated, not engineered around.** PostToolUse-only data cannot distinguish
"away from the keyboard" from "that test run really did take four minutes". Both are one long
gap. So idle is never hidden: a skill whose time is mostly idle reads as **unmeasured**,
not as fast or slow.

### 4. The `bleed` command

```
skill-advisor bleed [--since 30d] [--min-n 5] [--idle-threshold 120]
                    [--by skill|tool|both] [--limit 20] [--json]
```

Defaults, stated so they are not invented during implementation: `--since` unset means **all
history**; `--min-n 5`; `--idle-threshold 120` (seconds); `--by both`; `--limit 20`;
`--json` off, human table by default.

`--idle-threshold` is a flag and not only a config key, so the same history can be re-read at
60 s and at 300 s. If the ranking moves, that is itself the finding.

Output discipline, following defects this repo has already shipped and fixed:

- **Span coverage is always printed** — `spans: 0 of 4,348 turns (0.0%)`. Turn-level stats
  and per-tool stats are different populations and are never silently mixed.
- **Below-min-n skills are counted, not dropped** — `12 skills below n=5 (not ranked)`.
  Task 13 of the catalog-refresh branch shipped a dry-run proposing changes that could not
  happen; the fix was filtering before scoring and never cutting silently.
- **`--limit` prints an explicit `+N more`.** Never a silent truncation.
- **Unparseable event rows are skipped and counted** — `skipped N unparseable rows`.

### Retroactive behaviour

`bleed` is useful on first run. Existing turns have no spans but do have start/stop
timestamps, so turn-level and per-skill stats cover 651 hours immediately while the per-tool
breakdown accrues from install. The report labels which population each table is drawn from.

---

## Privacy

Tool **names** only. `tool_input` never reaches the event — no file paths, no shell commands,
no arguments. This matches how prompts are already reduced to `prompt_sha256`, and is covered
by an explicit test rather than left to reviewer vigilance.

---

## Failure modes

| Failure | Behaviour |
|---|---|
| `record_tool` raises while appending a mark | Caught by the existing `try/except` in `run_posttooluse`. Hook stays silent; the turn simply has no spans. |
| Turn file torn mid-write | `tool_marks` desyncs from `tool_names`; the turn is treated as span-less. No guessed alignment. |
| Clock adjusted backwards mid-turn | Negative delta clamped to 0. Never a negative total. |
| Old turn-state file with no `tool_marks` | `from_json` yields `[]`; turn is span-less. Same path as a pre-feature turn. |
| Older binary reads a newer turn file | Already safe — `from_json` ignores unknown keys (PR #9, Task 7b). |
| `events.jsonl` corrupt in places | `bleed` skips and reports the count. Never a silent partial report. |
| Skill invoked in a turn with no tools | Turn-level stats still count it; it contributes no spans. |
| A long-running tool is indistinguishable from an idle user | Accepted and disclosed. Both surface in the idle column. |

---

## Testing

Unit tests over `bleed.py`, which is pure: threshold boundary (delta exactly at the limit,
and one over), cap-and-spill arithmetic, negative-delta clamp, span-less turns excluded from
tool stats but retained in turn stats, desynced marks treated as span-less, min-n counted but
not ranked, and `from_json` with no `tool_marks`.

**The load-bearing test is a wiring test, not a unit test:**

> A test must go **RED** if `record_tool` stops appending the mark — driven through
> `hook.run_posttooluse()`, not through the aggregator.

This repo's most expensive recurring defect is exactly this shape: units covered, the call
into the hook unpinned. An aggregator with perfect unit coverage and an unwired collector
produces an empty report that reads as "no time bleeders found" rather than as a bug.

One privacy test asserts `tool_input` contents never appear in a written event.

---

## Deliberately out of scope

- **The recommend-time nudge and the statusline surface.** Both run on the hot path, which
  currently spends p50 14,781 ms per non-triaged prompt — and of 590 such prompts measured
  2026-08-06, 343 (58.1%) ended in a judge timeout against 142 (24.1%) where the judge
  answered.
  Adding a stats lookup there needs a precomputed cache with its own invalidation story, and
  that work is only worth doing once `bleed` shows the ranking is signal rather than noise.
- **Separating model thinking from tool execution.** Requires `PreToolUse` and a spawn per
  tool call. Revisit only if a span turns out to be dominated by an unattributable middle.
- **`events.jsonl` rotation.** Spans add roughly 600 bytes per turn — about 2.6 MB against
  the current 4.2 MB log over the same period, ~60% growth, with no rotation today. Noted as
  a follow-up trigger, not built here.
- **Acting on the findings automatically** — no auto-demotion of expensive skills, no
  budget enforcement. This spec measures; it does not decide.

---

## Open question for implementation, not for this spec

`subagents_invoked` is wired (`lifecycle.py:533`, on `tool_name == "Task"` with
`tool_input.subagent_type`) yet appears on **0 of 4,348** turns. Either subagent use is
genuinely absent, or `subagent_type` is not arriving in the payload. The implementation plan
should verify this empirically before the report claims to cover subagent time — a zero that
means "broken" and a zero that means "unused" look identical in a table.
