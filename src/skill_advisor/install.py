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


class RenderSettingsError(RuntimeError):
    """`render_settings()` could not prepare or write its target settings file.

    Raised instead of letting the underlying OSError propagate as a raw
    traceback — the message names the resolved path and, when set, the
    `SKILL_ADVISOR_SETTINGS_FILE` override responsible for it, so a typo'd or
    permission-restricted override fails with something the user can act on.
    """


class AliasQuotingError(ValueError):
    """A settings path can't be safely embedded in a shell alias/function (SEC-MIT-001)."""


def _reject_newline(value: str) -> None:
    if "\n" in value or "\r" in value:
        raise AliasQuotingError(
            "settings path contains a newline and cannot be safely quoted into a "
            "shell alias/function"
        )


def ps_single_quote(value: str) -> str:
    """Quote `value` as a PowerShell single-quoted string literal (SEC-MIT-001).

    PowerShell single-quoted strings are fully literal — the only metacharacter is
    the single quote itself, escaped by doubling it (`'` → `''`). A single-line
    statement can't carry a newline, so such a path is rejected rather than written
    as a broken function body.
    """
    _reject_newline(value)
    return "'" + value.replace("'", "''") + "'"


def _posix_double_quote(value: str) -> str:
    """Quote `value` for the inner double-quoted argument of a POSIX command.

    Escapes the four characters that stay special inside double quotes so a path
    with spaces, quotes, `$` or backticks survives intact at runtime.
    """
    for ch in ("\\", '"', "$", "`"):
        value = value.replace(ch, "\\" + ch)
    return '"' + value + '"'


def _posix_single_quote(value: str) -> str:
    """Wrap `value` in POSIX single quotes (bash/zsh): close, escaped quote, reopen."""
    return "'" + value.replace("'", "'\\''") + "'"


