"""`skill-advisor install` / `uninstall` — wires the framework into the user's shell."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import paths
from . import statusline
from .config import load as load_config

ALIAS_BEGIN = "# >>> skill-advisor alias >>>"
ALIAS_END = "# <<< skill-advisor alias <<<"


@dataclass(frozen=True)
class ShellTarget:
    name: str           # "bash" | "zsh" | "fish"
    rc_file: Path
    alias_line: str


def detect_shell() -> ShellTarget | None:
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    shell = (os.environ.get("SHELL") or "").split("/")[-1]
    settings_path = paths.settings_file()

    if shell == "fish":
        return ShellTarget(
            name="fish",
            rc_file=home / ".config" / "fish" / "config.fish",
            alias_line=f'alias claudeskill "claude --settings \\"{settings_path}\\""',
        )
    if shell == "zsh":
        return ShellTarget(
            name="zsh",
            rc_file=home / ".zshrc",
            alias_line=f"alias claudeskill='claude --settings \"{settings_path}\"'",
        )
    if shell == "bash":
        # macOS uses .bash_profile; Linux uses .bashrc.
        bashrc = home / ".bashrc"
        rc = bashrc if bashrc.exists() else home / ".bash_profile"
        return ShellTarget(
            name="bash",
            rc_file=rc,
            alias_line=f"alias claudeskill='claude --settings \"{settings_path}\"'",
        )
    return None


def _advisor_command() -> str:
    """Absolute path to the installed `skill-advisor` entry point."""
    resolved = shutil.which("skill-advisor")
    if resolved:
        return resolved
    # Fallback: rely on PATH at hook runtime (installer warns).
    return "skill-advisor"


_ADVISOR_SUBCOMMANDS = ("hook", "posttooluse", "stop")


def _is_advisor_command(command: str, subcommand: str) -> bool:
    """True if `command` ends with `<path>/skill-advisor <subcommand>` or bare form."""
    parts = (command or "").split()
    if len(parts) < 2 or parts[-1] != subcommand:
        return False
    binary = parts[-2]
    return binary == "skill-advisor" or binary.endswith("/skill-advisor")


def _merge_hook_entry(
    hooks_by_event: dict,
    event_name: str,
    command: str,
    *,
    timeout_ms: int = 3000,
) -> None:
    """Idempotently install our advisor hook for `event_name`.

    Finds any existing entry whose command is also `skill-advisor <subcommand>`
    and replaces it in place (handles path changes across reinstalls). If none
    exists, appends a fresh matcher block. User-defined hooks for the same
    event are left untouched.
    """
    subcommand = command.rsplit(" ", 1)[-1]
    blocks = hooks_by_event.setdefault(event_name, [])
    if not isinstance(blocks, list):
        blocks = []
        hooks_by_event[event_name] = blocks

    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_hooks = block.get("hooks")
        if not isinstance(block_hooks, list):
            continue
        for i, hook in enumerate(block_hooks):
            if not isinstance(hook, dict):
                continue
            cmd = hook.get("command", "")
            if _is_advisor_command(cmd, subcommand):
                block_hooks[i] = {
                    "type": "command",
                    "command": command,
                    "timeout": timeout_ms,
                }
                return

    blocks.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": command, "timeout": timeout_ms}],
    })


def render_settings() -> Path:
    """Write/merge the Claude Code settings file at `paths.settings_file()`.

    Merge semantics (per design decision 4 of the Stop/PostToolUse feature):
    read the existing file if present, upsert our UserPromptSubmit / PostToolUse
    / Stop hook entries by their sentinel command strings, leave every other
    hook block alone. Idempotent — re-runs yield no diff once paths are stable.
    When the effort feature is enabled, also upserts a `statusLine` key pointing
    at our generated script by exact path match — any `statusLine` whose command
    is not exactly our script's path is treated as user-owned and left untouched.
    """
    paths.ensure_dirs()
    target = paths.settings_file()

    data: dict = {}
    if target.is_file():
        try:
            parsed = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    advisor = _advisor_command()
    _merge_hook_entry(hooks, "UserPromptSubmit", f"{advisor} hook")
    _merge_hook_entry(hooks, "PostToolUse", f"{advisor} posttooluse")
    _merge_hook_entry(hooks, "Stop", f"{advisor} stop")

    try:
        cfg = load_config()
    except Exception:  # pragma: no cover - defensive; installer must not crash
        cfg = None

    if cfg is not None and cfg.effort.enabled and cfg.effort.statusline:
        existing = data.get("statusLine")
        expected = str(paths.statusline_script())
        existing_cmd = existing.get("command") if isinstance(existing, dict) else None
        is_foreign = isinstance(existing_cmd, str) and existing_cmd != expected
        if not is_foreign:
            ours = str(statusline.write_script())
            data["statusLine"] = {"type": "command", "command": ours}

    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target


def _default_config_text() -> str:
    # Bundled directly in source so the tool works without shipping examples/.
    return """# skill-advisor config — edit to taste; re-run `skill-advisor build` after changes.

