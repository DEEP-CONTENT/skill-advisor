"""XDG-aware filesystem paths.

Single source of truth — no other module calls `os.path.expanduser` directly.
All functions respect environment variables (`CLAUDE_HOME`, `XDG_CONFIG_HOME`,
`XDG_CACHE_HOME`, `SKILL_ADVISOR_CONFIG_HOME`, `SKILL_ADVISOR_CACHE_HOME`)
so tests and multi-user installs stay isolated.
"""
from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def claude_home() -> Path:
    """Current Claude Code config dir.

    Precedence:
      1. `CLAUDE_HOME`        — test/override escape hatch for this framework.
      2. `CLAUDE_CONFIG_DIR`  — Claude Code's official env var. Users with separate
         private vs work credential dirs (e.g. `~/.claude` vs `~/.claude-work`)
         flip between them by setting this.
      3. `$HOME/.claude`      — default.
    """
    override = os.environ.get("CLAUDE_HOME")
    if override:
        return Path(override)
    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_config:
        return Path(claude_config)
    return _home() / ".claude"


def skill_roots() -> list[Path]:
    """Roots scanned for SKILL.md files.

    The primary home (honouring `CLAUDE_CONFIG_DIR`) is scanned first. When that's
    different from `$HOME/.claude` — e.g. under a work credential dir whose
    `skills/` is empty — the default `~/.claude` skills are also included so the
    catalog isn't empty. Catalog dedupes by (kind, name); the primary root wins on
    collision.

    Dedupes by resolved path to handle cases where primary subdirectories are
    symlinks to the default home's subdirectories.
    """
    primary = claude_home()
    default = _home() / ".claude"
    candidates = [
        primary / "skills",
        primary / "plugins" / "marketplaces",
        primary / "plugins" / "cache",
    ]
    if primary.resolve() != default.resolve():
        for extra in (
            default / "skills",
            default / "plugins" / "marketplaces",
            default / "plugins" / "cache",
        ):
            candidates.append(extra)

    # Dedupe by resolved path, keeping first occurrence of each unique target.
    seen: set[Path] = set()
    roots: list[Path] = []
    for root in candidates:
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            roots.append(root)
    return roots


def _dual_roots(*subparts: str) -> list[Path]:
    """Primary-home roots with `~/.claude` fallback appended when they differ.

    Mirrors `skill_roots()`'s fallback contract for any subdir under the Claude
    home: the primary home (honouring `CLAUDE_CONFIG_DIR`) is listed first; when
    that's different from `$HOME/.claude` the default location is appended so the
    scan isn't blind to it. Never emits duplicates.
    """
    primary = claude_home()
    default = _home() / ".claude"
    roots = [primary.joinpath(*subparts)]
    if primary.resolve() != default.resolve():
        extra = default.joinpath(*subparts)
        if extra not in roots:
            roots.append(extra)
    return roots


def agent_roots() -> list[Path]:
    """Roots scanned for subagent definition files (`agents/*.md`).

    Same primary/default-fallback logic as `skill_roots()`: the active Claude
    home's `agents/` first, then `~/.claude/agents` as fallback when the primary
    home differs (e.g. under a work `CLAUDE_CONFIG_DIR`).
    """
    return _dual_roots("agents")


def command_roots() -> list[Path]:
    """Roots scanned for slash-command definition files (`commands/*.md`).

    Same primary/default-fallback logic as `agent_roots()`.
    """
    return _dual_roots("commands")


def config_dir() -> Path:
    override = os.environ.get("SKILL_ADVISOR_CONFIG_HOME")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "skill-advisor"


def cache_dir() -> Path:
    override = os.environ.get("SKILL_ADVISOR_CACHE_HOME")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or str(_home() / ".cache")
    return Path(base) / "skill-advisor"


