# `skillOverrides` via `--settings` — verification gate result

**Date:** 2026-07-30
**Verdict:** **PASSES — and it merges per-key.** The rotation design proceeds as written.
**Plan:** `docs/superpowers/plans/2026-07-30-catalog-refresh.md`, Task 8.

The catalog-refresh plan made this a hard stop: *"Do not proceed past task 1 on an
assumption."* Phase C (rotation) writes `skillOverrides` into the advisor's own
`--settings` file, and the entire mechanism rests on Claude Code honouring it.

---

## Method

Four settings files, each passed as `claude --settings <file> -p ...`, asking the
session to write the exact list of skill names available to its Skill tool.

**The answer had to be written to a file, not returned as the reply.** The first
three attempts failed: a nested `claude -p` inherits the caller's hooks, and this
repo's `Stop` hook forces an extra turn, so `-p`'s final message was the nested
session's reply *to the hook* rather than the answer. Adding `"hooks": {}` to the
`--settings` file did **not** suppress them — itself a data point for merge
semantics. Writing to a file makes the result independent of whatever the last
message happens to be.

---

## Results

| # | `skillOverrides` | Skills exposed | Observation |
|---|---|---|---|
| A | *(none)* | **107** | baseline; `plan-writing` absent (it is `off` in `~/.claude/settings.json`) |
| B | `{"plan-writing": "on"}` | **108** | `plan-writing` **present** |
| C | `{"superpowers:brainstorming": "off"}` | **107** | nothing disappeared |
| D | `{"brainstorming": "off"}` | **106** | only `brainstorming` gone; `superpowers:brainstorming` survived |

### Q1 — Is `skillOverrides` honoured via `--settings`? **Yes.**

B enabled a skill that the user's own `settings.json` has `off`. The assumption
the whole design rested on is confirmed empirically.

### Q2 — Merge or replace? **Merge, per key.**

B produced exactly **+1** skill (107 → 108). The other 106 remained. A wholesale
replacement would have left only `plan-writing`.

This is the good branch of the plan's three-way fork. The rotation writes a
**delta**, not the complete ~727-entry map — a smaller and far less dangerous
write. The plan's contingency for replace-semantics ("the advisor must write the
complete map … a larger file and a more dangerous write") is not needed.

### Q3 — How are plugin skills keyed? **They are not addressable at all.**

Added to the gate's scope after Task 5 measured that **15 directory names exist in
both a user skill and a plugin-cache skill** (`brainstorming`,
`executing-plans`, `frontend-design`, `requesting-code-review`, …), while
`overrides.override_key()` returned an identical key for both.

- **C** proves the namespaced form (`superpowers:brainstorming`) is silently
  **ignored** — accepted by the file format, no effect.
- **D** proves a bare directory name addresses the **user** skill only. The
  plugin skill of the same directory name was unaffected.

So `skillOverrides` governs user skills, by bare directory name, and plugin
skills are outside its reach.

---

## Consequence: `override_key` was wrong

Because `override_key` returned the bare directory name for plugin entries too,
a plugin skill inherited any `off` set on a same-named user skill. Measured — three
skills that Claude Code exposes were being marked disabled and withheld from
recommendations:

```
plugin-dev:mcp-integration            (dir 'mcp-integration' is off)
superpowers:receiving-code-review     (dir 'receiving-code-review' is off)
superpowers:writing-skills            (dir 'writing-skills' is off)
```

**Fix:** `override_key` returns `None` for plugin-namespaced entries, so they
always resolve to enabled — matching observed behaviour.

**Corollary for the write side:** a namespaced key is accepted by the file and
does nothing. Any future code that writes one produces a silent no-op — a
`rotate --apply` that reports success and changes nothing. Worth guarding at the
point of write, not just of read.

---

## Why the gate earned its place

Had this been assumed rather than measured, the rotation would have been built on
a key space that partly does not exist. `rotate --dry-run` would have printed
sensible proposals, `--apply` would have written the file successfully, and the
active set would never have changed — a failure with no error, discoverable only
by noticing weeks later that nothing rotated.
