# Effort signalling & baseline tuning — design

**Date:** 2026-07-29
**Status:** approved, ready for implementation planning
**Scope:** one sub-project. Catalog refresh / skill rotation is a separate spec (see [Out of scope](#out-of-scope)).

---

## Problem

Claude Code exposes a reasoning-effort dial with five positions plus a compound top setting:

```
low · medium · high · xhigh · ultracode
```

Setting it is entirely manual. In practice that means it never gets set: the user picks one
level (currently `effortLevel = "xhigh"` in `~/.claude/settings.json`) and leaves it there
forever, over-spending on trivial prompts and under-using workflow orchestration on the
tasks that would benefit from it.

The goal is for `skill-advisor` — which already inspects every prompt — to work out what
effort a task warrants and make that visible, without the user thinking about it.

Two originally-separate feature requests ("auto-ultracode" and "auto effort level") collapse
into this one, because **ultracode _is_ an effort level**, not a parallel mechanism. See
[Finding 1](#finding-1-ultracode-is-an-effort-level).

---

## Findings: what the harness actually permits

These were established by reading the compiled `claude` 2.1.220 binary. They are load-bearing
— the design is shaped by them — and expensive to re-derive, so they are recorded here.

> **Caveat.** These come from string and symbol extraction of a compiled binary, not from
> public API documentation. They are accurate for 2.1.220 and are not a stable contract.
> Anything depending on them needs a graceful-degradation path (see
> [Failure modes](#failure-modes)).

### Finding 1: ultracode is an effort level

```js
N6s(e, t) {
  let r = e.toLowerCase();
  if (r === "auto" || r === "unset") return { value: void 0 };
  if (r === "ultracode" && sZ(t)) return { value: "xhigh" };   // + dynamic-workflow flag
  ...
}
```

`/effort` accepts `low|medium|high|xhigh|ultracode|auto`. Ultracode resolves to `xhigh`
effort plus a standing dynamic-workflow-orchestration flag. It additionally requires dynamic
workflows to be enabled: *"Ultracode needs dynamic workflows enabled (see /config)."*

Note `auto` is **not** adaptive per-task — it means "use the model's default level". It does
not solve this problem.

### Finding 2: hooks cannot set effort or ultracode

```js
getUltracodeRequested(e) => e.getAppState().ultracode === true
getEffortValue(e)        => AppState().effortValue,
                            overridable only by a permissionLayer of kind:"effort"
```

Both live in **AppState**. AppState is mutated by slash commands, by `--settings` at launch,
or by the SDK control message `apply_flag_settings` — never by hooks. A `UserPromptSubmit`
hook can return only `additionalContext` (a string), `systemMessage`, `suppressOutput`, and
`decision`. There is no `updatedPrompt` field (zero occurrences in the binary).

**Consequence: the advisor cannot set effort. It can only recommend, display, and persist a
launch-time default.** Every part of this design follows from that.

### Finding 3: precedence chain

```
CLAUDE_CODE_EFFORT_LEVEL=…      highest — blocks everything
  "Not applied: CLAUDE_CODE_EFFORT_LEVEL=x overrides effort this session"
--effort <level> launch flag     creates a "launch-effort pin"
  "Not applied: the launch-effort pin holds effort at x this session.
   Run /effort <level> in an interactive terminal to release the pin."
--settings effortLevel           ← where this design writes
~/.claude/settings.json          ← the user's own saved default
```

Two consequences:

1. `--settings` **outranks** the user's own `settings.json`. Anything written to
   `claudeskill-settings.json` wins at every `claudeskill` launch. This is what makes
   write-back work — and what makes it dangerous without a veto path
   ([Finding 5](#finding-5-the-obvious-undo-does-not-work)).
2. The pin comes from the `--effort` **launch flag**, not from settings. Writing
   `effortLevel` into a settings file does **not** lock `/effort` out. The nudge and the
   write-back do not fight each other.

### Finding 4: ultracode is deliberately never persisted

`/effort <level>` saves normally — *"(saved as your default for new sessions)"* — but
ultracode is tagged *"(this session only)"* and is never written to settings.

**Decision: honour that.** The advisor recommends ultracode and never persists it. Persisting
it would invert an upstream default and start every session in the expensive mode.

### Finding 5: the obvious "undo" does not work

If the advisor writes `effortLevel` into `claudeskill-settings.json` and the user runs
`/effort xhigh` to undo it, that saves to `~/.claude/settings.json` — which `--settings`
outranks. The undo appears to work for the session and is silently reverted at the next
launch.

**This requires an explicit veto mechanism.** See [Write-back](#4-write-back-and-veto).

### Finding 6: the keyword badge table is closed

The input-box highlight the user sees on `ultrathink` / `ultracode` is a hardcoded
notification registry:

```
ultrathink-active        feedback     "Deeper reasoning requested for this turn"                 immediate
ultraplan-active         feedback     "This prompt will launch an ultraplan session…"            immediate
ultrareview-active       contextual   "Run /code-review ultra after Claude finishes…"            immediate
workflow-keyword-active               "Dynamic workflow requested for this turn"  + " to ignore" immediate
workflow-keyword-ignored              "Ultracode keyword ignored for this prompt" + " to undo"   immediate
```

Five fixed entries, matched by hardcoded regexes (`\bultrathink\b`). No settings key, plugin
surface, or hook output appends to it — searched, none found. **Custom keywords cannot be
highlighted in the input box.** Two channels remain open, and both are used here:
`systemMessage` (transient, per-prompt, all hooks) and `statusLine` (persistent).

### Finding 7: the statusline receives the live effort level

The `statusLine` command receives on stdin, every render:

```jsonc
"effort":  { "level": "low"|"medium"|"high"|"xhigh"|"max" },  // Optional — only when the model supports it
"thinking": { "enabled": boolean },
"model":   { "id": "string", "display_name": "string" },
"context_window": { "used_percentage": number, ... },
"rate_limits": { "five_hour": { "used_percentage", "resets_at" }, ... }
```

ANSI colour is explicitly supported: *"When using ANSI color codes, be sure to use `printf`.
Do not remove colors. Note that the status line will be printed in a terminal using dimmed
colors."*

Note the enum here tops out at `max`, and does **not** include `ultracode` — ultracode
surfaces as `xhigh`. The statusline therefore cannot distinguish live ultracode from live
xhigh, and must not claim to.

The observed and recommended enums are therefore **not the same set**:

| | Values |
|---|---|
| Observed (from the harness) | `low` `medium` `high` `xhigh` `max` |
| Recommended (by the classifier) | `low` `medium` `high` `xhigh` `ultracode` |

`max` is never recommended (the classifier has no basis for distinguishing it from `xhigh`),
and `ultracode` is never observed. Ordering for comparison treats
`low < medium < high < xhigh ≤ ultracode` and `xhigh < max`. **When observed is `max`, the
feature stays silent** — no nudge and no write-back contribution — because the user has
deliberately gone above anything the advisor knows how to recommend.

**This is the only surface that can observe live effort.** The `UserPromptSubmit` event
payload does not carry it. That asymmetry drives the sensor design in
[Section 2](#2-statusline-renderer-and-sensor).

---

## Design

Five components. Three are new modules, one extends an existing module, one is a rendered
shell script.

```
                    ┌──────────────────────────────────────────┐
   prompt ─────────▶│ UserPromptSubmit hook (hook.run)         │
                    │   ├─ existing: triage → match → inject   │
                    │   ├─ NEW: effort.classify()              │──▶ effort.json
                    │   └─ NEW: systemMessage nudge  ◀─────────┼──── observed-effort.json
                    └──────────────────────────────────────────┘            ▲
                                                                            │
                    ┌──────────────────────────────────────────┐            │
   every render ───▶│ statusline.sh  (shell + jq, no Python)   │────────────┘
                    │   reads: CC stdin JSON + effort.json     │
                    │   writes: observed-effort.json           │
                    │   renders: "xhigh → medium · opus · 34%" │
                    └──────────────────────────────────────────┘

                    ┌──────────────────────────────────────────┐
   session end ────▶│ baseline.py                              │──▶ claudeskill-settings.json
                    │   N consecutive disagreements → write    │──▶ baseline.json (provenance)
                    │   user veto → reset counter + cooldown   │
                    └──────────────────────────────────────────┘
```

### 1. Classifier (`effort.py`)

**Input:** prompt text, lifecycle state, matcher picks, parallelization verdict.
**Output:** `EffortRecommendation(level, reason, source)` or `None` (meaning "no opinion —
stay silent").

The classifier adds **no new subprocess**. Measured latency today is already p50 10.2 s /
p95 24.8 s per prompt, driven by the existing `claude -p` judge and parallelization
detector. The design rule is *add fields to existing round-trips, never add round-trips.*

Resolution ladder, first hit wins:

| Order | Source | Rule |
|---|---|---|
| 1 | `parallelization` | detector returned `parallel=true` → **ultracode**. This is already computed; it is the same question ("does this decompose into parallel subagents?") and today it only nudges toward `dispatching-parallel-agents`. |
| 2 | `judge` | when `use_judge = true`, extend the existing JSON schema with `"effort"`. Validated against the enum with the same hallucination guard as `picks`; an out-of-enum value is dropped and resolution falls through. |
| 3 | `phase` | lifecycle phase: `planning`/`review` → high, `implementation` → high, `complete` → low, `correction` → medium. |
| 4 | `heuristic` | prompt shape: word count, technical-keyword density, candidate count above `min_embedding_score`. |
| 5 | — | `None`. Suppress statusline arrow, nudge, and write-back contribution. |

Extending the judge schema:

```jsonc
{"picks": [...], "skip": false, "effort": "low|medium|high|xhigh|ultracode"}
```

**Backward compatibility:** a judge reply omitting `effort` must parse exactly as today.
The field is optional on the way in; absence falls through to rung 3.

### 2. Statusline (renderer and sensor)

Rendered at install time to `~/.config/skill-advisor/statusline.sh`; registered in
`claudeskill-settings.json` by `install.py`'s existing merge renderer. The user's real
`~/.claude/settings.json` is never touched, preserving the project's existing promise. The
`statusLine` slot there is currently empty, so there is nothing to conflict with.

**Must be shell + `jq`, not Python.** The statusline re-renders continuously; a ~400 ms
Python cold start in that loop is unacceptable. `doctor` gains a `jq` check; if `jq` is
absent the script prints nothing and exits 0.

Output:

```
xhigh · opus · ctx 34%              agreement — no arrow
xhigh → medium · opus · ctx 34%     disagreement — arrow, coloured by target level
```

Colours via `printf` ANSI: low=blue, medium=cyan, high=green, xhigh=yellow,
ultracode=magenta, max=yellow. The terminal dims them. `ultracode` can only ever appear on
the right of the arrow (it is never observed); `max` only on the left (it is never
recommended).

**Sensor role.** Because the statusline is the only component that can see live effort
([Finding 7](#finding-7-the-statusline-receives-the-live-effort-level)), it writes what it
observed to `~/.cache/skill-advisor/observed-effort.json` on each render. The hook reads
that file to decide whether the recommendation disagrees with reality. On the first prompt
of a session no observation exists yet — the nudge is suppressed rather than guessed.

### 3. Nudge (`hook.py`)

When `recommended != observed`, the hook returns `systemMessage` alongside its existing
`additionalContext`:

```
skill-advisor: this looks like xhigh work (5 independent tasks) — you're at medium.  /effort xhigh
```

Rate-limited to **once per (session, level-pair)** so a long session does not nag on every
prompt. `additionalContext` behaviour is unchanged; `systemMessage` is purely additive.

Because ultracode is indistinguishable from xhigh in the observed value
([Finding 7](#finding-7-the-statusline-receives-the-live-effort-level)), an ultracode
recommendation nudges whenever observed is not already `xhigh`, and phrases itself as a
keyword suggestion — typing `ultracode` trips the real built-in badge:

```
skill-advisor: this decomposes into 5 parallel tasks — consider the `ultracode` keyword.
```

### 4. Write-back and veto

Sticky and announced.

**Trigger.** Each `UserPromptSubmit` firing appends its recommendation to the existing
per-session state under `~/.cache/skill-advisor/sessions/<session_id>.json`. The session's
**modal** recommendation is finalised by the existing `Stop` handler (which already runs for
auto-advance) and appended to a rolling window in `baseline.json`. Sessions contributing
fewer than 3 recommendations are discarded as too thin to be meaningful.

The comparison baseline is the **effective launch value**: `effortLevel` from
`claudeskill-settings.json` if the advisor has written one, otherwise the sensor's first
observation for the session.

**Both branches are required.** A mid-session correction to this spec briefly claimed the
first observation alone suffices, reasoning that the `--settings` file determines what a
session launches at so the two must agree. That holds *across* sessions and fails *within*
one: after a write lands, `first_observation` still holds the pre-write launch value, so the
"target differs from launch" test keeps passing and `maybe_write` rewrites — and re-announces
— on every subsequent turn. The final whole-branch review measured four identical writes on
four consecutive turns. `current_written_level()` is the guard that closes this. When that value has
differed from the session modal for `write_back_after_sessions` consecutive qualifying
sessions, the advisor writes.

**Write.** Atomic: temp file → JSON round-trip validation → `rename`. The value is validated
against the enum before serialisation. `claudeskill-settings.json` is never left in a
partial state; an unparseable existing file aborts the write and logs.

**Provenance.** JSON cannot hold comments, so history goes to a sidecar
`~/.cache/skill-advisor/baseline.json`: previous value, new value, observation window,
session count, timestamp. Without this, `effortLevel: "high"` in that file is
indistinguishable a year later from something the user typed.

**Announcement.** The next launch's first hook firing emits:

```
skill-advisor moved your effort baseline xhigh → high (5 sessions of lighter work).
Run /effort xhigh to keep it there.
```

**Veto.** Required, not optional — see
[Finding 5](#finding-5-the-obvious-undo-does-not-work). When the sensor observes live effort
change *after* launch — i.e. a later observation differs from the session's first
observation — that is a user veto, regardless of whether the advisor had written anything:
reset the counter and suppress write-back for `veto_cooldown_sessions`. Without this,
"sticky + announced" silently becomes "sticky and unoverridable", because the documented
undo (`/effort xhigh`) is outranked by the very file the advisor writes.

**Ultracode is never written** ([Finding 4](#finding-4-ultracode-is-deliberately-never-persisted)).
A session whose modal recommendation is ultracode contributes `xhigh` to the write-back
window.

### 5. Configuration

```toml
[effort]
enabled = true                  # master toggle; false = no classify, no statusline, no write
statusline = true
nudge = true                    # systemMessage on disagreement
write_back = true
write_back_after_sessions = 5   # consecutive disagreeing sessions before writing
veto_cooldown_sessions = 10     # suppression window after a manual override
ultracode_nudge = true          # recommend ultracode; never persist it
```

Every sub-behaviour is independently disableable, matching the project's existing config
style. Defaults are conservative: with `enabled = false` the advisor behaves exactly as it
does today.

---

## Failure modes

Everything inherits the project's existing silent-on-error contract: any exception exits 0
and logs to `advisor.log`. Claude Code never sees a failure.

| Failure | Behaviour |
|---|---|
| `jq` missing | Statusline prints nothing, exits 0. `doctor` reports it. |
| Statusline raises | Exits 0 with empty output. It must never be able to break prompt rendering. |
| `effort.json` missing or stale | Statusline renders live effort only, no arrow. |
| `observed-effort.json` missing (first prompt) | Nudge suppressed. No guessing. |
| Model without effort support | `effort` key absent from statusline stdin → render model/context only, suppress the whole feature for that session. |
| `claudeskill-settings.json` unparseable | Write-back aborts, logs, leaves the file untouched. |
| Judge returns an out-of-enum effort | Dropped by the hallucination guard; falls through the ladder. |
| Harness internals change (2.1.221+) | Statusline degrades to no-arrow; classifier still works; write-back still valid (it uses only the documented `effortLevel` settings key). |

The last row matters: only [Finding 7](#finding-7-the-statusline-receives-the-live-effort-level)
is genuinely fragile. The rest of the design rests on documented surfaces
(`statusLine`, `systemMessage`, `effortLevel`).

---

## Testing

Follows the existing `isolated_paths` / `fake_claude_home` fixture style — no test touches
the real `~/.claude`.

- **Classifier:** each ladder rung in isolation; ladder fall-through when a rung returns
  nothing; out-of-enum judge value dropped; judge reply *without* `effort` parses as today.
- **Statusline:** synthetic Claude Code stdin → asserted rendered string and exit code.
  Cases: agreement, disagreement, **`effort` key absent**, malformed JSON, `jq` missing.
- **Sensor:** `observed-effort.json` written on render; hook suppresses the nudge when it is
  absent.
- **Nudge:** fires once per (session, level-pair); `additionalContext` unchanged when the
  nudge is suppressed.
- **Enum asymmetry:** observed `max` silences nudge *and* write-back contribution;
  `ultracode` never appears as an observed value; `max` never appears as a recommendation.
- **Write-back:** N−1 sessions → no write; N → write plus announcement queued; sessions with
  fewer than 3 recommendations excluded from the window; unparseable settings → abort with
  file unchanged; atomicity (interrupted write leaves the original intact).
- **Veto:** mid-session override resets the counter and starts the cooldown; write-back
  stays suppressed for the full window.
- **Provenance round-trip:** the advisor never writes a level it did not compute — assert
  the written value is traceable to a recorded recommendation.

---

## Deliberately cut

- **`min_level` / `max_level` band.** Considered and rejected as redundant with sticky
  write-back plus veto. Reintroduce only if drift is observed in practice.
- **Persisting ultracode.** [Finding 4](#finding-4-ultracode-is-deliberately-never-persisted).
- **A dedicated classifier subprocess.** Would double an already-painful latency floor to
  buy what the existing judge call can return for free.
- **Custom input-box keyword highlighting.** Not possible —
  [Finding 6](#finding-6-the-keyword-badge-table-is-closed).

---

## Out of scope

Catalog refresh and skill rotation is a **separate spec**. It is not blocked by this work,
but it carries a known defect worth recording here so it is not lost:

`catalog.py` scans `SKILL.md` from disk and never reads `skillOverrides` from
`settings.json`. Measured 2026-07-29 against `advisor.events.jsonl` (8,403 events at the
time of measurement — the log is append-only and live, so re-running this will give larger
absolute counts) and the then-current `skillOverrides`:

```
skill picks total:                      3,955
  -> pointing at a DISABLED skill:      2,328   (58.9%)

worst offenders:
  plan-writing      488     ← the #2 most-recommended skill overall
  iterate-pr        116
  conductor-status  106
  pr-creator         93
  imagen             76
```

Nearly six in ten recommendations name a skill Claude Code cannot invoke — a likely major
contributor to the 34% ingestion rate. There are also two independent, drifted "off" lists:
889 entries in `skillOverrides` and ~400 names in `config.toml`'s `exclude_names`.
