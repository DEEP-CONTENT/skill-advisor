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
from . import paths
from .config import Config, load as load_config

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class CatalogEntry:
    kind: str         # "skill" | "subagent" | "command"
    name: str
    namespace: str    # "user" | "plugin:<name>" | "builtin"
    description: str
    path: str = ""    # SKILL.md path for skills; "" for builtins

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


def _entries_from_skill_file(skill_md: Path, namespace: str) -> CatalogEntry | None:
    fm = parse_frontmatter(skill_md)
    if not fm:
        return None
    name = fm.get("name")
    description = fm.get("description")
    if not name or not description:
        return None
    return CatalogEntry(
        kind="skill",
        name=str(name).strip(),
        namespace=namespace,
        description=str(description).strip(),
        path=str(skill_md),
    )


def _scan_user_skills(root: Path) -> Iterable[CatalogEntry]:
    if not root.is_dir():
        return
    for skill_md in sorted(root.glob("*/SKILL.md")):
        entry = _entries_from_skill_file(skill_md, namespace="user")
        if entry:
            yield entry


def _scan_plugin_skills(root: Path) -> Iterable[CatalogEntry]:
    if not root.is_dir():
        return
    # Pattern: <marketplace>/plugins/<plugin>/skills/<skill>/SKILL.md
    for skill_md in sorted(root.glob("*/plugins/*/skills/*/SKILL.md")):
        try:
            plugin_name = skill_md.parents[2].name
        except IndexError:
            plugin_name = "unknown"
        entry = _entries_from_skill_file(skill_md, namespace=f"plugin:{plugin_name}")
        if entry:
            yield entry


def _scan_extra_root(root: Path) -> Iterable[CatalogEntry]:
    if not root.is_dir():
        return
    # Accept either a flat skills/<name>/SKILL.md layout or a single SKILL.md file.
    for skill_md in sorted(root.rglob("SKILL.md")):
        entry = _entries_from_skill_file(skill_md, namespace="extra")
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


def scan(config: Config | None = None) -> list[CatalogEntry]:
    cfg = config or load_config()
    exclude = set(cfg.catalog.exclude_names)
    seen: set[tuple[str, str]] = set()
    out: list[CatalogEntry] = []

    def _accept(entry: CatalogEntry) -> None:
        if entry.name in exclude:
            return
        key = (entry.kind, entry.name)
        if key in seen:
            return
        seen.add(key)
        out.append(entry)

    # skill_roots() returns an ordered list of candidate roots — primary
    # Claude home first, then ~/.claude fallback if primary differs. Each root
    # is either a skills/ dir or a plugins/marketplaces dir; detect by name.
    for root in paths.skill_roots():
        if root.name == "skills":
            for entry in _scan_user_skills(root):
                _accept(entry)
        elif root.name == "marketplaces":
            for entry in _scan_plugin_skills(root):
                _accept(entry)

    for extra in cfg.catalog.extra_roots:
        for entry in _scan_extra_root(Path(extra).expanduser()):
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
