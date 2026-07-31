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
import os
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

    Verified live against Claude Code
    (docs/superpowers/notes/2026-07-30-skilloverrides-verification.md, case D):
    a bare directory name in `skillOverrides` (e.g. `{"brainstorming": "off"}`)
    silences only the *user* skill in that directory. A plugin skill living in
    a same-named directory (`superpowers:brainstorming`) is not addressable
    through `skillOverrides` at all and stays enabled regardless of what the
    table contains — the namespaced form is silently ignored too (case C).
    So a plugin entry must resolve to no key, not to the shared dirname.
    """
    if kind != "skill":
        return None
    if namespace.startswith("plugin:"):
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


def write(updates: dict[str, str], *, settings_path: Path | None = None) -> bool:
    """Merge `updates` into skillOverrides in the ADVISOR's settings file.

    Never touches ~/.claude/settings.json — that promise is why this tool is
    safe to run. Same read-modify-write contract as baseline._write_settings_effort,
    including the documented race with install.render_settings(): the later
    writer wins and no lock is taken, judged acceptable for a single-user tool.
    """
    for key in updates:
        if ":" in key:
            raise ValueError(
                f"refusing to write namespaced skillOverrides key {key!r}: "
                "Claude Code silently ignores namespaced keys (plugin skills "
                "are not addressable through skillOverrides at all — see "
                "override_key), so writing one would report success while "
                "changing nothing."
            )

    target = settings_path or paths.settings_file()
    data: dict = {}
    if target.is_file():
        try:
            parsed = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("settings unparseable; skillOverrides write aborted")
            return False
        if not isinstance(parsed, dict):
            log.warning("settings not a JSON object; skillOverrides write aborted")
            return False
        data = parsed

    table = data.get("skillOverrides")
    if not isinstance(table, dict):
        table = {}
    table.update({str(k): str(v) for k, v in updates.items()})
    data["skillOverrides"] = table

    tmp = target.with_suffix(f".json.tmp.{os.getpid()}")
    try:
        paths.ensure_dirs()
        serialised = json.dumps(data, indent=2) + "\n"
        json.loads(serialised)  # round-trip before it touches the real path
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(target)
        return True
    except (OSError, ValueError) as exc:
        log.warning("skillOverrides write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def remove_keys(keys, *, settings_path: Path | None = None) -> bool:
    """Remove exactly `keys` from skillOverrides in the ADVISOR's settings file.

    The surgical counterpart to `write()` — used by `migrate-excludes
    --revert` to undo exactly what a migration added, and nothing else.
    Leaves every other `skillOverrides` key and every other top-level key
    (`effortLevel`, `statusLine`, ...) untouched, because a settings file
    written by THIS tool is not the only writer of it: `baseline.
    _write_settings_effort()` (the Stop hook's effortLevel write-back) and
    `install.render_settings()` both do their own independent
    read-modify-write of the same file, ordinarily. Deleting the whole file
    on revert — rather than removing just the keys a migration added — would
    silently destroy whatever one of those unrelated writers had added since.

    Same atomic contract as `write()`: JSON round-trip validated before it
    touches the real path, atomic replace via a `.json.tmp.<pid>` temp file,
    unlinked on any failure. Returns `False` on any failure leaving the file
    byte-identical. A missing target or a target with no `skillOverrides`
    table is a no-op that returns `True` — there is nothing to remove from.
    """
    target = settings_path or paths.settings_file()
    if not target.is_file():
        return True
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("settings unparseable; skillOverrides removal aborted")
        return False
    if not isinstance(parsed, dict):
        log.warning("settings not a JSON object; skillOverrides removal aborted")
        return False

    table = parsed.get("skillOverrides")
    if isinstance(table, dict):
        for key in keys:
            table.pop(key, None)
        parsed["skillOverrides"] = table

    tmp = target.with_suffix(f".json.tmp.{os.getpid()}")
    try:
        paths.ensure_dirs()
        serialised = json.dumps(parsed, indent=2) + "\n"
        json.loads(serialised)  # round-trip before it touches the real path
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(target)
        return True
    except (OSError, ValueError) as exc:
        log.warning("skillOverrides removal failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def is_enabled(key: str | None, table: dict[str, str]) -> bool:
    """True unless the key is explicitly `off`. Absent key ⟹ enabled."""
    if key is None:
        return True
    return table.get(key, "").strip().lower() != OFF
