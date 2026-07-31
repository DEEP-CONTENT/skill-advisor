# Catalog refresh & skill rotation — design

**Date:** 2026-07-29
**Status:** approved, ready for implementation planning
**Scope:** one sub-project. Latency reduction is a separate spec
(`2026-07-29-latency-design.md`); the two are independent and can ship in either order.

---

> **Status 2026-07-30: implemented.** See
> `docs/superpowers/plans/2026-07-30-catalog-refresh.md` for the task-by-task
> implementation and `docs/superpowers/notes/2026-07-30-skilloverrides-verification.md`
> for the verification-gate result. The plan's "Audit corrections" table is
> **authoritative wherever it disagrees with this document.** Beyond that
> table, five things were learned during implementation that this document
> does not know about:
>
> 1. **"`skillOverrides` becomes the single source of truth" (§2,
>    "`exclude_names` is migrated and deleted") is false as written.**
>    `catalog.exclude_names` mutes skills, subagents, and slash commands
>    alike; `skillOverrides` can only address user skills. `migrate-excludes`
>    is therefore **partial by design**: it migrates only names that resolve
>    to a `skillOverrides`-addressable catalog entry and leaves the rest in
>    `exclude_names`. Measured on the author's machine: of 428 excluded
>    names, 417 classify as migratable and 11 are retained — 8 match nothing
>    on disk, 3 are built-in subagents (`Explore`,
>    `feature-dev:code-architect`, `feature-dev:code-explorer`) that
>    `skillOverrides` has no key for.
> 2. **The verification gate below passed, and answered more than it
>    asked.** `skillOverrides` via `--settings` works and merges per key, so
>    `rotate --apply` writes a delta rather than the complete pool-sized map
>    — the gate's replace-semantics contingency was not needed. The gate
>    also proved plugin skills are **not addressable via `skillOverrides` at
>    all**: a namespaced key (`plugin:skill`) is silently accepted by the
>    settings file and ignored by Claude Code.
> 3. **`invocation_rate` is weighted 0.0 in v1** (see the corrected Scoring
>    table below) — the telemetry that produces it only started shipping on
>    this branch, so it has no history to score against yet.
> 4. **The pool is 727 parseable names, not 947.** 216 of 947 `SKILL.md`
>    files on disk carry no parseable YAML frontmatter and are invisible to
>    `catalog.scan()`; `skill-advisor doctor` now reports this split.
> 5. **Only user skills are rotatable.** Subagents, slash commands, and
>    plugin skills are permanently enabled and outside `skillOverrides`'s
>    reach, so `rotate` excludes them from its pool entirely (and reports
>    how many it excluded) rather than scoring entries it could never
>    promote or demote.

---

## Problem

Two problems, one root cause: `skill-advisor` has no idea which skills Claude Code can
actually invoke.

### Problem 1 — the advisor recommends skills that cannot be used

`catalog.py` scans `SKILL.md` files from disk and never reads `skillOverrides` from
`settings.json`. Measured against the live event log and live settings on 2026-07-29:

```
skill picks total:                      3,981
  -> pointing at a DISABLED skill:      2,334   (58.6%)

worst offenders:
  plan-writing        490     ← the #2 most-recommended skill overall
  iterate-pr          116
  conductor-status    106
  pr-creator           95
```

Nearly six in ten recommendations name a skill the model cannot invoke. The ingestion
rate — prompts where the advisor recommended something and a Skill was actually invoked —
sits at **34.2%**, and this is a large part of why.

The precise cause is list drift. Two hand-maintained lists exist and have diverged:

| | Count |
|---|---|
| Skills on disk with a `SKILL.md` | 947 |
| `skillOverrides` set to `off` in `~/.claude/settings.json` | 889 |
| → enabled, i.e. actually invocable | **62** |
| `catalog.exclude_names` in `config.toml` | 428 |
| In both lists | 406 |
| **`off` but NOT excluded → advisor still recommends** | **483** |
| Excluded but enabled → muted in advisor, invocable in Claude Code | 11 |

Those 483 entries are the bug.

### Problem 2 — the active set is curated by hand and never revisited

62 of 947 skills are enabled. That ratio is right — Claude Code's skill list is injected
into context, so a thousand entries is not viable — but the *selection* is a snapshot of
whatever seemed useful when it was made. Nothing revisits it as the work changes.

The goal is for the advisor to maintain the active set itself: keep roughly 50–100 skills
enabled, retire ones that never get used, and promote ones that fit current work.

---

## Findings

### Finding 1 — `skillOverrides` appears to be a mergeable settings key

`skillOverrides` is read through the same settings machinery as `statusLine` and
`effortLevel`:

```js
skillOverrides,n=Pr(…)          // Pr is the settings reader, cf. Pr("policySettings")
ur.skillOverrides!==Bc.skillOverrides   // change-detection against previous settings
```

