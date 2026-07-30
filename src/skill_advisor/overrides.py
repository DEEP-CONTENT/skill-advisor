"""Sole owner of Claude Code's `skillOverrides` map.

Two naming systems collide here and the difference is load-bearing:

  * `settings.json` keys skills by their **directory name**
    (`~/.claude/skills/<dir>/SKILL.md`).
  * `catalog.CatalogEntry.name` is the skill's **frontmatter `name:`**.

They differ for 7 skills on the author's machine — `xlsx` on disk declares
`name: xlsx-official`. Joining on the wrong one silently mis-resolves those
entries, and emitting the wrong one produces a recommendation the Skill tool
rejects. Everything in this module works in directory-name space.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

OFF = "off"


def _dir_name(path: str) -> str:
    return Path(path).parent.name if path else ""


def override_key(*, kind: str, namespace: str, path: str, name: str) -> str | None:
    """The `skillOverrides` key governing this entry, or None if none does.

    `skillOverrides` covers skills only — subagents and slash commands are not
    addressable through it. Plugin skills carry no key today (zero of the 889
    live keys contain a colon), so they resolve to enabled by default.
    """
    if kind != "skill":
        return None
    return _dir_name(path) or name


def invoke_name(*, kind: str, namespace: str, path: str, name: str) -> str:
    """The exact string Claude Code's Skill tool accepts for this entry."""
    if kind != "skill":
        return name
    dirname = _dir_name(path) or name
    if namespace.startswith("plugin:"):
        plugin = namespace.split(":", 1)[1]
        return f"{plugin}:{dirname}"
    return dirname


def _read_one(target: Path) -> dict[str, str]:
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("skillOverrides unreadable in %s: %s", target, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("skillOverrides")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def read(settings_path: Path | None = None) -> dict[str, str]:
    """Merged `skillOverrides`, advisor settings winning per key.

    Never raises and never returns None: a malformed file degrades to `{}`,
    which resolves every skill to enabled — today's behaviour, and strictly
    better than an empty catalog.
    """
    table = _read_one(paths.claude_home() / "settings.json")
    table.update(_read_one(settings_path or paths.settings_file()))
    return table


def is_enabled(key: str | None, table: dict[str, str]) -> bool:
    """True unless the key is explicitly `off`. Absent key ⟹ enabled."""
    if key is None:
        return True
    return table.get(key, "").strip().lower() != OFF
