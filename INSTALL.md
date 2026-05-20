# Installing skill-advisor

A step-by-step guide for onboarding. You need Claude Code, Python 3.11+, and
either `uv` or `pipx` to install the tool.

## 1. Prerequisites

```bash
claude --version         # must be installed and on PATH
python3 --version        # 3.11 or newer
which uv || which pipx   # one of these
```

If `uv` is missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
If `pipx` is missing: `python3 -m pip install --user pipx && pipx ensurepath`.

## 2. Install the package

```bash
git clone https://github.com/deep-content/skill-advisor.git
cd skill-advisor
uv tool install .        # or: pipx install .
skill-advisor --help     # confirm the CLI is on PATH
```

## 3. Bring your own skill library (optional)

The advisor recommends from whatever Claude Code can already invoke under
`~/.claude/skills/` — this repo does not ship a skill bundle. If you already
have skills installed (manually, via plugins, or via a team dotfiles repo),
skip to step 4.

If you maintain an external library of `SKILL.md` folders elsewhere,
`skill-advisor sync-skills` will copy them into your Claude home:

```bash
skill-advisor sync-skills --from /path/to/your/skill-library
skill-advisor sync-skills --from /path/to/your/skill-library --dry-run
skill-advisor sync-skills --from /path/to/your/skill-library --force
```

Non-destructive by default: any skill you already have in `~/.claude/skills/`
is left alone. `--force` overwrites; `--dry-run` previews. The source must
contain top-level subdirectories each with a `SKILL.md` file.

## 4. Wire it into Claude Code

```bash
skill-advisor install
```

**Safe by default** — this does NOT edit your shell rc file. It only:

1. Renders `~/.config/skill-advisor/claudeskill-settings.json` with the absolute path
   to your installed `skill-advisor` binary (no repo-relative paths leak).
2. Writes a default `~/.config/skill-advisor/config.toml` (skipped if present).
3. Builds the catalog + embeddings (first run downloads ~15 MB).
4. Detects any existing `claudeskill` alias/function/binary and prints the exact
   `--settings` flag you should add to it.
5. Warns if `CLAUDE_CONFIG_DIR` is set, confirming the resolved Claude home.

You activate the advisor by passing the `--settings` flag to `claude`.

### If you already have a `claudeskill` wrapper

Common pattern — work/private credential split via `CLAUDE_CONFIG_DIR`:

```bash
# Before:
alias claudeskill='CLAUDE_CONFIG_DIR=~/.claude-work claude'

# After — add --settings, keep the rest:
alias claudeskill='CLAUDE_CONFIG_DIR=~/.claude-work claude --settings ~/.config/skill-advisor/claudeskill-settings.json'
```

The `--settings` flag merges with your user settings chain. It does NOT override
`CLAUDE_CONFIG_DIR`, credentials, existing hooks, permissions, or MCP servers.

### If you have no existing claudeskill wrapper

Let the installer write one to your shell rc:

```bash
skill-advisor install --write-alias
source ~/.zshrc   # or your shell's rc
```

### One-off activation (no alias at all)

```bash
claude --settings ~/.config/skill-advisor/claudeskill-settings.json
```

## 5. Verify

```bash
skill-advisor doctor
tail -f ~/.cache/skill-advisor/advisor.log
```

Type a substantive prompt (`"write a react counter"`). You should see a
`picks=[...]` log line. Type `"thanks"` — the advisor should skip silently.

If Claude Code doesn't invoke the suggested skill, the context injection is
working but the model judged the skill not appropriate; lower the
`budget_seconds` or tune `config.toml`.

### Try lifecycle mode

Type a task-shaped prompt like `"build a rate limiter for the api endpoints"`
in `claudeskill` — the advisor starts a `planning → implementation → review → …`
state machine for that session. Reply with `"go"` to advance, `"looks good"`
to complete, `"cancel"` to stop. See the
[Lifecycle mode](README.md#lifecycle-mode) section of the README for the full
flow, phase preferences, and inspection commands (`skill-advisor lifecycle status`).

## 6. Keeping the catalog fresh

After installing or removing skills:

```bash
skill-advisor build
```

The rebuild is a no-op if the source-file mtimes haven't changed.

## 7. Uninstall

```bash
skill-advisor uninstall          # keeps config.toml
skill-advisor uninstall --purge-config
uv tool uninstall skill-advisor  # or: pipx uninstall skill-advisor
```

## Troubleshooting

### `claudeskill: command not found`

You skipped `source ~/.zshrc`. Open a new shell or source the rc file.

### `skill-advisor: command not found` after `uv tool install .`

`uv tool install` puts binaries in `~/.local/bin`. Make sure it is on your PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Every prompt sits silent with no picks

Check `~/.cache/skill-advisor/advisor.log`. Common causes:

- `claude` binary not on the PATH that the hook inherits → `skill-advisor doctor`
  will show `claude on PATH : MISSING`.
- Catalog is empty → run `skill-advisor build` and inspect
  `~/.cache/skill-advisor/catalog.json`.
- Budget too tight → bump `budget_seconds` in `config.toml`.

### Model keeps suggesting the wrong skill

Bump `matcher.model` to `claude-opus-4-7` in `config.toml` for better
ranking quality (at the cost of latency).

### Windows

Not supported in v1. Use WSL2, or open a PR wiring PowerShell profile editing
into `install.py`.
