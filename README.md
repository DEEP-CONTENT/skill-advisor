<div align="center">

# skill-advisor

**Auto-route every Claude Code prompt to the right skill, subagent, or slash command — silently, locally, with zero API keys.**

[![CI](https://github.com/deep-content/skill-advisor/actions/workflows/ci.yml/badge.svg)](https://github.com/deep-content/skill-advisor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-261%20passing-brightgreen.svg)](#development)
[![Hook latency](https://img.shields.io/badge/latency-~0.3s%20warm-success.svg)](#latency-and-the-two-matcher-modes)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

[**Quickstart**](#quickstart) • [**How it works**](#how-the-advisor-works) • [**Configuration**](#configuration-reference) • [**Architecture**](#architecture-deep-dive) • [**Lifecycle mode**](#lifecycle-mode) • [**Effort signalling**](#effort-signalling) • [**Telemetry**](#telemetry-and-usage-reports)

</div>

---

## Overview

`skill-advisor` is a silent `UserPromptSubmit` hook for Claude Code. It inspects every prompt you type, matches it against your installed skill, subagent, and slash-command library, and injects the best picks into the model's context — so the right skill fires automatically, without you lifting a finger.

The advisor is **bring-your-own-library**: it reads skills from `~/.claude/skills/` (plus plugin-marketplace skills and any extra roots you configure) at runtime. This repo ships the runtime only; you supply the skills.

No API keys are ever introduced. All LLM calls route through `claude -p` subprocesses, and embeddings run locally via [`fastembed`](https://github.com/qdrant/fastembed).

## Highlights

- **Silent and automatic** — fires on Claude Code's `UserPromptSubmit` hook; never interrupts your workflow.
- **Zero API keys, fully local** — embeddings via `fastembed` (BGE-small ONNX, ~15 MB); optional LLM judge piggy-backs on your existing `claude -p`.
- **Bring your own library** — scans `~/.claude/skills/` plus plugin marketplaces and any extra roots you configure. No skills bundled.
- **Lifecycle aware** — drives a `planning → implementation → review → correction → complete` state machine for substantive tasks.
- **Effort aware** — recommends a reasoning-effort level per prompt, shows it in a status line,
  nudges when it disagrees with your live setting, and slowly tunes your launch default behind
  a user veto. Adds no new subprocess.
- **Hallucination-guarded** — LLM-judge picks are intersected with the candidate shortlist; unknown names are dropped.
- **Fail-safe by design** — any error or budget overrun exits silent so your prompt always reaches the model.
- **Opt-in telemetry** — local JSONL with hashed prompts; `skill-advisor report` surfaces dead-skill detection, latency, and ingestion rate.

---

## Table of contents

1. [What's in this repo](#whats-in-this-repo)
2. [How the advisor works](#how-the-advisor-works)
3. [Quickstart](#quickstart)
4. [Installation (detailed)](#installation-detailed)
5. [Using your own skill library](#using-your-own-skill-library)
6. [Everyday use](#everyday-use)
7. [CLI reference](#cli-reference)
8. [Configuration reference](#configuration-reference)
9. [Latency and the two matcher modes](#latency-and-the-two-matcher-modes)
10. [Architecture deep dive](#architecture-deep-dive)
11. [Lifecycle mode](#lifecycle-mode)
12. [Effort signalling](#effort-signalling)
13. [Telemetry and usage reports](#telemetry-and-usage-reports)
14. [Troubleshooting](#troubleshooting)
15. [Maintainer workflow](#maintainer-workflow)
16. [Contributing](#contributing)
17. [Development](#development)
18. [File layout](#file-layout)
19. [Uninstalling](#uninstalling)
20. [Licensing notes](#licensing-notes)

---

## What's in this repo

```
skill-advisor/
├── src/skill_advisor/     Python package — the advisor runtime & CLI
├── tests/                 pytest suite (261 tests, all pass, zero real-home leaks)
├── examples/
│   ├── config.toml        Commented default user config
│   ├── claudeskill-settings.json  Reference UserPromptSubmit hook settings
│   └── prompts.jsonl      Calibration prompts for `skill-advisor replay`
├── .github/workflows/     CI (Linux + macOS, Python 3.11/3.12)
├── Makefile, pyproject.toml, README.md, INSTALL.md, LICENSE
```

The Python package is installed globally via `uv tool install .` and exposes a single
CLI: `skill-advisor`. The wiring that activates it inside Claude Code is a per-user
`claudeskill` shell alias written by `skill-advisor install`.

---

## How the advisor works

```
   your prompt
        │
        ▼
   ┌────────────────────────────┐
   │   UserPromptSubmit hook    │   triggered by Claude Code before the model sees the prompt
   │   (bin: skill-advisor hook)│
   └────────────────────────────┘
        │
        ▼
   triage — skip cheap prompts ("thanks", "/help", "yes") in ~0 ms
        │ (substantive prompts continue)
        ▼
   load catalog + embeddings from ~/.cache/skill-advisor/
        │
        ▼
   embed the prompt locally (BGE-small via fastembed, ~50-200 ms)
        │
        ▼
   cosine top-K over the catalog of skills + subagents + slash commands
        │
        ▼  (two modes, chosen via config.toml)
        │
   ┌────┴────────────────────────────────────┐
   │                                         │
   │  embedding-only (default, ~0.3 s warm)  │
   │  use cosine ranking as the final picks  │
   │                                         │
   │  OR                                     │
   │                                         │
   │  LLM judge (opt-in, ~7-12 s)            │
   │  claude -p (Haiku) re-ranks the         │
   │  shortlist, hallucination-guarded       │
   │                                         │
   └─────────────────────┬───────────────────┘
                         │
                         ▼
   emit JSON hookSpecificOutput.additionalContext
                         │
                         ▼
   Claude Code injects it into the model's context, which then invokes
   the recommended Skill / Agent / Command
```

Guarantees:

- **Never fails your prompt.** Any error or budget overrun exits 0 silent; Claude Code
  still submits your prompt normally.
- **No external services.** Embeddings are fully local. The optional LLM judge runs via
  your existing `claude -p` install.
- **Opt-in.** Wiring lives in `~/.config/skill-advisor/claudeskill-settings.json` and a
  `claudeskill` alias. Your `~/.claude/settings.json` is never touched.
- **Hallucination-guarded.** LLM-judge picks are validated against the candidate list
  (names not in the shortlist are rejected).

---

## Quickstart

```bash
git clone https://github.com/deep-content/skill-advisor.git && cd skill-advisor

uv tool install .                    # installs skill-advisor to ~/.local/bin
skill-advisor install                # render settings file + build catalog (no shell edits)

# Activate by passing --settings to claude. Pick ONE of:
#
# (a) One-off:
claude --settings ~/.config/skill-advisor/claudeskill-settings.json
#
# (b) Add --settings to your existing claudeskill wrapper (recommended if you
#     already have one that sets CLAUDE_CONFIG_DIR for work/private credentials):
#     alias claudeskill='CLAUDE_CONFIG_DIR=~/.claude-work claude --settings ~/.config/skill-advisor/claudeskill-settings.json'
#
# (c) Let the installer write a fresh alias for you (safe only if no existing claudeskill):
skill-advisor install --write-alias
source ~/.zshrc
```

First run of `skill-advisor install` downloads the BGE-small ONNX model (~15 MB) into
the `fastembed` cache (`~/.cache/fastembed`).

---

## Installation (detailed)

### Prerequisites

```bash
claude --version         # Claude Code must be installed and on PATH
python3 --version        # 3.11 or newer
which uv || which pipx   # one of these
```

Install `uv` if missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
Install `pipx` if missing: `python3 -m pip install --user pipx && pipx ensurepath`.

### 1. Install the package

```bash
git clone https://github.com/deep-content/skill-advisor.git
cd skill-advisor
uv tool install .        # or: pipx install .
skill-advisor --help     # confirm the CLI is on PATH
```

### 2. Bring your own skill library (optional)

The advisor recommends from whatever Claude Code can already invoke under
`~/.claude/skills/` (and your plugin-marketplace skills). This repo does **not**
ship a skill bundle — you supply your own. See
[Using your own skill library](#using-your-own-skill-library) for the full picture.

If you maintain a directory of `SKILL.md` folders elsewhere (a private team repo,
a shared dotfiles checkout, etc.), `skill-advisor sync-skills` will copy them into
your Claude home:

```bash
skill-advisor sync-skills --from /path/to/your/skill-library
skill-advisor sync-skills --from /path/to/your/skill-library --dry-run
skill-advisor sync-skills --from /path/to/your/skill-library --force
```

**Non-destructive by default.** Any skill already in `~/.claude/skills/` is left
alone; `--force` overwrites. The source must contain top-level subdirectories
each with a `SKILL.md` file.

Source lookup order if `--from` is omitted:

1. `$SKILL_ADVISOR_BUNDLE/skills`
2. `./skills` (when run from a directory containing a `skills/` folder)

### 3. Wire the advisor into Claude Code

```bash
skill-advisor install
```

One command — idempotent, re-running is a no-op. By default it does NOT edit your
shell rc file; it only:

1. Renders `~/.config/skill-advisor/claudeskill-settings.json` with the absolute path
   to your installed `skill-advisor` binary (no repo-relative paths leak).
2. Writes a default `~/.config/skill-advisor/config.toml` (skipped if it already exists).
3. Builds the catalog + embeddings (first run triggers the model download).
4. Detects any existing `claudeskill` wrapper in your login shell and prints the exact
   `--settings` flag you should add to it.
5. Warns if `CLAUDE_CONFIG_DIR` is set, confirming which Claude home it resolves to.

You activate the advisor by passing `--settings ~/.config/skill-advisor/claudeskill-settings.json`
to `claude`. See [Activating the advisor](#activating-the-advisor) below.

Flags:

- `--no-build` — skip the first catalog build (run it manually with `skill-advisor build`).
- `--write-alias` — also append a fenced `claudeskill` alias to your shell rc file:
  ```
  # >>> skill-advisor alias >>>
  alias claudeskill='claude --settings "$HOME/.config/skill-advisor/claudeskill-settings.json"'
  # <<< skill-advisor alias <<<
  ```
  **Only use this if you don't already have a custom `claudeskill` wrapper.** A naïve alias
  will shadow any wrapper that sets `CLAUDE_CONFIG_DIR` or similar environment variables.

### Activating the advisor

Pick the option that matches your setup.

#### (a) One-off / per-invocation

```bash
claude --settings ~/.config/skill-advisor/claudeskill-settings.json
```

#### (b) You already have a `claudeskill` wrapper (work/private credential split)

Common pattern (matches `CLAUDE_CONFIG_DIR=~/.claude-work claude`):

```bash
# Patch the existing alias/function to include --settings:
alias claudeskill='CLAUDE_CONFIG_DIR=~/.claude-work claude --settings ~/.config/skill-advisor/claudeskill-settings.json'
```

The `--settings` flag is merged with your user settings chain; it does NOT touch
`CLAUDE_CONFIG_DIR`, credentials, existing hooks, permissions, or MCP servers.

#### (c) You want the installer to write a fresh alias

```bash
skill-advisor install --write-alias
source ~/.zshrc   # or your shell's rc
claudeskill       # Claude Code with the advisor active
```

Only do this if `which claudeskill` returns nothing and you don't have a shell alias
named `claudeskill` elsewhere.

### 4. Reload and verify

```bash
source ~/.zshrc              # or your shell's rc
skill-advisor doctor         # end-to-end diagnosis
claudeskill                  # drops you into Claude Code with the advisor wired up
tail -f ~/.cache/skill-advisor/advisor.log   # watch the hook fire
```

---

## Using your own skill library

The advisor does **not** ship any skills. It scans the disk at build time and
matches against whatever is there. The catalog sources are scanned in this
order, with earlier entries winning on duplicate `(kind, name)`:

1. `$CLAUDE_HOME/skills/*/SKILL.md` — user-installed skills
2. `$CLAUDE_HOME/plugins/marketplaces/*/plugins/*/skills/*/SKILL.md` — plugin skills
3. Built-in subagents + slash commands (hardcoded in `builtins.py`)
4. Any directory listed in `config.catalog.extra_roots` — your team-internal skills

`$CLAUDE_HOME` resolves to `$CLAUDE_CONFIG_DIR` if set, else `~/.claude`. See
[Work / private credential splits](#work--private-credential-splits) for the
multi-home setup.

### Seeding from an external bundle

If you maintain a private bundle of `SKILL.md` folders (a team dotfiles repo,
a shared NFS mount, a colleague's checkout), `skill-advisor sync-skills` will
copy them into your Claude home:

```bash
skill-advisor sync-skills --from /path/to/your/skill-library
skill-advisor sync-skills --from /path/to/your/skill-library --dry-run
skill-advisor sync-skills --from /path/to/your/skill-library --force
```

| Local state | Default behavior | `--force` |
|---|---|---|
| Skill dir does not exist | Copy it | Copy it |
| Skill dir exists | Skip (report it) | Replace |

The source directory must contain top-level subdirectories each with a `SKILL.md`
file; everything else (root-level files, dotfiles, non-skill subdirs) is ignored.

If `--from` is omitted, the lookup order is `$SKILL_ADVISOR_BUNDLE/skills` →
`./skills`. The repo's own `skills/` directory is gitignored, so you can
maintain a working mirror there without polluting commits.

### Scanning team-internal skills in place

If your team-internal skills already live on disk somewhere and you'd rather
scan them in place than copy, add the path to `config.toml`:

```toml
[catalog]
extra_roots = ["/path/to/team/skill-library"]
```

The advisor recursively globs for `SKILL.md` under each `extra_roots` entry on
the next `skill-advisor build`.

### Plugin-namespaced skills

Skills under `~/.claude/plugins/marketplaces/**/plugins/*/skills/*/SKILL.md`
come from plugin packages with their own install mechanisms (MCP servers, slash
commands, tool registrations). Install plugin packs the normal way and the
advisor picks them up automatically; you don't need `sync-skills` for these.

---

## Everyday use

```bash
claudeskill                              # launch Claude Code with the advisor active
claude                               # plain Claude Code — advisor inactive

skill-advisor sync-skills --from PATH # copy skills from an external library into ~/.claude/skills/
skill-advisor sync-skills --from PATH --force  # overwrite local versions
skill-advisor build                  # rebuild the catalog + embeddings (after new skills)
skill-advisor build --force          # rebuild even if the catalog hash is unchanged
skill-advisor doctor                 # diagnose install state (claude on PATH, catalog, etc.)
skill-advisor replay examples/prompts.jsonl   # benchmark the pipeline on a prompt list
skill-advisor hook < event.json      # manually invoke the hook with a synthetic event
skill-advisor match "some prompt"    # evaluate the matcher against a single prompt
skill-advisor report                 # pick frequency, dead-skill list, latency (telemetry must be on)
skill-advisor install                # re-run the shell/rc install (idempotent)
skill-advisor uninstall              # remove alias, settings, cache; keep config.toml
skill-advisor uninstall --purge-config
```

The `Makefile` wraps the common ones:

```bash
make sync           # install dev deps via uv
make test           # pytest
make sync-skills    # skill-advisor sync-skills (point at an external library with --from)
make build          # skill-advisor build
make doctor
make replay
make match Q="..."  # evaluate the matcher against a single prompt
make report         # pick frequency / dead-skill report (telemetry must be on)
```

---

## CLI reference

### `skill-advisor install [--no-build] [--write-alias]`

Renders `claudeskill-settings.json`, writes a default `config.toml` (if absent), builds
the catalog, and prints the exact `--settings` flag to add to your claude launcher.
Does **not** edit your shell rc file unless `--write-alias` is passed. Idempotent. When
`[effort] enabled = true` and `[effort] statusline = true`, also writes
`~/.config/skill-advisor/statusline.sh` and registers it as `statusLine` — see
[Effort signalling](#effort-signalling).

### `skill-advisor uninstall [--purge-config]`

Remove the alias block, settings file, catalog, embeddings, and log. Keeps
`config.toml` unless `--purge-config` is given. Does not `uv tool uninstall` the
package — run that separately.

### `skill-advisor sync-skills [--from PATH] [--force] [--dry-run]`

Copy an external library of `SKILL.md` folders into `~/.claude/skills/`. The
source is `--from PATH`, or (if omitted) `$SKILL_ADVISOR_BUNDLE/skills` or
`./skills` relative to the working directory. Non-destructive by default;
`--force` overwrites existing skill dirs; `--dry-run` prints the plan without
touching disk. This repo does not ship a skill bundle — bring your own.

### `skill-advisor build [--force]`

Scan the catalog sources, compute the source-file hash, and rebuild embeddings only
if the hash changed. `--force` rebuilds unconditionally.

### `skill-advisor replay <prompts_file>`

Replays a list of prompts (one per line, or JSONL with a `prompt` field) through the
full pipeline. Prints per-prompt picks + wall-clock time and an n/p50/p95 summary.

Useful for tuning `max_candidates`, `min_embedding_score`, and `use_judge`.

### `skill-advisor match [prompt] [options]`

Evaluate the matcher against a single prompt and print ranked picks with scores.
No side effects — does not consult or mutate lifecycle state, does not inject into
Claude Code.

```bash
skill-advisor match "refactor the auth middleware"
#   1. skill  api-security-best-practices  embedding match (0.78)
#   2. skill  security-bluebook-builder    embedding match (0.74)
#   3. skill  clerk-auth                   embedding match (0.72)
```

The prompt can come from stdin instead of a positional arg, but only when stdin is
piped (interactive-shell invocation without a prompt exits with an error):

```bash
echo "plan a postgres migration" | skill-advisor match --top-k 3
```

Flags:

- `-n, --top-k N` — picks to print (default: `matcher.max_picks` from config).
- `-k, --candidates N` — shortlist size (default: `matcher.max_candidates`).
- `--threshold F` — override `min_embedding_score` for this call (0–1).
- `--judge / --no-judge` — force the LLM judge on or off for this call.
- `--phase {planning,implementation,review,correction,complete}` — dispatch to
  the lifecycle's per-phase preference list instead of the prompt-based matcher.
  The prompt text is ignored in this mode.
- `--show-triage` — append an info line stating whether the hook's triage layer
  would have skipped this prompt (does not enforce the skip).
- `--json` — machine-readable output with per-pick `kind`, `name`, `score`,
  `namespace`, `description`, `path`.
- `--verbose` — append per-pick namespace, description (first line), and SKILL.md
  path to the text output.

### `skill-advisor report [options]`

Summarize pick frequency, dead skills, and latency from the opt-in event log at
`~/.cache/skill-advisor/advisor.events.jsonl`. Requires `events_enabled = true`
under `[telemetry]` in `config.toml` (see [Telemetry and usage reports](#telemetry-and-usage-reports)).

```bash
skill-advisor report --top 5 --since 30d
# Events: 1,247 across 89 sessions  (triage-skipped: 312)
#
# Top picks (top 5):
#   1. react-best-practices       142  11.4%
#   2. systematic-debugging        98   7.9%
#   3. frontend-design             74   5.9%
#   ...
# Catalog coverage: 527 / 940 entries picked at least once (56.1%)
#   Run with --dead to list the 413 unused.
# Latency: p50 283 ms, p95 1.24 s
```

Flags:

- `--since DURATION` — time window, e.g. `7d`, `24h`, `2w` (default: `30d`).
- `--top N` — show the top-N most-picked entries (default: 20).
- `--dead` — also list catalog entries that received zero picks in the window.
- `--by-phase` — group a secondary count by lifecycle phase.
- `--by-kind` — group a secondary count by kind (`skill` / `subagent` / `command`).
- `--format {table,csv,json}` — output format (default: `table`).
- `--purge-older-than DURATION` — rewrite the log, keeping only events newer
  than `DURATION`, and exit. Manual retention tool; the hook itself never prunes.

### `skill-advisor doctor`

End-to-end diagnosis. Checks: `claude` on PATH, settings file present, catalog
present & parseable, catalog freshness vs source mtimes, log file. Exits non-zero
if a required piece is missing. When `[effort] enabled = true`, also checks for
`jq` (required by the status line), whether the status line script has been
written, and whether an effort recommendation has been recorded yet — these
three lines are silent (not printed at all) when the effort feature is off.

Example output (effort feature enabled):

```
claude on PATH : /home/you/.local/bin/claude
settings file  : /home/you/.config/skill-advisor/claudeskill-settings.json
catalog        : 938 entries
freshness      : up-to-date
log            : /home/you/.cache/skill-advisor/advisor.log (3412 bytes)
jq             : /usr/bin/jq
statusline     : /home/you/.config/skill-advisor/statusline.sh
effort state   : present
```

`jq` prints `MISSING (status line will render nothing)` when absent; `statusline`
prints `not written (run install)` until you `skill-advisor install` after
enabling the feature; `effort state` prints `none yet` until the hook has fired
at least once with a recommendation to record.

### `skill-advisor hook`

The UserPromptSubmit hook entry point. Claude Code invokes this automatically when
the `claudeskill` alias is active; you don't normally call it yourself. For manual
testing:

```bash
echo '{"prompt":"refactor the auth middleware"}' | skill-advisor hook | jq .
```

### `skill-advisor posttooluse` and `skill-advisor stop`

Sibling hook entry points that feed the lifecycle state machine from model-side
events. `skill-advisor install` wires both into your settings file automatically.
You don't call them manually under normal use. Behavior is a no-op unless you
opt into [auto-advance](#auto-advance).

- `posttooluse` — reads a PostToolUse event from stdin, records the `tool_name`
  (and the `subagent_type` when the tool was `Task`) into
  `~/.cache/skill-advisor/sessions/<session_id>.turn.json`.
- `stop` — reads a Stop event from stdin, inspects the per-session turn state,
  and applies the auto-advance rules if enabled. Always clears the turn file.

### `skill-advisor lifecycle {status,reset}`

Inspect or reset per-session lifecycle state. See [Lifecycle mode](#lifecycle-mode)
for the full flow.

```
skill-advisor lifecycle status                          # list all sessions
skill-advisor lifecycle status --session <session_id>   # one session
skill-advisor lifecycle reset  --session <session_id>   # delete one session
skill-advisor lifecycle reset  --all                    # delete every session file
```

### Global flags

- `-v`, `--verbose` — emit INFO-level logs to stderr in addition to
  `~/.cache/skill-advisor/advisor.log`.

---

## Configuration reference

Config lives at `~/.config/skill-advisor/config.toml`. `skill-advisor install` writes a
fully-commented default. A representative file:

```toml
[matcher]
# Enable the `claude -p` judge for re-ranking the embedding shortlist.
#   false (default) → embedding top-K is the final ranking. ~50-300 ms per prompt warm.
#   true            → claude -p (Haiku) re-ranks for higher precision. Adds 5-15 s of
#                     overhead due to Claude Code session startup costs.
use_judge = false

# Judge model (only used when use_judge = true).
model = "claude-haiku-4-5-20251001"

# Embedding shortlist size — also the candidate count sent to the judge when enabled.
max_candidates = 15

# Max picks surfaced in additionalContext.
max_picks = 3

# Total hook budget in seconds. Hook exits silent if exceeded — your prompt always
# goes through, even when the advisor can't answer in time.
# Bump to ~15.0 if you enable use_judge = true, or ~25.0 if you enable
# [parallelization] (judge_timeout_seconds 20 + ~3 s margin + subprocess startup).
budget_seconds = 4.0

# Minimum cosine score to surface a pick in embedding-only mode. Range 0-1;
# 0.35 filters out weak matches on unrelated prompts.
min_embedding_score = 0.35

[catalog]
# Additional directories to scan for SKILL.md files (e.g. team-internal skills).
# extra_roots = ["/Users/me/work/team-skills"]
extra_roots = []

# Catalog names to suppress from the picker.
# exclude_names = ["overly-chatty-skill"]
exclude_names = []

[triage]
# Prompts shorter than this (in words) are skipped unless they contain technical
# keywords (bug, refactor, auth, test, …).
skip_if_shorter_than = 6

# Extra regex patterns (case-insensitive) that cause the advisor to skip.
# extra_skip_patterns = ["^wip:", "^\\s*draft:"]
extra_skip_patterns = []

[lifecycle]
# Master toggle. Set to false to disable lifecycle mode entirely — the advisor
# stays stateless and never injects a phase banner.
enabled = true

# Max correction cycles before the advisor forces `complete`.
max_correction_cycles = 3

# Extra regexes (case-insensitive) that should be treated as lifecycle triggers
# in addition to the built-in verbs (build / implement / refactor / add / migrate / …).
# extra_trigger_patterns = ["\\bstand\\s+up\\b", "\\bport\\s+over\\b"]
extra_trigger_patterns = []

# Extra regexes that suppress triggering even when a built-in verb matches.
# Use these to carve out prompts you don't want escalated into a multi-step lifecycle.
# extra_disable_patterns = ["^prototype:", "^spike:"]
extra_disable_patterns = []

# Per-phase skill/agent preferences. Entries use "kind:name"; kind is one of
# skill / subagent / command.
#
# phase_candidates REPLACES the built-in preference list for a phase.
# phase_additions PREPENDS to the built-in list (team picks take priority).
#
# [lifecycle.phase_additions]
# implementation = ["skill:team-implementation-checklist"]
# review = ["subagent:feature-dev:code-reviewer", "skill:team-review-checklist"]

[effort]
# Master toggle. When false, nothing in this feature runs: no classification, no
# status line registration, no nudge, no write-back. Off by default so existing
# installs are untouched until you opt in. See "Effort signalling" below.
enabled = false

# Register a `statusLine` command in claudeskill-settings.json (needs `jq`).
statusline = true

# Emit a systemMessage when the recommendation disagrees with observed effort.
nudge = true

# Allow occasional writes of `effortLevel` into claudeskill-settings.json.
write_back = true

# Consecutive qualifying sessions of disagreement before a write happens.
write_back_after_sessions = 5

# Sessions to suppress write-back for after the user manually overrides.
veto_cooldown_sessions = 10

# Recommend `ultracode` when the parallelization detector says yes.
# Never persisted — Claude Code treats ultracode as session-only by design.
ultracode_nudge = true
```

> **Sync note:** as of this writing, `skill-advisor install` does not yet write this
> `[effort]` block into a fresh `config.toml` — `install.py::_default_config_text()`
> has no `[effort]` section (a separate task adds it). The seven fields, names, and
> defaults above are read directly from `src/skill_advisor/config.py::EffortConfig`
> and are what the advisor actually uses when you add this block by hand; they are
> not yet what a fresh install writes for you.

Environment variables override path-derived defaults (useful for tests / multi-user
setups):

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_HOME` | — | Framework-specific escape hatch. Highest precedence. |
| `CLAUDE_CONFIG_DIR` | — | Claude Code's official env var. When set, `paths.claude_home()` resolves to it (e.g. `~/.claude-work` for work credentials). The catalog scans that dir's `skills/` first and falls back to `$HOME/.claude/skills` when the primary has nothing. |
| `SKILL_ADVISOR_CONFIG_HOME` | `$XDG_CONFIG_HOME/skill-advisor` → `$HOME/.config/skill-advisor` | |
| `SKILL_ADVISOR_CACHE_HOME` | `$XDG_CACHE_HOME/skill-advisor` → `$HOME/.cache/skill-advisor` | |
| `SKILL_ADVISOR_BUNDLE` | — | Used by `sync-skills` as a fallback source directory when `--from` is not passed. |

### Work / private credential splits

If you run Claude Code with multiple credential sets via `CLAUDE_CONFIG_DIR` (e.g.
`~/.claude` for private, `~/.claude-work` for work, each with its own
`.credentials.json`), the advisor honors whichever is active:

- `paths.claude_home()` resolves to `$CLAUDE_CONFIG_DIR` when set, else `$HOME/.claude`.
- `paths.skill_roots()` scans the active home's `skills/` + `plugins/marketplaces/`,
  then appends `$HOME/.claude`'s equivalents as fallbacks so the catalog stays
  populated even if the active home has no local skills (common when the work dir
  symlinks `skills/` back to the private one).
- Catalog deduplicates by `(kind, name)`; active-home entries win on collision.

---

## Latency and the two matcher modes

### Embedding-only (default)

| Stage | Cold | Warm |
|---|---|---|
| Python process launch | ~500 ms | — |
| Module imports (fastembed lazy) | ~400 ms | cached |
| Load catalog.json + embeddings.npz | ~30 ms | ~15 ms |
| Embed the prompt | ~200 ms | ~50 ms |
| Cosine top-K + render | <5 ms | <5 ms |
| **Total (hook invocation)** | **~3 s** | **~0.3 s** |

Picks are the cosine ranking directly. Quality is strong on term-overlap prompts
(`"plan a migration from mongodb to postgres"` → `database-migrations-sql-migrations`)
and weaker on abstract intent (`"there's a bug in tax calculation"` may miss the
better semantic match).

### LLM-judge (`use_judge = true`)

Adds a `claude -p` subprocess call to re-rank the embedding shortlist. Measured:

| Run | Total |
|---|---|
| Cold | ~11 s |
| Warm | ~7 s |

The overhead is not the LLM itself — it's Claude Code's session-startup cost: skill-list
injection, memory-recall hook, MCP servers. Bump `budget_seconds` to ~15.0 before
enabling.

Use this mode when ranking quality matters more than UX smoothness.

### Triage short-circuit

Prompts that match the triage skip rules bypass the pipeline in ~0 ms:

- Starts with `/` (user is invoking a slash command directly).
- Matches `^(yes|ok|thanks|continue|…)[\s!.?]*$`.
- Contains `\b(use|invoke|run|call)\s+(the\s+)?(\w+\s+)?(skill|agent|subagent|command)\b`.
- Short (`< skip_if_shorter_than` words) AND contains no technical keyword
  (`bug|fix|refactor|implement|review|test|build|deploy|debug|api|schema|…`).
- Matches any user-provided `triage.extra_skip_patterns`.

---

## Architecture deep dive

### Data flow

1. Claude Code (launched via `claudeskill`) receives a prompt.
2. Before the model sees it, the `UserPromptSubmit` hook defined in
   `~/.config/skill-advisor/claudeskill-settings.json` fires.
3. The hook runs `skill-advisor hook`, which reads a JSON event like
   `{"prompt": "...", "cwd": "...", "session_id": "..."}` from stdin.
4. `triage.should_skip()` short-circuits on cheap prompts.
5. `matcher.pick()` loads the catalog + embeddings, runs `index.top_k()`, and either
   returns the cosine ranking directly or delegates to `judge.rank()` for LLM re-ranking.
   When `[effort] enabled = true`, it also calls `effort.classify()` with the phase, judge
   verdict, and parallelization result it already computed, and attaches the resulting
   recommendation (or `None`) to the result — no extra subprocess.
6. If a recommendation was produced, the hook writes it to
   `~/.cache/skill-advisor/effort.json` (atomic temp+rename) and compares it against
   `~/.cache/skill-advisor/observed-effort.json` — the live level the status line last
   saw — to decide whether a nudge and/or a queued write-back announcement are due.
7. `inject.format()` renders picks into a `<skill-advisor>` block.
8. The hook prints
   `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}, "systemMessage": "..."}`
   to stdout — `systemMessage` present only when a nudge or announcement is pending.
9. Claude Code injects `additionalContext` into the model's context and shows any
   `systemMessage` to you directly.
10. The model sees a terse, authoritative "invoke the top match via the Skill tool"
    nudge and typically follows it.
11. Optional: if `telemetry.events_enabled = true`, one JSON event is appended to
    `~/.cache/skill-advisor/advisor.events.jsonl` with hashed prompt + session.
12. Independently, on every status-line render, `statusline.sh` reads Claude Code's live
    `effort.level` from stdin, records it to `observed-effort.json` (step 6's sensor
    input for the *next* prompt), and prints `observed [→ recommended] · model · ctx%`.
13. At the end of the turn, `skill-advisor stop` finalizes the session's modal
    recommendation into the rolling write-back window and may atomically rewrite
    `effortLevel` in `claudeskill-settings.json` — see [Effort signalling](#effort-signalling).

### Sibling hooks (PostToolUse + Stop)

When [auto-advance](#auto-advance) is enabled, `skill-advisor install` also wires
two sibling handlers into your settings file:

- **`skill-advisor posttooluse`** fires after every tool call. It appends the
  `tool_name` (and, for `Task` calls, the `subagent_type`) into
  `~/.cache/skill-advisor/sessions/<session_id>.turn.json`.
- **`skill-advisor stop`** fires at the end of each assistant turn. It reads
  the turn file, clears it, then consults the active lifecycle state and the
  `[lifecycle.auto_advance]` rules to decide whether to advance the phase
  without a user prompt.

All three handlers share the silent-on-error contract: any exception exits 0
and logs to `~/.cache/skill-advisor/advisor.log`. Claude Code never sees a
hook failure.

### Safety rails

- **`signal.alarm(budget_seconds + 0.5)`** — hard wall-clock budget; any overrun exits 0
  silent via a `_BudgetExceeded` exception caught in `hook.run()`.
- **Broad try/except in `hook.run()`** — any unexpected error exits 0 silent and logs
  to `advisor.log`.
- **Hallucination guard in `judge._parse_judge_reply()`** — LLM-returned names are
  intersected with the candidate list; unknown names are dropped.
- **Invalid regex in `triage.extra_skip_patterns`** — silently ignored rather than
  crashing the hook.

### Catalog sources

`catalog.scan()` walks, in order:

1. `$CLAUDE_HOME/skills/*/SKILL.md` → `namespace="user"`
2. `$CLAUDE_HOME/plugins/marketplaces/*/plugins/*/skills/*/SKILL.md` → `namespace="plugin:<name>"`
3. Built-in subagents (`Explore`, `Plan`, `general-purpose`, `feature-dev:*`,
   `superpowers:code-reviewer`, `code-simplifier`, `claude-code-guide`, `statusline-setup`)
   — hardcoded in `builtins.py` with the descriptions from Claude Code's Agent tool spec.
4. Built-in slash commands (`/loop`, `/schedule`, `/init`, `/help`, `/clear`, `/compact`,
   `/remember`, `/fast`).
5. Each path in `config.catalog.extra_roots` (recursive glob for `SKILL.md`) →
   `namespace="extra"`.

`config.catalog.exclude_names` filters by exact name at the end.

### Catalog storage

```
~/.cache/skill-advisor/
├── catalog.json       JSON list of {kind, name, namespace, description, path}
├── embeddings.npz     numpy array (N×384, float32, L2-normalized) aligned with catalog.json
├── catalog.hash       sha256 of (kind, name, mtime|description) tuples
└── advisor.log        INFO-level hook log, timestamped
```

`skill-advisor build` rebuilds the first three atomically. It's a no-op when the source
hash is unchanged, unless `--force` is passed.

When `[effort] enabled = true`, three more files live alongside these (not touched by
`build` — see [Effort signalling](#effort-signalling)):

```
~/.cache/skill-advisor/
├── effort.json             Latest recommendation, written by the UserPromptSubmit hook
├── observed-effort.json    Live effort level last seen by the status line (the sensor)
└── baseline.json           Rolling window, write-back history, veto/cooldown state

~/.config/skill-advisor/
└── statusline.sh            Generated POSIX-sh status line, registered as `statusLine`
```

### Embeddings

`fastembed.TextEmbedding("BAAI/bge-small-en-v1.5")` — 384-dim, ONNX via onnxruntime, CPU.
First download is ~15 MB cached under `~/.cache/fastembed`. Cold import inside the hook
is ~400 ms; the index load + embed + top-K combined is sub-100 ms warm.

Each catalog entry is embedded as `f"{name}: {description}"`.

### LLM judge

When `use_judge = true`, `judge.rank()` spawns:

```
claude -p --model <config.matcher.model> --output-format json
```

with stdin:

```
You are a skill router for Claude Code. [...]
Return ONLY JSON matching this schema:
{"picks": [{"name": "<exact catalog name>", "reason": "<<=12 words>"}], "skip": <bool>}

User message: <<< ... >>>
Candidates:
- name (kind) — description
...
```

The `--output-format json` envelope wraps the model's reply in `{"result": "<string>", ...}`.
`_parse_judge_reply()` extracts `result`, tolerates code fences / embedded objects,
parses the inner JSON, validates against the schema, and filters hallucinated names.

---

## Lifecycle mode

For substantive tasks, the advisor can drive Claude Code through a staged
lifecycle instead of a single one-shot pick:

```
planning → (parallelization_check →)? implementation → review → (correction ⇄ review)* → complete
```

`parallelization_check` is an optional one-shot detour that fires when the model writes
a multi-item TodoWrite during `planning` and you've opted into the feature. See
[Parallelization check](#parallelization-check) below.

### When a lifecycle starts

Automatically, when your prompt contains a "do something meaningful" verb
(`build`, `implement`, `create a`, `add a`, `refactor`, `migrate`, `design a`,
`rewrite`, `scaffold`, `wire up`, `set up`, `develop`, `ship`, `extend the`)
and a `session_id` is present (Claude Code always passes one).

To **suppress** the lifecycle for a specific prompt:

```
[no-lifecycle] build a rate limiter
```

Or use phrasing that triggers the anti-pattern (`just fix the typo`,
`quick tweak`, `small fix`, `one-off script`).

### What each phase recommends

| Phase | Preferred skills / agents (first that exist in your catalog) |
|---|---|
| `planning` | `Plan` subagent, `superpowers:write-plan`, `writing-plans`, `brainstorming`, `concise-planning` |
| `parallelization_check` | `superpowers:dispatching-parallel-agents`, `dispatching-parallel-agents`, `superpowers:using-git-worktrees`, `using-git-worktrees`, `parallel-agents`, `superpowers:subagent-driven-development`, `subagent-driven-development` |
| `implementation` | `superpowers:execute-plan`, `plan-executor`, `executing-plans`, `subagent-driven-development`, `test-driven-development` |
| `review` | `feature-dev:code-reviewer` subagent, `superpowers:code-reviewer`, `code-reviewer`, `code-review-excellence`, `code-review-checklist` |
| `correction` | `fix-review`, `address-github-comments`, `iterate-pr`, `debugger`, `superpowers:systematic-debugging` |
| `complete` | `commit`, `create-pr`, `pr-creator`, `finishing-a-development-branch`, `git-pushing` |

Rankings come from `src/skill_advisor/lifecycle.py::PHASE_CANDIDATES`. When
none of the preferred names exist in your catalog, the advisor falls back to
the normal embedding matcher against the phase name.

### Customizing the lifecycle per user

Everything about the lifecycle — the master toggle, cycle cap, trigger verbs,
and phase preferences — is configurable in `~/.config/skill-advisor/config.toml`:

```toml
[lifecycle]
enabled = true                    # set false to disable the state machine
max_correction_cycles = 3         # force complete after N correction rounds

# Add team-specific trigger verbs ("stand up the staging env", "port over …").
extra_trigger_patterns = ["\\bstand\\s+up\\b", "\\bport\\s+over\\b"]

# Skip lifecycle on prompts that match these, even if they contain a trigger verb.
extra_disable_patterns = ["^prototype:", "^spike:"]

# Prepend team picks to the built-in preference list (team picks come first,
# built-ins still follow as fallbacks).
[lifecycle.phase_additions]
implementation = ["skill:team-implementation-checklist"]
review = ["skill:team-review-checklist"]

# Fully REPLACE the built-in list for a phase — team's review flow wins outright.
[lifecycle.phase_candidates]
review = ["subagent:feature-dev:code-reviewer", "skill:security-audit"]

[parallelization]
# One-shot parallelization-check phase between planning and implementation.
# See README › Parallelization check. Uses `claude -p`, so budget_seconds must
# be bumped to >= judge_timeout_seconds + 3.
enabled = false
min_tasks = 3
judge_timeout_seconds = 20.0

[lifecycle.auto_advance]
# Opt-in Stop/PostToolUse hooks that push phases forward without a user prompt.
# Requires re-running `skill-advisor install` after flipping to True so the
# PostToolUse + Stop hook entries land in claudeskill-settings.json.
enabled = false
# PLANNING → IMPLEMENTATION when a Plan subagent completed in the last turn.
on_plan_subagent_done = true
# IMPLEMENTATION → REVIEW when the last turn ended with Edit/Write/NotebookEdit/Bash.
on_edit_stop = true

[telemetry]
# Opt-in structured event log at ~/.cache/skill-advisor/advisor.events.jsonl.
# When enabled, every hook firing records: timestamp, hashed session + prompt,
# picks (name/kind/score), phase, triage decision, duration. Session ids and
# prompts are ALWAYS hashed; no plaintext ever lands on disk.
events_enabled = false

# Informational retention target in days. Enforcement is manual — run
# `skill-advisor report --purge-older-than 90d` periodically if you care.
retain_days = 90

# Hash salt for prompt + session id SHAs. Leave empty to auto-generate a salt on
# first write (stored at ~/.cache/skill-advisor/telemetry.salt, mode 0600).
# Wipe the salt file to anonymise historical data.
prompt_hash_salt = ""
```

Format for `phase_additions` / `phase_candidates` entries:

- String form `"kind:name"`.
- `kind` must be one of `skill`, `subagent`, `command`.
- `name` is the exact catalog name (case-sensitive).
- Invalid entries (bad kind, missing colon) are silently dropped.

Phases recognised: `planning`, `implementation`, `review`, `correction`, `complete`.

### Advancing and stopping

Each phase is advanced by the **next prompt** you type:

| Your next prompt | Effect |
|---|---|
| `go`, `next`, `continue`, `proceed`, `yes`, `ship it`, `do it` | Advance to next phase |
| `continue, fix these issues` (during `review`) | Advance to `correction` |
| `looks good`, `done`, `LGTM`, `finished` (during `review`/`correction`) | Jump to `complete` |
| `cancel`, `abort`, `stop`, `nevermind` | End the lifecycle; pick normally |
| A substantive off-topic prompt (≥4 words, no continuation verb) | Silently end the lifecycle; pick normally |

After **3 correction cycles**, the advisor forces `complete` to avoid infinite
rework loops.

### The injected context during a lifecycle

```
<skill-advisor>
Based on the user's prompt, the following catalog entries are strong matches. ...

Lifecycle: planning → implementation → [review] → correction → complete (correction cycle 1 of 3)
Original task: build a rate limiter for the api endpoints
After this step completes, the next phase is: correction (if review flags issues) or complete.
Reviewer: surface any issues explicitly in the response so the user can reply
'fix these' to advance to correction, or 'looks good' to complete.

  1. feature-dev:code-reviewer (subagent) — review phase preference
  2. superpowers:code-reviewer (subagent) — review phase preference
  3. code-reviewer (skill) — review phase preference
</skill-advisor>
```

This lets the model see *where* it is in the arc and *what's coming next*, so
reviewers surface issues the user can respond to, and implementers know a
review will follow.

### Inspecting and resetting

```bash
skill-advisor lifecycle status                          # list all active sessions
skill-advisor lifecycle status --session <session_id>   # one session
skill-advisor lifecycle reset --session <session_id>    # clear one session
skill-advisor lifecycle reset --all                     # clear all sessions
```

State is stored at `~/.cache/skill-advisor/sessions/<session_id>.json` — safe
to delete manually if something gets stuck.

### Auto-advance

Lifecycle phases normally advance only on your follow-up prompt (`go`, `next`,
`looks good`, …). **Auto-advance** is an opt-in mode that pushes phases forward
based on model-side activity, so you don't have to acknowledge every transition.

Enable it in `~/.config/skill-advisor/config.toml`:

```toml
[lifecycle.auto_advance]
enabled = true                # master toggle
on_plan_subagent_done = true  # PLANNING → IMPLEMENTATION
on_edit_stop = true           # IMPLEMENTATION → REVIEW
```

Then re-run `skill-advisor install` once so the PostToolUse and Stop hook
entries land in your `claudeskill-settings.json`, and relaunch `claudeskill`. Nothing
else changes for sessions that don't trigger a lifecycle.

Two transitions auto-advance:

- **`PLANNING → IMPLEMENTATION`** when the last turn invoked a `Plan` subagent
  (via Claude Code's `Task` tool with `subagent_type="Plan"`). Use this when you
  want the advisor to hand off to the implementer immediately after planning
  completes, without typing "go".
- **`IMPLEMENTATION → REVIEW`** when the last turn ended with any mutating tool
  (`Edit`, `Write`, `NotebookEdit`, `MultiEdit`, `Bash`). If the model only read
  files this turn (`Read`, `Grep`, `Glob`, `LS`), the state stays at
  `IMPLEMENTATION` — this is what makes Esc-cancelled turns safe.

Two transitions remain **user-driven** by design:

- **`REVIEW → CORRECTION`** — detecting "the reviewer flagged issues" from model
  text is fuzzy and brittle. You type `continue, fix these issues` to advance.
- **`anything → COMPLETE`** — you type `looks good` / `LGTM` to close out.

Guards:

- **1-second double-advance guard.** If you type `go` at the exact moment a
  `Stop` event fires, the Stop handler defers — `lifecycle.advance()` was just
  called and the state's `updated_at` is less than 1 s in the past.
- **Readonly-only turn skip.** If the turn's `tool_names` are all read-only
  (or empty), the Stop handler does not advance. This covers Esc-cancelled
  turns, Q&A turns, and exploration turns that happen to land mid-phase.

When auto-advance triggers, the resulting history entry gets `"source": "auto"`
instead of `"user"`, and the telemetry log (when enabled) records
`"phase_source": "auto"` for the next prompt in that session:

```bash
skill-advisor lifecycle status --session <id>
# phase:      review
# last event: review — auto: Stop after mutating tools (Edit,Read)
```

To turn it off, flip `enabled = false` (no reinstall needed — the hook still
fires but becomes a no-op beyond clearing the per-turn state file).

### Parallelization check

Opt-in one-shot detour between `planning` and `implementation`: when the model
writes a multi-item `TodoWrite` during planning, the advisor asks `claude -p`
whether the tasks can be dispatched as parallel subagents in isolated git
worktrees. If yes, the next prompt's banner recommends
`dispatching-parallel-agents` + `using-git-worktrees`, including the merge-back
workflow (rebase → fast-forward merge → remove worktrees, escalate on conflict).
If the judge says no or times out, the phase auto-advances silently to
`implementation`.

The phase is **one-shot**: it fires at most once per lifecycle, never loops,
and is always followed by `implementation` on the same prompt.

#### Enable

Off by default. To enable, add to `~/.config/skill-advisor/config.toml`:

```toml
[matcher]
# Must be >= judge_timeout_seconds + 3 s for the detector to have a chance to run.
budget_seconds = 25.0

[lifecycle.auto_advance]
# The parallelization transition rides on top of the Stop-hook auto-advance path.
enabled = true

[parallelization]
enabled = true
min_tasks = 3              # fire only when the TodoWrite has at least this many items
judge_timeout_seconds = 20.0
```

Then re-run `skill-advisor install` to refresh the PostToolUse + Stop hook
registrations in `claudeskill-settings.json`, and restart `claudeskill`. `skill-advisor
doctor` will warn if `budget_seconds < judge_timeout_seconds + 3`.

#### How it flows

1. **PostToolUse** captures the `TodoWrite` payload (task titles + count) into
   `<session_id>.turn.json` when `parallelization.enabled`.
2. **Stop** sees a qualifying TodoWrite (count ≥ `min_tasks`) during `planning`
   and transitions the session into `parallelization_check`, persisting the
   task titles onto `LifecycleState.pending_todos`.
3. **Your next UserPromptSubmit** (any prompt — typically `go`) routes through
   `parallelization.detect()`, which spawns `claude -p` with a strict JSON
   schema asking for a verdict (`{parallel: bool, groups: [[idx, ...], ...], reason: str}`).
4. A **hallucination guard** (mirroring `judge.py`) drops any task index outside
   `[0, n_tasks)`, removes empty groups, and downgrades `parallel=True` to
   `False` if every group was wiped.
5. If `parallel=True`: banner highlights `[parallelization_check]`, picks list
   contains the parallel-exec skills, and the phase-specific nudge instructs
   the model to use isolated worktrees + the merge-back workflow. If
   `parallel=False` or the detector returned `None`: no parallelization nudge;
   the banner shows implementation-phase picks.
6. The on-disk state is **always** advanced to `implementation` before the
   hook returns, so the detour can't fire twice.

#### Why it's opt-in

The detector uses `claude -p`, which carries Claude Code's ~15 s session-startup
cost. That's why `budget_seconds` must be bumped well above the default 4.0 —
the `doctor` check enforces `>= judge_timeout_seconds + 3` so the subprocess
has room to actually return a verdict.

#### Turning it off

```toml
[parallelization]
enabled = false
```

No reinstall needed — the hook still fires but the PostToolUse capture and
Stop-hook transition become no-ops.

#### The merge-back workflow

When the model follows the parallelization nudge, it dispatches subagents into
separate worktrees and must reassemble their work onto the working branch. The
`using-git-worktrees` skill documents the exact workflow: commit inside each
worktree, then for each worktree **serially** rebase onto the current working
branch and immediately fast-forward merge (independent rebases + batch FF
merges fail on the second branch because the working branch has advanced),
remove the worktrees, verify clean. Conflicts abort the rebase and escalate
to the user — never auto-resolved.

### Non-goals / limitations

- **Reviewer output is opaque.** `REVIEW → CORRECTION` still needs a user
  prompt because we don't parse model text for issue-shaped phrases — too many
  false positives/negatives.
- **Lifecycle is per-session.** Restarting Claude Code creates a new session
  id, so long-running work spanning sessions isn't stitched together.
- **No `SubagentStop` handling yet.** PostToolUse fires for the parent `Task`
  invocation, which is enough for the Plan-subagent rule; deep subagent event
  plumbing is a future enhancement.

---

## Effort signalling

Claude Code exposes a reasoning-effort dial (`low`/`medium`/`high`/`xhigh`/`ultracode`)
that most people set once and forget. skill-advisor already inspects every prompt, so it
reuses that work to recommend an effort level, show it to you in a status line, and nudge
you when your live setting and its recommendation disagree — and, over enough sessions of
sustained disagreement, it will quietly rewrite your launch-time default, subject to a
veto you trigger just by using `/effort` yourself. **It is off by default** —
`[effort] enabled = false` — so a fresh install behaves exactly as it did before this
feature existed.

Two things it *cannot* do, because Claude Code doesn't let a hook do them: it cannot set
effort for the current turn, and it cannot read live effort from the `UserPromptSubmit`
event payload. Both facts shape everything below.

### Why the advisor can only recommend, never set

Effort and the `ultracode` flag live in Claude Code's own `AppState`, mutated only by
slash commands, the `--settings` launch flag, or an SDK control message — never by a hook.
A `UserPromptSubmit` hook can return `additionalContext`, `systemMessage`,
`suppressOutput`, and `decision`; there is no field that changes what the model runs with
this turn. So the advisor's only lever is a **launch-time default**, written to the
`--settings` file the `claudeskill` alias already passes to `claude`
(`~/.config/skill-advisor/claudeskill-settings.json`). At the next launch, the precedence
chain is:

```
CLAUDE_CODE_EFFORT_LEVEL=…      highest — blocks everything
  "Not applied: CLAUDE_CODE_EFFORT_LEVEL=x overrides effort this session"
--effort <level> launch flag     creates a "launch-effort pin"
  "Not applied: the launch-effort pin holds effort at x this session.
   Run /effort <level> in an interactive terminal to release the pin."
--settings effortLevel           ← where skill-advisor writes
~/.claude/settings.json          ← the user's own saved default
```

The load-bearing fact is line three: **`--settings` outranks your own
`~/.claude/settings.json`.** That's what makes write-back possible at all — and it's also
what makes the veto below necessary, not optional. (These are 2026-07 findings from
reading the compiled Claude Code 2.1.220 binary — accurate for that build, not a stable
contract. If a future Claude Code build changes this, the status line degrades gracefully
[see Limits](#limits-of-this-feature); classification and write-back only ever touch the
documented `effortLevel` settings key, so they keep working regardless.)

### Two vocabularies, not one

The level Claude Code shows you and the level the classifier recommends are drawn from
**different enums** — this is deliberate, not a bug:

| | Values |
|---|---|
| Observed (from the harness's status-line payload) | `low` `medium` `high` `xhigh` `max` |
| Recommended (by the classifier) | `low` `medium` `high` `xhigh` `ultracode` |

`max` is never recommended — the classifier has no basis for distinguishing it from
`xhigh`. `ultracode` is never observed — Claude Code's status-line payload surfaces it as
plain `xhigh` (it resolves to `xhigh` effort plus a standing dynamic-workflow flag), so the
status line cannot tell live `ultracode` apart from live `xhigh` and does not try. Ordering
for comparison is `low < medium < high < xhigh ≤ ultracode`, with `xhigh < max`. **When
your observed level is `max`, the feature goes quiet** — no arrow, no nudge, no write-back
contribution for that session — because you've deliberately gone above anything the
advisor knows how to recommend.

### The status line

`skill-advisor install` renders a POSIX-sh script to `~/.config/skill-advisor/statusline.sh`
and registers it as `statusLine` in `claudeskill-settings.json`, but only when both
`effort.enabled` and `effort.statusline` are true, and only if you don't already have a
foreign `statusLine` command registered (anything whose command isn't exactly this script's
path is left alone). It renders:

```
xhigh · opus · ctx 34%              agreement — no arrow
xhigh → medium · opus · ctx 34%     disagreement — arrow, coloured by the target level
```

It needs **`jq`** — the script is deliberately shell, not Python, because Claude Code
re-renders the status line continuously and a Python cold start in that loop would be
felt on every keystroke. Without `jq` on `PATH` it prints nothing and exits 0; `doctor`
flags this (see below).

It also does a second job you don't see: on every render it writes the live effort level
it was just handed to `~/.cache/skill-advisor/observed-effort.json`. This is the **only**
surface that can see live effort at all — the `UserPromptSubmit` hook's own event payload
doesn't carry it — so the hook reads this file back to know whether its recommendation
agrees with reality. On the very first prompt of a session no observation has landed yet;
the nudge stays silent rather than guessing.

### The nudge

When the recommendation disagrees with the last-observed level, the hook adds a
`systemMessage` alongside its normal `additionalContext`:

```
skill-advisor: this looks like xhigh work (judge assessment) — you're at medium.  /effort xhigh
```

Because `ultracode` is indistinguishable from `xhigh` in what the status line can observe,
an `ultracode` recommendation nudges as a keyword suggestion instead of a `/effort`
argument — typing `ultracode` trips Claude Code's own built-in badge, which
skill-advisor cannot render itself (Claude Code's keyword-badge table is a hardcoded
five-entry registry with no plugin or hook extension point):

```
skill-advisor: this decomposes into parallel work (tasks decompose into parallel sub-agents) — consider the `ultracode` keyword. You're at medium.
```

Rate-limited to once per `(session, observed→recommended)` pair, so a long session doesn't
nag on every prompt — but a *different* disagreement (e.g. you move to `high` and the
recommendation is now `xhigh`) gets its own single nudge.

### Write-back and the veto

**This is the part people get wrong, so read it even if you skim the rest.**

Every `UserPromptSubmit` firing appends its recommendation to that session's tally. Once a
session has at least 3 recommendations, the `Stop` hook computes that session's **modal**
(most common) level and upserts it — one entry per session, however many turns it ran —
into a rolling window in `~/.cache/skill-advisor/baseline.json` (capped at the most recent
30 qualifying sessions).

The comparison point is the **effective launch value**: skill-advisor's own previously
written `effortLevel` if it has written one, otherwise the status line's first observation
for that session. When the most recent `write_back_after_sessions` (default **5**)
sessions in the window all agree on one level, and that level differs from the effective
launch value, skill-advisor writes it into `claudeskill-settings.json` — atomically
(temp file → JSON round-trip validation → rename) — and queues a one-shot announcement
for the next session's first hook firing:

```
skill-advisor moved your effort baseline xhigh → high (5 sessions of consistent work). Run /effort xhigh to keep it there.
```

**Now the veto.** Suppose you don't like the new baseline and, mid-session, type
`/effort xhigh` to go back. That save lands in `~/.claude/settings.json` — your own file —
which, per the precedence chain above, `--settings` **outranks**. At your *next* launch,
`claudeskill-settings.json`'s `effortLevel` wins again, silently, and your `/effort` command
appears to have done nothing. **`/effort` alone does not undo a write-back.**

What actually protects you: the status line's sensor is watching live effort on every
render. If a later observation in a session differs from that session's *first*
observation — i.e. you reached for `/effort` or a keyword mid-session — skill-advisor
treats that as an explicit veto, regardless of whether it had written anything yet. A veto:

- resets the rolling window (so post-override evidence starts fresh), and
- suppresses further write-back for `veto_cooldown_sessions` (default **10**) sessions.

That pauses *future* writes. It does **not** retroactively revert a baseline already
written to `claudeskill-settings.json` — there is currently no code path that does that.
If skill-advisor has already moved your baseline and you want it back immediately, edit or
delete `effortLevel` in `~/.config/skill-advisor/claudeskill-settings.json` by hand.

### `ultracode` is never persisted

Claude Code itself treats a `/effort ultracode` save as *"(this session only)"* — it never
writes `ultracode` to settings, unlike every other level. skill-advisor honours that:
`effort.to_persistable("ultracode")` degrades to the `xhigh` it resolves to, so a session
whose modal recommendation is `ultracode` contributes `xhigh` — never the literal string
`"ultracode"` — to the write-back window. No code path in this feature ever writes
`ultracode` into `claudeskill-settings.json`. Persisting it would invert Claude Code's own
upstream default and start every future session in the expensive mode.

### Limits of this feature

- **No recommendation, no nudge, no arrow — sometimes, by design.** Classification is
  attached to the matcher's result, not computed independently. When your prompt matches
  no catalog entry above `min_embedding_score` (or triage skips it), `matcher.pick()`
  returns nothing to attach a recommendation to, so `effort.classify()` never runs. The
  status line still renders your **live** effort level in that case — it just has no
  `effort.json` to compare against, so it shows no arrow and the hook has nothing to nudge
  about.
- **Write-back is not lock-protected.** Writing `effortLevel` is a read-modify-write on
  the same `claudeskill-settings.json` that `skill-advisor install` also rewrites (to merge
  hook entries, the `statusLine` key, etc.). If the two run at literally the same moment,
  the later writer silently discards the earlier writer's key. No lock is taken — the code
  judges this an acceptable exposure for a single-user local tool, since it requires
  running `install` at the exact instant a `Stop` hook fires.
- **The harness findings are not a stable contract.** The precedence chain, the
  status-line payload shape, and the keyword-badge table were established by reading the
  compiled `claude` 2.1.220 binary, not from public documentation. If a later Claude Code
  build changes them, only the status line's live-effort sensor is fragile — it degrades
  to "no arrow, live effort only." Classification and write-back rest entirely on the
  documented `statusLine`, `systemMessage`, and `effortLevel` surfaces and keep working.

### Configuring

```toml
[effort]
enabled = true          # turn the whole feature on
statusline = true        # register the generated status line (needs jq)
nudge = true              # systemMessage when observed and recommended disagree
write_back = true         # allow the advisor to tune your launch default over time
write_back_after_sessions = 5
veto_cooldown_sessions = 10
ultracode_nudge = true    # recommend `ultracode`; it is never written to settings
```

See [Configuration reference](#configuration-reference) for the full field list with
comments, and re-run `skill-advisor install` after flipping `enabled` so the status line
gets registered.

### Troubleshooting: the status line shows nothing

Check, in order:

1. **Is `jq` installed?** `command -v jq`. Without it the script exits 0 with empty
   output by design. `skill-advisor doctor` reports this when `[effort] enabled = true`.
2. **Did you run `skill-advisor install` after enabling the feature?** The `statusLine`
   key is only registered when `effort.enabled` (and `effort.statusline`) were true *at
   install time*. Flipping the config afterward doesn't retroactively register it.
3. **Is `[effort] enabled = true`?** With the feature off, no script is generated or
   registered, and `doctor` prints nothing effort-related at all — that's expected, not a
   bug.
4. **Do you already have a `statusLine` command from something else?** skill-advisor
   never overwrites a `statusLine` whose command isn't exactly its own script's path.

`skill-advisor doctor` is the fastest way to check all of the above in one shot — see
[`skill-advisor doctor`](#skill-advisor-doctor).

---

## Telemetry and usage reports

An opt-in structured event log records what the advisor picked for each prompt,
so `skill-advisor report` can surface pick frequency, dead-skill detection,
and latency stats. Off by default.

### Privacy model

- Prompts are stored as `sha256(salt + prompt)[:16]` — never plaintext.
- Session ids are hashed the same way — raw Claude Code session ids never land
  on disk.
- The salt auto-generates once at `~/.cache/skill-advisor/telemetry.salt`
  (mode `0600`) unless you pin one in `config.toml`. Wiping the salt file
  anonymises historical events.
- Nothing leaves your machine. The log is a local JSONL file that only
  `skill-advisor report` reads.

### Enabling

```toml
[telemetry]
events_enabled = true
# prompt_hash_salt = ""       # leave blank to auto-generate
# retain_days = 90            # informational; use --purge-older-than manually
```

No reinstall needed — the `UserPromptSubmit` hook reads the config on every
firing. After a few prompts through `claudeskill`:

```bash
skill-advisor report --top 10 --since 30d
```

### Example output

```
Events: 1,247 across 89 sessions  (triage-skipped: 312)

Top picks (top 10):
  1. react-best-practices           142  11.4%
  2. systematic-debugging            98   7.9%
  3. frontend-design                 74   5.9%
  ...

Catalog coverage: 527 / 940 entries picked at least once (56.1%)
  Run with --dead to list the 413 unused.

Latency: p50 283 ms, p95 1.24 s
```

### Finding dead weight in the bundle

```bash
skill-advisor report --dead --since 90d        # list skills picked zero times
skill-advisor report --dead --format csv       # pipe into a spreadsheet
```

### Drilling down

```bash
skill-advisor report --by-phase      # pick counts grouped by lifecycle phase
skill-advisor report --by-kind       # split by skill / subagent / command
skill-advisor report --format json   # machine-readable for downstream tools
```

### Retention

The hook never prunes the log. To cap growth:

```bash
skill-advisor report --purge-older-than 90d    # atomic rewrite; keeps newer lines
```

### Event shape

Each line is one JSON object with this schema:

```json
{
  "schema": 1,
  "ts": "2026-04-22T10:24:20Z",
  "session_sha256": "f8c05c20acf09295",
  "prompt_sha256": "89d7c0f1a2054b3e",
  "prompt_words": 12,
  "triage_skipped": false,
  "duration_ms": 283,
  "phase": "planning",
  "phase_source": "user",
  "judge_used": false,
  "picks": [
    {"rank": 1, "name": "Plan", "kind": "subagent", "score": null},
    {"rank": 2, "name": "writing-plans", "kind": "skill", "score": null}
  ]
}
```

`score` is `null` for lifecycle-phase picks (which come from a preference list,
not embeddings) and the LLM judge mode (where scores aren't returned).
`phase_source` is `"auto"` when the previous Stop handler advanced the phase.

### Disabling and wiping

```toml
[telemetry]
events_enabled = false
```

```bash
rm ~/.cache/skill-advisor/advisor.events.jsonl ~/.cache/skill-advisor/telemetry.salt
```

`skill-advisor uninstall` also removes both files.

---

## Troubleshooting

### `claudeskill: command not found`

Either you haven't sourced your rc file yet (`source ~/.zshrc`), or your existing
`claudeskill` wrapper isn't defined in an interactive shell. Run `type claudeskill` in a
fresh shell to confirm. If you haven't wired anything up yet, see
[Activating the advisor](#activating-the-advisor).

### `skill-advisor install --write-alias` shadowed my existing claudeskill

`--write-alias` blindly appends `alias claudeskill='claude --settings …'` to your rc,
which overrides any wrapper that sets `CLAUDE_CONFIG_DIR` or similar. Fix:

```bash
skill-advisor uninstall     # removes the alias block
# Then manually add --settings to your existing claudeskill:
alias claudeskill='CLAUDE_CONFIG_DIR=~/.claude-work claude --settings ~/.config/skill-advisor/claudeskill-settings.json'
```

### `skill-advisor: command not found` after `uv tool install .`

`uv tool install` writes binaries to `~/.local/bin`. Ensure it's on PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### The hook fires but no picks appear

Check `~/.cache/skill-advisor/advisor.log`. Common causes:

- **Catalog empty / missing** — run `skill-advisor build`. Confirm with
  `skill-advisor doctor`.
- **`claude` not on PATH (when `use_judge = true`)** — `skill-advisor doctor` shows
  `claude on PATH : MISSING`.
- **Budget too tight** — bump `budget_seconds` in `config.toml`, then reinstall
  (`uv tool install --reinstall .`) if you changed source.
- **All candidates below `min_embedding_score`** — your prompt may be genuinely unrelated
  to any catalog entry. Lower the threshold or verify with `skill-advisor replay`.

### Latency feels high

Default is embedding-only mode, which measures ~3 s cold / ~0.3 s warm on the test
machine. If you enabled `use_judge = true`, expect 7-12 s — that's inherent to
`claude -p` startup, not something we can shave from the subprocess side. Switch back
to `use_judge = false` for speed, or accept the latency for better picks.

### Every prompt times out at exactly `budget_seconds`

Usually means `fastembed` is re-downloading the model on every invocation (cache dir
unwritable). Check `ls ~/.cache/fastembed/` — should contain the BGE-small files after
the first build.

### `skill-advisor doctor` reports freshness STALE

Your installed skills changed since the last build. Run `skill-advisor build`. If you
expect auto-rebuild, there is none by design — rebuild is always explicit.

### Windows

Not supported in v1. Use WSL2 or adapt `install.py`'s shell detection to PowerShell.

---

## Maintainer workflow

### Before pushing

```bash
make test                            # pytest
make doctor                          # sanity check your own install still works
```

### Bumping the version

Edit `pyproject.toml`:

```toml
version = "0.2.0"
```

Tag the release and ask colleagues to `git pull && uv tool install --reinstall .`.

### Adding a new built-in subagent or slash command

Edit `src/skill_advisor/builtins.py`. The descriptions you put there are what the
embedding model sees, so they should match (or closely paraphrase) the text Claude
Code itself uses.

---

## Contributing

PRs and issues are welcome. Before opening a PR:

- Run `make test` — all 261 tests should pass on Linux and macOS, Python 3.11 / 3.12.
- Keep changes focused. Match the existing [Conventional Commits](https://www.conventionalcommits.org/) style (`feat:`, `fix:`, `docs:`, `chore:`, …).
- For larger features or behavioral changes, open an issue first so we can align on scope before you invest time.

See [Development](#development) below for the local hacking loop.

---

## Development

### Setup

```bash
git clone https://github.com/deep-content/skill-advisor.git
cd skill-advisor
uv sync --extra dev
```

### Running tests

```bash
uv run pytest                        # 261 tests
uv run pytest tests/test_hook.py -v  # single file
uv run pytest --cov=skill_advisor    # with coverage
```

All tests run against a synthetic `fake_claude_home` fixture — they never touch your
real `~/.claude`.

### Continuous integration

`.github/workflows/ci.yml` runs `pytest` on Linux + macOS, Python 3.11 and 3.12. CI is
the source of truth for compatibility.

### Local hacking loop

Source changes don't automatically propagate to the globally-installed `skill-advisor`.
After editing, reinstall:

```bash
uv tool install --reinstall .
```

Or during rapid iteration, run inside the project venv:

```bash
echo '{"prompt":"..."}' | uv run python -m skill_advisor.cli hook
```

---

## File layout

```
skill-advisor/
│
├── src/skill_advisor/
│   ├── __init__.py            Package version
│   ├── paths.py               XDG-aware path resolution (single source of truth)
│   ├── config.py              TOML config + dataclass schema + defaults
│   ├── builtins.py            Hardcoded subagents + slash commands
│   ├── catalog.py             Scan SKILL.md + builtins, save/load catalog.json
│   ├── index.py               fastembed embeddings + cosine top-K + hash check
│   ├── triage.py              Cheap-prompt detector
│   ├── judge.py               `claude -p` subprocess wrapper, JSON schema, hallucination guard
│   ├── parallelization.py     `claude -p` detector for TodoWrite parallelizability (hallucination-guarded)
│   ├── lifecycle.py           Phase state machine + TurnState for auto-advance
│   ├── effort.py              Effort-level vocabulary, ordering, and classify() — the
│   │                            resolution ladder that turns phase/judge/parallelization
│   │                            signals into an EffortRecommendation, no new subprocess
│   ├── matcher.py             Orchestrator: triage → lifecycle | index → (optionally) judge;
│   │                            exposes pick_stateless() shared with `skill-advisor match`;
│   │                            attaches effort.classify()'s result when effort is enabled
│   ├── telemetry.py           Opt-in event log: record/iter_events/purge_older_than
│   ├── inject.py              Format picks + lifecycle banner into the additionalContext block
│   ├── baseline.py            Effort nudge ledger + rolling per-session write-back window;
│   │                            veto/cooldown state; atomic effortLevel write-back + announce
│   ├── hook.py                Three handlers: run (UserPromptSubmit),
│   │                            run_posttooluse, run_stop — all silent-on-error
│   ├── install.py             Shell detection, rc editing, settings-file *merge* renderer
│   ├── statusline.py          Generates the POSIX-sh status-line script (renderer + the
│   │                            only sensor that can observe live effort)
│   ├── sync.py                Bundled-skills → ~/.claude/skills copy logic
│   └── cli.py                 argparse dispatcher: install / uninstall / build / replay /
│                                doctor / sync-skills / lifecycle / hook /
│                                match / report / posttooluse / stop
│
├── tests/
│   ├── conftest.py            Auto-use isolated_paths fixture (per-test tempdirs)
│   ├── fixtures/              fake_claude_home/, prompts.jsonl
│   └── test_*.py              261 tests across all modules
│
├── examples/
│   ├── config.toml            Default user config (commented)
│   ├── claudeskill-settings.json  Reference UserPromptSubmit hook settings
│   └── prompts.jsonl          Calibration prompts for `skill-advisor replay`
├── .github/workflows/ci.yml   pytest on Linux + macOS, Python 3.11/3.12
│
├── pyproject.toml             Package definition, console-script entry point
├── Makefile                   Convenience wrappers around common commands
├── README.md                  This file
├── INSTALL.md                 Step-by-step onboarding (colleague-facing)
└── LICENSE                    MIT
```

Per-user state (created by the installer, not tracked in git):

```
~/.config/skill-advisor/
├── config.toml                 User config (edit to tune)
├── claudeskill-settings.json       --settings file that the `claudeskill` alias passes to claude
└── statusline.sh               Generated status line (only when [effort] enabled+statusline)

~/.cache/skill-advisor/
├── catalog.json                Current built catalog
├── embeddings.npz              Aligned embeddings
├── catalog.hash                Source-file mtime hash (for no-op rebuilds)
├── advisor.log                 Human-readable hook log
├── advisor.events.jsonl        Opt-in structured event log (see telemetry section)
├── telemetry.salt              Auto-generated SHA-256 salt (mode 0600)
├── effort.json                 Latest recommendation (only when [effort] enabled)
├── observed-effort.json        Live effort last seen by the status line (the sensor)
├── baseline.json                Write-back window, history, veto/cooldown state
└── sessions/
    ├── <session_id>.json       Per-session lifecycle state
    └── <session_id>.turn.json  Per-turn ephemeral tool/subagent record (auto-advance)

~/.cache/fastembed/             BGE-small ONNX model (~15 MB, shared across projects)
```

---

## Uninstalling

```bash
skill-advisor uninstall              # removes alias, settings, cache; keeps config.toml
skill-advisor uninstall --purge-config   # also removes config.toml
uv tool uninstall skill-advisor      # removes the CLI
```

Skills copied into `~/.claude/skills/` via `sync-skills` are **not** removed by
`uninstall` (they are indistinguishable from skills you installed by hand).
Remove them by name if you want to.

`uninstall` deletes `claudeskill-settings.json` (dropping any `effortLevel` baseline the
advisor had written), plus the catalog, embeddings, log, and telemetry files. **As of this
writing it does not yet remove the effort feature's own state** —
`~/.cache/skill-advisor/effort.json`, `observed-effort.json`, `baseline.json`, or
`~/.config/skill-advisor/statusline.sh` — that wiring is tracked as follow-up work. Clean
those up by hand if you want a fully clean slate:

```bash
rm -f ~/.cache/skill-advisor/{effort,observed-effort,baseline}.json \
      ~/.config/skill-advisor/statusline.sh
```

---

## Licensing notes

- **Advisor code (this repo)** is released under the [MIT License](LICENSE).
- **Skills you bring** retain their original licenses. This repo ships no skills, so
  no third-party content is redistributed. If you publish a derived skill bundle
  alongside the advisor, ensure each skill's license is preserved.
- **Embedding model**: the BGE-small ONNX model is downloaded on first run from
  [`qdrant/fastembed`](https://github.com/qdrant/fastembed). Model weights are
  licensed by the upstream authors (BAAI/bge-small-en-v1.5, MIT).
- **No data leaves your machine**. Embeddings are computed locally; the optional
  LLM judge uses your existing `claude -p` install. The opt-in telemetry log is a
  local JSONL file with SHA-256-hashed prompts and session ids.

---

<div align="center">

Built for [Claude Code](https://claude.com/claude-code) · [Report an issue](https://github.com/deep-content/skill-advisor/issues) · [MIT License](LICENSE)

</div>