Both `statusLine` and `effortLevel` are proven to work when supplied via `--settings` (the
effort-signalling work depends on exactly that). This is strong circumstantial evidence
that `skillOverrides` supplied via `--settings` is honoured too.

> **This is an assumption, not a verified fact, and the whole design rests on it.**
> Implementation task 1 must verify it empirically before anything else is built. See
> [Verification gate](#verification-gate).

### Finding 2 — a disabled skill can still be scored

The obvious objection to rotation is a feedback trap: a disabled skill is invisible to the
model, so it is never invoked, so it scores dead, so it stays disabled forever. Usage data
alone can only ever confirm the existing selection.

The escape is that **relevance does not require usage**. Every skill in the pool already
has an embedding (`embeddings.npz`, 384-dim, aligned with `catalog.json`). A disabled
skill's fit can be measured against a sketch of the user's actual work without the skill
ever having been active. See [Scoring](#3-scoring).

---

## Design

### 1. Two sets, not one

The central correction. The catalog stops being a single flat list and carries a state:

| Set | Contents | Used for |
|---|---|---|
| **Pickable** | skills whose `skillOverrides` value is not `off` | what `matcher` may recommend |
| **Rotation pool** | every skill on disk, enabled or not | what `rotate` may promote or retire |

`catalog.scan()` continues to walk every parseable skill — 727 of the 947 `SKILL.md`
files on disk; the rest carry no parseable YAML frontmatter and are invisible to the
scanner (see the status banner above) — and gains an `enabled: bool` field per entry,
resolved from `skillOverrides`. **Two fix sites, not one:** the matcher filters to
`enabled == True`, and so does `lifecycle.pick_candidates_for_phase()` — the
hardcoded per-phase preference list, which turned out to be the single largest source
of un-invocable recommendations (490 of them from one entry, `plan-writing`, alone).
The rotation operates over the whole pool.

This is what fixes the 58.6% without sacrificing the rotation's ability to reach back into
the disabled majority.

### 2. `exclude_names` is migrated and deleted

`skillOverrides` becomes the single source of truth. A one-time `skill-advisor migrate-excludes`
command:

1. reads the 428 `exclude_names` entries,
2. writes `off` for each into the advisor's own settings file,
3. rewrites `config.toml` with `exclude_names = []` and a comment pointing at the new mechanism,
4. backs up both files first, and prints a revert command.

**Accepted consequence, chosen deliberately:** the 428 previously-muted skills become
rotation-eligible. Some were muted for irrelevance and may be promoted back. The migration
is therefore backed up and reversible, and the first `rotate --dry-run` after migrating
should be read carefully rather than applied blind.

### 3. Scoring

Each skill in the pool gets a score from three signals:

| Signal | Source | Applies to | v1 weight |
|---|---|---|---|
| `invocation_rate` | telemetry: picks followed by an actual Skill invocation in the same session | enabled skills only | **0.0** |
| `pick_rate` | telemetry: how often the matcher chose it | enabled skills only | 0.4 |
| `semantic_fit` | cosine of the skill's embedding against the nearest prompt centroid | **every skill, enabled or not** | 0.6 |

`invocation_rate` is computed and reported but weighted zero in v1: the telemetry that
produces it (which skill a `Skill` tool call actually invoked, not just that some skill
was invoked) only started being captured on this branch, so it has no history yet to
score against. Promote its weight once weeks of data exist. `semantic_fit` is what
makes promotion possible today.

#### The centroid sketch

Prompt text is hashed in telemetry and unrecoverable, so past prompts cannot be
re-embedded. Instead the advisor maintains a small online sketch:

```
~/.cache/skill-advisor/centroids.npz    8 × 384 float32   (~12 KB, fixed size)

per prompt:      embed → find nearest centroid → nudge it toward the prompt
per rotation:    score(skill) = max_i cosine(skill_embedding, centroid_i)
```

Eight centroids rather than one, because the work is genuinely multi-modal — Kubernetes,
frontend, Python services, and documentation are different regions of embedding space and a
single mean would sit in the middle of all of them, describing none.

**Privacy:** no prompt text and no per-prompt vectors are stored. The file is fixed-size and
does not grow. It is a lossy aggregate of thousands of prompts, not a record of any one of
them. It is written only when `telemetry.events_enabled` is already true, and
`skill-advisor uninstall` removes it.

Cold start: with fewer than a configurable number of prompts observed, `semantic_fit` is
suppressed and rotation refuses to run rather than acting on an unformed sketch.

### 4. Rotation

```
skill-advisor rotate              # dry run: proposed swaps, scores, reasons
skill-advisor rotate --apply      # write them
```

Target active-set size is configurable (default 75, within the 50–100 band). The rotation
selects the top-scoring N from the pool, subject to:

- **Hysteresis.** A swap requires a score margin, not a bare ordering difference. Without
  it, near-tied skills thrash in and out on every run and the active set is never stable.
- **An exploration slice.** A fixed fraction of the active set (default 10%) is reserved
  for high-`semantic_fit` skills with no usage history. This is the deliberate cost of
  discovering skills the usage data cannot recommend, and it is what makes the design more
  than a leaderboard.
- **Never demote a skill used recently**, regardless of score, within a recency window.

Dry-run output must show, per proposed change, the score, which signal drove it, and — for
a demotion — when it was last actually invoked.

**Cadence:** manual first. An opt-in automatic mode (rotate every N sessions, announced via
`systemMessage`, as the effort baseline does) sits behind a config flag and is not enabled
by default. The scoring model is unvalidated; a wrong rotation silently removes a skill the
user relies on, which is more disruptive than a wrong effort level. Automation is earned
after the manual command has demonstrably produced sane proposals against real data.

### 5. Where the write goes

Into the advisor's own `--settings` file — `paths.settings_file()`, honouring
`SKILL_ADVISOR_SETTINGS_FILE` — never `~/.claude/settings.json`. This preserves the
project's standing promise that the user's own settings file is never modified.

The write reuses the machinery the effort write-back already established: read-modify-write,
serialise, round-trip validate, temp file, atomic `replace()`, provenance in a sidecar. The
same documented race with `install.render_settings()` applies and the same reasoning holds.

---

## Verification gate

**Implementation task 1 does nothing but answer this question:** does `skillOverrides`
supplied via `--settings` actually control which skills Claude Code exposes, and how does it
compose with the copy in `~/.claude/settings.json` — merge, or replace?

Concretely: write a `skillOverrides` block enabling a currently-disabled skill into a test
`--settings` file, launch Claude Code with it, and confirm the skill appears in the skill
list and is invocable.

- If it works and **merges per-key**, the design proceeds as written.
- If it works but **replaces wholesale**, the advisor must write the complete map (all 947
  entries), not a delta — a larger file and a more dangerous write, but workable.
- If it is **ignored entirely**, the rotation half of this spec is dead as designed and the
  decision returns to the user: write `~/.claude/settings.json` directly with safeguards, or
  fall back to propose-only. **Do not proceed past task 1 on an assumption.**

Problem 1's fix — the pickable/pool split — is independent of this gate and can ship
regardless.

---

## Failure modes

| Failure | Behaviour |
|---|---|
| `skillOverrides` missing or malformed in settings | Treat every skill as enabled; log. Degrades to today's behaviour, never to an empty catalog. |
| `centroids.npz` missing or wrong shape | `semantic_fit` suppressed; rotation refuses to run and says why. |
| Fewer than the minimum observed prompts | Rotation refuses to run. |
| Rotation would empty the active set | Refuse and abort. A floor on the active-set size is enforced before any write. |
| Settings file unparseable | Abort the write, leave the file byte-identical — same contract as the effort write-back. |
| Telemetry disabled | `invocation_rate` and `pick_rate` unavailable; rotation runs on `semantic_fit` alone, and says so in the dry run. |

Everything inherits the project's silent-on-error contract on hook paths. The `rotate`
command is a CLI verb, not a hook, and *should* fail loudly with a clear message.

---

## Testing

- **The 58.6% regression, measured.** A test that builds a catalog against a fixture where
  some skills are `off`, runs the matcher, and asserts zero picks name a disabled skill.
  This is the bug; it needs a test that fails against today's code.
- **Pool vs pickable.** A disabled skill is absent from matcher output and present in the
  rotation pool, in the same fixture.
- **Centroid sketch.** Fixed size across many updates; nearest-centroid assignment is
  stable; a skill semantically close to an established centroid outscores an unrelated one.
- **Rotation.** Hysteresis prevents thrash on near-ties across consecutive runs; the
  exploration slice actually promotes a zero-usage skill; a recently-used skill is never
  demoted; the active-set floor is enforced.
- **Dry run writes nothing.** Assert the settings file is byte-identical after `rotate`
  without `--apply`.
- **Migration.** `exclude_names` entries land in `skillOverrides`, `config.toml` is
  emptied, backups exist, and the revert command restores both exactly.

**Test the real sequence, not the units.** The effort work's worst defect was a dead code
path that 390 unit tests reported as healthy, because the tests called functions in an order
production never produces. At least one test here must drive the full loop: observe prompts
→ update centroids → score → propose → apply → rebuild catalog → confirm the matcher's
pickable set changed accordingly.

---

## Deliberately out of scope

- **Automatic rotation on by default.** Behind a flag, off, until the scoring proves itself.
- **Per-project active sets.** Genuinely useful — a Kubernetes repo and a frontend repo want
  different skills — but it multiplies the state and the failure modes. Revisit once
  single-set rotation works.
- **Rotating subagents or slash commands.** Skills only; the others are far fewer and not
  the problem.
- **Fixing the 34.2% ingestion rate as a target.** This work should move it, and that
  movement is the honest measure of whether the spec succeeded — but ingestion is affected
  by much else and is not a number to optimise directly.