def fastembed_cache_dir() -> Path:
    """Persistent cache dir for the fastembed model weights (BGE-small ONNX).

    Pinning this keeps the ~15 MB model out of `~/.cache/fastembed` and off the
    OS temp dir, so it survives temp cleanup instead of re-downloading on every
    cold start. `index._embed_model()` passes it to `TextEmbedding(cache_dir=...)`.

    Precedence:
      1. `SKILL_ADVISOR_FASTEMBED_CACHE` — explicit override.
      2. `cache_dir() / 'fastembed'`     — default, inside our XDG cache so test
         isolation (`SKILL_ADVISOR_CACHE_HOME`) redirects it too.
    """
    override = os.environ.get("SKILL_ADVISOR_FASTEMBED_CACHE")
    if override:
        return Path(override)
    return cache_dir() / "fastembed"


def config_file() -> Path:
    return config_dir() / "config.toml"


def settings_file() -> Path:
    """Claude Code `--settings` file this tool reads and writes.

    Precedence:
      1. `SKILL_ADVISOR_SETTINGS_FILE` — full path override (not just a filename).
         Set this when your `claude --settings ...` invocation already points at a
         file that isn't named `claudeskill-settings.json` (e.g. a pre-existing
         settings file with your own hooks/statusLine in it). Without this, the
         installer and the effort write-back would create/target a SECOND file
         your actual `claude` invocation never reads — silently inert.
      2. `config_dir() / "claudeskill-settings.json"` — default.
    """
    override = os.environ.get("SKILL_ADVISOR_SETTINGS_FILE")
    if override:
        return Path(override)
    return config_dir() / "claudeskill-settings.json"


def catalog_file() -> Path:
    return cache_dir() / "catalog.json"


def embeddings_file() -> Path:
    return cache_dir() / "embeddings.npz"


def catalog_hash_file() -> Path:
    return cache_dir() / "catalog.hash"


def log_file() -> Path:
    return cache_dir() / "advisor.log"


def events_file() -> Path:
    return cache_dir() / "advisor.events.jsonl"


def telemetry_salt_file() -> Path:
    return cache_dir() / "telemetry.salt"


def effort_file() -> Path:
    """Latest effort recommendation, written by the UserPromptSubmit hook."""
    return cache_dir() / "effort.json"


def observed_effort_file() -> Path:
    """Live effort level as last seen by the status line (the sensor)."""
    return cache_dir() / "observed-effort.json"


def baseline_file() -> Path:
    """Rolling window + write-back provenance."""
    return cache_dir() / "baseline.json"


def centroids_file() -> Path:
    """Fixed-size online sketch of prompt embeddings (8 x 384 float32, ~12 KB).

    Holds no prompt text and no per-prompt vectors — it is a lossy aggregate of
    thousands of prompts, not a record of any one of them. Written only when
    telemetry.events_enabled is already true; removed by `uninstall`.
    """
    return cache_dir() / "centroids.npz"


def statusline_script() -> Path:
    """Generated POSIX-sh status line, registered in claudeskill-settings.json."""
    return config_dir() / "statusline.sh"


def sessions_dir() -> Path:
    return cache_dir() / "sessions"


def session_file(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:128] or "default"
    return sessions_dir() / f"{safe}.json"


def turn_file(session_id: str) -> Path:
    """Per-turn ephemeral state file: tool_names + subagents_invoked so far in this turn.

    Cleared by the Stop hook after it decides whether to auto-advance.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:128] or "default"
    return sessions_dir() / f"{safe}.turn.json"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    cache_dir().mkdir(parents=True, exist_ok=True)
    sessions_dir().mkdir(parents=True, exist_ok=True)
    # settings_file()'s parent is config_dir() by default (already covered above),
    # but SKILL_ADVISOR_SETTINGS_FILE may point anywhere. Both writers
    # (install.render_settings, baseline._write_settings_effort) call
    # ensure_dirs() before writing and neither is allowed to raise, so this must
    # create the override's parent too — same trust model this module already
    # applies to SKILL_ADVISOR_CONFIG_HOME / SKILL_ADVISOR_CACHE_HOME above.
    settings_file().parent.mkdir(parents=True, exist_ok=True)