def _fish_single_quote(value: str) -> str:
    """Wrap `value` in fish single quotes — only `\\` and `'` are special inside."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


@dataclass(frozen=True)
class ShellTarget:
    name: str  # "bash" | "zsh" | "fish" | "powershell"
    rc_file: Path
    alias_line: str
    # PowerShell (and any non-alias target) carries its block body here instead of
    # `alias_line`; `install_alias` writes `snippet` verbatim when it's non-empty.
    snippet: str = ""


def _is_windows() -> bool:
    return os.name == "nt"


def _documents_dir() -> Path:
    """The user's real Documents folder, honouring OneDrive Known Folder Move.

    `$USERPROFILE/Documents` is WRONG on any machine where OneDrive has taken
    over the Documents known folder — the real path is then something like
    `C:/Users/<user>/OneDrive/05 - Dokumente`, localised folder name and all.
    Writing the `claudeskill` function into the literal `Documents` path there
    produces a file PowerShell never loads: install reports success and nothing
    happens, which is worse than failing.

    The registry's `User Shell Folders\\Personal` value is the authority Windows
    itself consults, so read that first and expand its embedded `%USERPROFILE%`.
    Falls back to the literal path when the registry is unavailable (non-Windows
    hosts running the test suite, a stripped-down image, a permission error).

    `SKILL_ADVISOR_PS_PROFILE` overrides everything — the escape hatch for a
    machine whose profile lives somewhere neither heuristic finds.
    """
    override = os.environ.get("SKILL_ADVISOR_PS_PROFILE")
    if override:
        return Path(override).parent

    userprofile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if _is_windows():
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            try:
                raw, _ = winreg.QueryValueEx(key, "Personal")
            finally:
                winreg.CloseKey(key)
            expanded = os.path.expandvars(str(raw)).strip()
            if expanded and "%" not in expanded:
                return Path(expanded)
        except OSError:
            pass
    return Path(userprofile) / "Documents"


def _powershell_profile_path() -> Path:
    """pwsh 7 profile path, resolved against the REAL Documents folder."""
    override = os.environ.get("SKILL_ADVISOR_PS_PROFILE")
    if override:
        return Path(override)
    return _documents_dir() / "PowerShell" / "Microsoft.PowerShell_profile.ps1"


def detect_shell() -> ShellTarget | None:
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    shell = (os.environ.get("SHELL") or "").split("/")[-1]
    settings_path = str(paths.settings_file())
    _reject_newline(settings_path)

    if shell == "fish":
        body = "claude --settings " + _posix_double_quote(settings_path)
        return ShellTarget(
            name="fish",
            rc_file=home / ".config" / "fish" / "config.fish",
            alias_line="alias claudeskill " + _fish_single_quote(body),
        )
    if shell == "zsh":
        body = "claude --settings " + _posix_double_quote(settings_path)
        return ShellTarget(
            name="zsh",
            rc_file=home / ".zshrc",
            alias_line="alias claudeskill=" + _posix_single_quote(body),
        )
    if shell == "bash":
        # macOS uses .bash_profile; Linux uses .bashrc.
        bashrc = home / ".bashrc"
        rc = bashrc if bashrc.exists() else home / ".bash_profile"
        body = "claude --settings " + _posix_double_quote(settings_path)
        return ShellTarget(
            name="bash",
            rc_file=rc,
            alias_line="alias claudeskill=" + _posix_single_quote(body),
        )
    # Windows without a Unix `$SHELL` → a PowerShell profile function (pwsh 7).
    if _is_windows() and not shell:
        return ShellTarget(
            name="powershell",
            rc_file=_powershell_profile_path(),
            alias_line="",
            snippet=(
                "function claudeskill { claude --settings "
                f"{ps_single_quote(settings_path)} @args }}"
            ),
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
_ADVISOR_BASENAMES = {"skill-advisor", "skill-advisor.exe"}


def _advisor_basename(binary: str) -> str:
    """Separator-agnostic, lowercased basename of a binary path.

    Splits on both `/` and `\\` so a Windows path (`C:\\Tools\\skill-advisor.exe`)
    resolves identically on POSIX test hosts, then strips any surrounding quotes
    left over from a quoted command string.
    """
    binary = binary.strip()
    if len(binary) >= 2 and binary[0] == binary[-1] and binary[0] in ("'", '"'):
        binary = binary[1:-1]
    return binary.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _is_advisor_command(command: str, subcommand: str) -> bool:
    """True if `command` is `<path>/skill-advisor <subcommand>` (or the bare form).

    Platform-neutral: a Windows `skill-advisor.exe` with backslash separators and
    a path containing spaces (quoted or not) is recognised the same as a POSIX
    `/usr/bin/skill-advisor`. Detection works off the trailing subcommand token
    plus the basename of everything before it, so it never naively `split()`s a
    path that contains spaces.
    """
    command = (command or "").strip()
    suffix = " " + subcommand
    if subcommand not in _ADVISOR_SUBCOMMANDS or not command.endswith(suffix):
        return False
    binary = command[: -len(suffix)].strip()
    if not binary:
        return False
    return _advisor_basename(binary) in _ADVISOR_BASENAMES


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

    blocks.append(
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": command, "timeout": timeout_ms}],
        }
    )


def render_settings() -> Path:
    """Write/merge the Claude Code settings file at `paths.settings_file()`.

    Merge semantics (per design decision 4 of the Stop/PostToolUse feature):
    read the existing file if present, upsert our UserPromptSubmit / PostToolUse
    / Stop hook entries by their sentinel command strings, leave every other
    hook block alone. Idempotent — re-runs yield no diff once paths are stable.
    When the effort feature is enabled, also upserts a `statusLine` key pointing
    at our generated script by exact path match — any `statusLine` whose command
    is not exactly our script's path is treated as user-owned and left untouched.

    Raises `RenderSettingsError` (not the raw OSError) if the settings file's
    directory can't be prepared — most likely a typo'd or permission-restricted
    `SKILL_ADVISOR_SETTINGS_FILE`. Deliberately not swallowed: the caller must
    know install did not complete.
    """
    target = paths.settings_file()
    try:
        paths.ensure_dirs()
    except OSError as exc:
        override = os.environ.get("SKILL_ADVISOR_SETTINGS_FILE")
        source = (
            f"SKILL_ADVISOR_SETTINGS_FILE={override!r}"
            if override
            else "the default config directory"
        )
        raise RenderSettingsError(
            f"could not prepare the directory for the settings file at {target} "
            f"(from {source}): {exc}"
        ) from exc

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
budget_seconds = 8.0

# Minimum cosine score to surface a pick in embedding-only mode.
# Range 0-1; 0.35 filters out weak matches on unrelated prompts.
min_embedding_score = 0.35

[catalog]
# Additional directories to scan for SKILL.md files (e.g. team-internal skills).
extra_roots = []
# Catalog names to suppress (noisy or irrelevant skills).
exclude_names = []
# Optional path to a curated catalog manifest (JSON). Leave empty to keep the
# default behaviour — the catalog scans everything and only exclude_names applies.
# When set, the manifest is the structural source of truth for exclusions
# (excluded sub-skills/sub-agents, plugin whitelist); exclude_names stays additive.
# A missing or malformed manifest never breaks the advisor (fail-soft).
# catalog_manifest = "~/.claude/skill-advisor-manifest.json"
catalog_manifest = ""

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
# default. Enabling requires matcher.budget_seconds >= judge_timeout_seconds + 3.
# [parallelization]
# enabled = false
# min_tasks = 3
# judge_timeout_seconds = 5.0

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
    # `snippet` (PowerShell function) wins over `alias_line` when present; the
    # block mechanism stays content-agnostic either way.
    block_body = shell.snippet if shell.snippet else shell.alias_line
    block = f"\n{ALIAS_BEGIN}\n{block_body}\n{ALIAS_END}\n"
    updated = (
        (stripped.rstrip("\n") + "\n" + block)
        if stripped.strip()
        else block.lstrip("\n")
    )
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
