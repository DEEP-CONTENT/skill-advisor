"""Catalog of skills, subagents, and slash commands available to the advisor."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import yaml

from . import builtins as builtin_entries
from . import overrides
from . import paths
from .config import Config, load as load_config

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class CatalogEntry:
    kind: str  # "skill" | "subagent" | "command"
    name: str
    namespace: str  # "user" | "plugin:<name>" | "builtin"
    description: str
    path: str = ""  # SKILL.md path for skills; "" for builtins
    enabled: bool = True  # resolved from skillOverrides at scan time
    invoke_name: str = ""  # what the Skill tool accepts; "" ⟹ same as `name`

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "CatalogEntry":
        return cls(**data)

    def embed_text(self) -> str:
        return f"{self.name}: {self.description}"


def parse_frontmatter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _entries_from_skill_file(
    skill_md: Path, namespace: str, table: dict[str, str]
) -> CatalogEntry | None:
    fm = parse_frontmatter(skill_md)
    if not fm:
        return None
    name = fm.get("name")
    description = fm.get("description")
    if not name or not description:
        return None
    name = str(name).strip()
    path = str(skill_md)
    key = overrides.override_key(
        kind="skill", namespace=namespace, path=path, name=name
    )
    return CatalogEntry(
        kind="skill",
        name=name,
        namespace=namespace,
        description=str(description).strip(),
        path=path,
        enabled=overrides.is_enabled(key, table),
        invoke_name=overrides.invoke_name(
            kind="skill", namespace=namespace, path=path, name=name
        ),
    )


def _scan_user_skills(root: Path, table: dict[str, str]) -> Iterable[CatalogEntry]:
    if not root.is_dir():
        return
    for skill_md in sorted(root.glob("*/SKILL.md")):
        entry = _entries_from_skill_file(skill_md, namespace="user", table=table)
        if entry:
            yield entry


def _scan_plugin_skills(root: Path, table: dict[str, str]) -> Iterable[CatalogEntry]:
    if not root.is_dir():
        return
    # Pattern: <marketplace>/plugins/<plugin>/skills/<skill>/SKILL.md
    for skill_md in sorted(root.glob("*/plugins/*/skills/*/SKILL.md")):
        try:
            plugin_name = skill_md.parents[2].name
        except IndexError:
            plugin_name = "unknown"
        entry = _entries_from_skill_file(
            skill_md, namespace=f"plugin:{plugin_name}", table=table
        )
        if entry:
            yield entry


def _scan_plugin_cache_skills(
    root: Path, table: dict[str, str]
) -> Iterable[CatalogEntry]:
    """Installed-plugin layout: <marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md.

    Distinct from `plugins/marketplaces/`, which is the *catalogue* of available
    plugins. The cache is what is actually installed and invocable, and it
    interposes a version segment — so the marketplaces glob misses it entirely.
    """
    if not root.is_dir():
        return
    for skill_md in sorted(root.glob("*/*/*/skills/*/SKILL.md")):
        try:
            plugin_name = skill_md.parents[3].name
        except IndexError:
            plugin_name = "unknown"
        entry = _entries_from_skill_file(skill_md, f"plugin:{plugin_name}", table)
        if entry:
            yield entry


def _scan_extra_root(root: Path, table: dict[str, str]) -> Iterable[CatalogEntry]:
    if not root.is_dir():
        return
    # Accept either a flat skills/<name>/SKILL.md layout or a single SKILL.md file.
    for skill_md in sorted(root.rglob("SKILL.md")):
        entry = _entries_from_skill_file(skill_md, namespace="extra", table=table)
        if entry:
            yield entry


def _builtin_entries() -> Iterable[CatalogEntry]:
    for item in builtin_entries.SUBAGENTS:
        yield CatalogEntry(
            kind="subagent",
            name=item["name"],
            namespace="builtin",
            description=item["description"],
        )
    for item in builtin_entries.COMMANDS:
        yield CatalogEntry(
            kind="command",
            name=item["name"],
            namespace="builtin",
            description=item["description"],
        )


def scan(
    config: Config | None = None,
    *,
    overrides_table: dict[str, str] | None = None,
) -> list[CatalogEntry]:
    cfg = config or load_config()
    table = overrides_table if overrides_table is not None else overrides.read()
    exclude = set(cfg.catalog.exclude_names)
    seen: set[tuple[str, str]] = set()
    out: list[CatalogEntry] = []

    def _accept(entry: CatalogEntry) -> None:
        if entry.name in exclude:
            return
        key = (entry.kind, entry.invoke_name or entry.name)
        if key in seen:
            return
        seen.add(key)
        out.append(entry)

    # skill_roots() returns an ordered list of candidate roots — primary
    # Claude home first, then ~/.claude fallback if primary differs. Each root
    # is either a skills/ dir, a plugins/marketplaces dir, or a plugins/cache dir;
    # detect by name.
    for root in paths.skill_roots():
        if root.name == "skills":
            for entry in _scan_user_skills(root, table):
                _accept(entry)
        elif root.name == "marketplaces":
            for entry in _scan_plugin_skills(root, table):
                _accept(entry)
        elif root.name == "cache":
            for entry in _scan_plugin_cache_skills(root, table):
                _accept(entry)

    for extra in cfg.catalog.extra_roots:
        for entry in _scan_extra_root(Path(extra).expanduser(), table):
            _accept(entry)

    for entry in _builtin_entries():
        _accept(entry)

    return out


def compute_hash(entries: list[CatalogEntry]) -> str:
    """Fingerprint source-file mtimes + builtin entry descriptions.

    Used so `skill-advisor build` is a no-op when nothing changed.
    """
    h = hashlib.sha256()
    for e in entries:
        h.update(e.kind.encode())
        h.update(b"\x00")
        h.update(e.name.encode())
        h.update(b"\x00")
        h.update(b"1" if e.enabled else b"0")
        h.update(b"\x00")
        if e.path:
            try:
                mtime = Path(e.path).stat().st_mtime_ns
            except OSError:
                mtime = 0
            h.update(str(mtime).encode())
        else:
            h.update(e.description.encode())
        h.update(b"\n")
    return h.hexdigest()


def save(entries: list[CatalogEntry], target: Path | None = None) -> Path:
    dest = target or paths.catalog_file()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps([e.to_json() for e in entries], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return dest


def load(source: Path | None = None) -> list[CatalogEntry]:
    src = source or paths.catalog_file()
    if not src.is_file():
        raise FileNotFoundError(f"catalog not built yet; expected {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    return [CatalogEntry.from_json(item) for item in data]


def pool_health(
    config: Config | None = None,
    *,
    overrides_table: dict[str, str] | None = None,
) -> dict:
    """Counts for `doctor`: how much of the disk the scanner can actually see.

    About ~20% of SKILL.md files carry no parseable YAML frontmatter and are
    silently invisible. Reporting that is deliberate — the files belong to
    third-party skill libraries and fixing them is out of scope, but a rotation
    pool that silently excludes a fifth of the disk should say so.
    """
    cfg = config or load_config()
    table = overrides_table if overrides_table is not None else overrides.read()
    files = 0
    unparseable_dirs: list[str] = []
    for root in paths.skill_roots():
        for skill_md in root.rglob("SKILL.md"):
            files += 1
            fm = parse_frontmatter(skill_md)
            if not fm or not fm.get("name") or not fm.get("description"):
                unparseable_dirs.append(skill_md.parent.name)
    unparseable_count = len(unparseable_dirs)
    entries = scan(cfg, overrides_table=table)
    off_keys = {k for k, v in table.items() if v.strip().lower() == overrides.OFF}
    excluded = set(cfg.catalog.exclude_names)
    return {
        "skill_md_files": files,
        "parseable": files - unparseable_count,
        "unparseable_count": unparseable_count,
        "unparseable_dirs": sorted(set(unparseable_dirs)),
        "pool": len(entries),
        "pickable": sum(1 for e in entries if e.enabled),
        "excluded_but_enabled": sorted(excluded - off_keys),
    }