[matcher]
# Enable the `claude -p` judge for re-ranking the embedding shortlist.
# Trade-off:
#   false (default) — embedding top-K is the final ranking. ~50-200 ms per prompt.
#   true            — claude -p (Haiku) re-ranks for higher precision. Adds 5-15 s
#                     of overhead due to Claude Code session startup costs
#                     (skills list + session hooks). Only enable if you need the
#                     higher accuracy and can tolerate the latency.
use_judge = false

# Judge model (only used when use_judge = true).
model = "claude-haiku-4-5-20251001"

# Embedding-shortlist size — also the candidate list sent to the judge when enabled.
max_candidates = 15

# Max picks surfaced in additionalContext.
max_picks = 3

# Total hook budget in seconds. Hook exits silent if exceeded — your prompt
# always goes through, even when the advisor can't answer in time.
# Bump to ~15.0 if you enable use_judge = true.
budget_seconds = 4.0

# Minimum cosine score to surface a pick in embedding-only mode.
# Range 0-1; 0.35 filters out weak matches on unrelated prompts.
min_embedding_score = 0.35

[catalog]
# Additional directories to scan for SKILL.md files (e.g. team-internal skills).
extra_roots = []
# Catalog names to suppress (noisy or irrelevant skills).
exclude_names = []

[triage]
# Prompts shorter than this (in words) are skipped unless they contain technical keywords.
skip_if_shorter_than = 6
# Extra regex patterns (case-insensitive) that cause the advisor to skip.
extra_skip_patterns = []

[lifecycle]
# Master toggle. Set to false to disable lifecycle mode entirely — the advisor
# stays stateless and never injects a phase banner.
enabled = true

# Max correction cycles before the advisor forces `complete`.
max_correction_cycles = 3

# Extra regexes that should be treated as lifecycle triggers in addition to the
# built-in verbs (build / implement / refactor / add / migrate / …).
# extra_trigger_patterns = ["\\\\bstand\\\\s+up\\\\b", "\\\\bport\\\\s+over\\\\b"]
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

[lifecycle.auto_advance]
# Opt-in: let Claude Code's Stop hook push phases forward when the model's
# last turn has completed implementation work (Edit/Write/NotebookEdit/Bash)
# or when a Plan subagent finished. REVIEW → CORRECTION stays user-driven.
# Requires re-running `skill-advisor install` after flipping to True so the
# PostToolUse + Stop hook entries land in claudeskill-settings.json.
enabled = false
on_plan_subagent_done = true
on_edit_stop = true

[telemetry]
# Opt-in structured event log at ~/.cache/skill-advisor/advisor.events.jsonl.
# When enabled, every hook firing records: timestamp, hashed session + prompt,
# picks (name/kind/score), phase, triage decision, duration. Session ids and
# prompts are ALWAYS hashed; no plaintext ever lands on disk.
#
# Consumed by `skill-advisor report` for pick frequency, dead-skill detection,
# and latency stats. Off by default.
events_enabled = false

# Informational retention target in days. Enforcement is manual — run
# `skill-advisor report --purge-older-than 90d` periodically if you care.
retain_days = 90

# Hash salt for prompt + session id SHAs. Leave empty to auto-generate a salt
# on first write (stored at ~/.cache/skill-advisor/telemetry.salt, mode 0600).
# Wipe the salt file to anonymize historical data.
prompt_hash_salt = ""

# Parallelization-check feature. When enabled, the advisor watches for multi-item
# TodoWrite events during the planning phase and asks `claude -p` whether the
# tasks can be executed as parallel subagents in isolated worktrees. Off by
# default. Enabling requires matcher.budget_seconds >= 23.
# [parallelization]
# enabled = false
# min_tasks = 3
# judge_timeout_seconds = 20.0

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
"""


def write_default_config(force: bool = False) -> Path:
    paths.ensure_dirs()
    target = paths.config_file()
    if target.is_file() and not force:
        return target
    target.write_text(_default_config_text(), encoding="utf-8")
    return target


def _read_rc(rc: Path) -> str:
    if not rc.is_file():
        return ""
    return rc.read_text(encoding="utf-8")


def _strip_block(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == ALIAS_BEGIN:
            skipping = True
            continue
        if line.strip() == ALIAS_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    result = "\n".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def install_alias(shell: ShellTarget) -> bool:
    """Idempotent. Returns True if the rc file changed."""
    shell.rc_file.parent.mkdir(parents=True, exist_ok=True)
    original = _read_rc(shell.rc_file)
    stripped = _strip_block(original)
    block = f"\n{ALIAS_BEGIN}\n{shell.alias_line}\n{ALIAS_END}\n"
    updated = (stripped.rstrip("\n") + "\n" + block) if stripped.strip() else block.lstrip("\n")
    if updated == original:
        return False
    shell.rc_file.write_text(updated, encoding="utf-8")
    return True


def uninstall_alias(shell: ShellTarget) -> bool:
    if not shell.rc_file.is_file():
        return False
    original = _read_rc(shell.rc_file)
    stripped = _strip_block(original)
    if stripped == original:
        return False
    shell.rc_file.write_text(stripped, encoding="utf-8")
    return True
