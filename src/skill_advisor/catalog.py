"""Catalog of skills, subagents, and slash commands available to the advisor."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterable

import yaml

from . import builtins as builtin_entries
from . import manifest as manifest_loader
from . import overrides
from . import paths
from .config import Config, load as load_config

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _norm_name(name: str) -> str:
    """Normalize a name for slash-insensitive exclusion matching.

    Commands carry a leading slash (``/loop``) while a config or manifest may
    name them bare (``loop``); stripping a single leading slash lets either form
    exclude the other. Only one leading slash is removed — names never legitimately
    start with two.
    """
    name = name.strip()
    return name[1:] if name.startswith("/") else name

# Subdirectory under agent roots whose files are helper definitions, not
# standalone subagents (e.g. `agents/reviewers/*.md`). This constant is the
# structural default that hides the whole subtree by path. A manifest adds a
# complementary, name-based layer: `excluded_subagents` entries are unioned into
# the exclusion set in `scan()` (slash-insensitive), so teams can hide further
# agents/skills by name without editing this constant.
_REVIEWERS_SUBDIR = "reviewers"


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
        """Construct from a saved catalog.json entry, tolerating schema drift.

        A newer binary can write fields an older installed CatalogEntry
        doesn't know about (e.g. `enabled`, `invoke_name` were added after
        the first release). Filter to this dataclass's own field names so a
        forward-compatible cache never bricks an older binary with a
        TypeError — but still raise if a field with no default (genuine
        corruption) is missing.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

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
    # Computed once and handed to both calls below — override_key() and
    # invoke_name() each independently derive Path(path).parent.name, and
    # this is the one call site that needs both for the same path.
    dir_name = overrides.dir_name_for(path)
    key = overrides.override_key(
        kind="skill", namespace=namespace, path=path, name=name, dir_name=dir_name
    )
    return CatalogEntry(
        kind="skill",
        name=name,
        namespace=namespace,
        description=str(description).strip(),
        path=path,
        enabled=overrides.is_enabled(key, table),
        invoke_name=overrides.invoke_name(
            kind="skill", namespace=namespace, path=path, name=name, dir_name=dir_name
        ),
    )


def _entry_from_md_file(
    path: Path,
    kind: str,
    namespace: str,
    *,
    name_from_stem: bool = False,
) -> CatalogEntry | None:
    """Build a catalog entry from a single Markdown definition file.

    `description` is mandatory (missing → entry rejected). The name is taken
    from frontmatter `name` with a stem fallback for agents; when
    `name_from_stem` is set (commands) the name is the slash-prefixed file stem
    `/{stem}` for consistency with the built-in command names (`/loop` etc.).
    """
    fm = parse_frontmatter(path)
    if not fm:
        return None
    description = fm.get("description")
    if not description:
        return None
    if name_from_stem:
        name = f"/{path.stem}"
    else:
        raw_name = fm.get("name")
        name = str(raw_name).strip() if raw_name else path.stem
    name = str(name).strip()
    if not name or name == "/":
        return None
    return CatalogEntry(
        kind=kind,
        name=name,
        namespace=namespace,
        description=str(description).strip(),
        path=str(path),
    )


def _symlink_escapes(path: Path, root: Path) -> bool:
    """True if `path` is a symlink resolving outside `root` (SEC-MIT-005).

    Non-symlinks are always safe. A symlink is only accepted when its target
    stays within the scanned root; anything pointing elsewhere (or unresolvable)
    is treated as an escape and skipped by the caller.
    """
    if not path.is_symlink():
        return False
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return True
    return not (resolved == root_resolved or root_resolved in resolved.parents)


def _scan_md_agents(root: Path) -> Iterable[CatalogEntry]:
    """Scan a `agents/` root for subagent definitions.

    Uses `rglob('*.md')` so namespaced agents (`agents/sub/x.md`) are found,
    but skips the `reviewers/` helper subtree and symlinks that escape the root.
    """
    if not root.is_dir():
        return
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        if _REVIEWERS_SUBDIR in rel.parts:
            continue
        if _symlink_escapes(md, root):
            continue
        entry = _entry_from_md_file(md, kind="subagent", namespace="user")
        if entry:
            yield entry


def _scan_md_commands(root: Path) -> Iterable[CatalogEntry]:
    """Scan a `commands/` root for slash-command definitions.

    Uses `rglob('*.md')` so namespaced commands (`commands/sub/x.md`) are found
    and skips symlinks that escape the root. Unlike agents there is no helper
    subtree to exclude. The command name is the slash-prefixed file stem
    (`/{stem}`) for consistency with the built-in command names (`/loop` etc.).
    """
    if not root.is_dir():
        return
    for md in sorted(root.rglob("*.md")):
        if _symlink_escapes(md, root):
            continue
        entry = _entry_from_md_file(
            md, kind="command", namespace="user", name_from_stem=True
        )
        if entry:
            yield entry


def _scan_user_skills(root: Path, table: dict[str, str]) -> Iterable[CatalogEntry]:
    if not root.is_dir():
        return
    for skill_md in sorted(root.glob("*/SKILL.md")):
        entry = _entries_from_skill_file(skill_md, namespace="user", table=table)
        if entry:
            yield entry


def _scan_plugin_skills(
    root: Path,
    table: dict[str, str],
    plugin_whitelist: tuple[str, ...] | None = None,
) -> Iterable[CatalogEntry]:
    """Scan a plugin marketplaces root for SKILL.md files.

    `plugin_whitelist` is Tri-State (see manifest.ManifestFilter): ``None`` keeps
    the default "scan every plugin" behaviour, while a tuple (possibly empty) gates
    the scan to only those plugin names — an empty tuple therefore yields nothing.
    """
    if not root.is_dir():
        return
    # Pattern: <marketplace>/plugins/<plugin>/skills/<skill>/SKILL.md
    for skill_md in sorted(root.glob("*/plugins/*/skills/*/SKILL.md")):
        try:
            plugin_name = skill_md.parents[2].name
        except IndexError:
            plugin_name = "unknown"
        if plugin_whitelist is not None and plugin_name not in plugin_whitelist:
            continue
        entry = _entries_from_skill_file(
            skill_md, namespace=f"plugin:{plugin_name}", table=table
        )
        if entry:
            yield entry


def _version_sort_key(version: str) -> tuple:
    """Comparable key for a dotted version string, e.g. "10.2.0" > "9.0.0".

    `sorted()`/`max()` on the raw path strings compares lexicographically,
    which ranks "10.0.0" below "9.0.0" (and, more subtly, before "6.2.0")
    because it compares character-by-character rather than component-by-
    component. Each dot-separated segment is wrapped as `(0, int)` when
    numeric or `(1, str)` otherwise, so every key is uniformly comparable
    (numeric segments always sort before non-numeric ones at the same
    position) without raising on an unexpected pre-release-style segment
    like "6.2.0-beta".
    """
    key: list[tuple[int, int | str]] = []
    for part in version.split("."):
        key.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(key)


def _scan_plugin_cache_skills(
    root: Path,
    table: dict[str, str],
    plugin_whitelist: tuple[str, ...] | None = None,
) -> Iterable[CatalogEntry]:
    """Installed-plugin layout: <marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md.

    Distinct from `plugins/marketplaces/`, which is the *catalogue* of available
    plugins. The cache is what is actually installed and invocable, and it
    interposes a version segment — so the marketplaces glob misses it entirely.

    `plugin_whitelist` is the same Tri-State gate `_scan_plugin_skills` applies
    (see `manifest.ManifestFilter`): ``None`` scans every plugin, a tuple gates
    to those plugin names, and an empty tuple therefore yields nothing. Applied
    BEFORE the version tournament so a gated-out plugin never competes.

    When multiple versions of the same plugin skill are installed side by
    side, the highest version wins. This is decided HERE, by tracking the
    best `(plugin_name, skill_dir_name)` seen so far, rather than relying on
    `catalog.scan()`'s `_accept()` dedup (which keeps whichever entry it saw
    *first* — previously whatever `sorted()` on the raw glob happened to
    yield first, i.e. the LOWEST version string).
    """
    if not root.is_dir():
        return
    best: dict[tuple[str, str], tuple[tuple, Path]] = {}
    for skill_md in root.glob("*/*/*/skills/*/SKILL.md"):
        try:
            plugin_name = skill_md.parents[3].name
            version = skill_md.parents[2].name
        except IndexError:
            plugin_name = "unknown"
            version = ""
        if plugin_whitelist is not None and plugin_name not in plugin_whitelist:
            continue
        key = (plugin_name, skill_md.parent.name)
        vkey = _version_sort_key(version)
        current = best.get(key)
        if current is None or vkey > current[0]:
            best[key] = (vkey, skill_md)

    # Sorted by path for a deterministic scan order (matches every other
    # scanner here) — the version comparison above already picked the
    # winner per (plugin, skill dir); this only orders the winners.
    for _vkey, skill_md in sorted(best.values(), key=lambda item: str(item[1])):
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

    # The manifest (when configured) is the structural SSoT for exclusions and
    # plugin gating; it loads fail-soft to a neutral filter, so an unset/broken
    # manifest leaves scan behaviour exactly as it was before MAN-003.
    manifest_filter = manifest_loader.load_manifest(
        cfg.catalog.catalog_manifest, paths.claude_home()
    )

    # Union of config excludes and manifest excludes, normalized for
    # slash-insensitive matching so "loop" and "/loop" exclude each other.
    exclude = {
        _norm_name(name)
        for name in (*cfg.catalog.exclude_names, *manifest_filter.excluded_names)
    }
    plugin_whitelist = manifest_filter.plugin_whitelist

    seen: set[tuple[str, str]] = set()
    out: list[CatalogEntry] = []

    def _accept(entry: CatalogEntry) -> None:
        # Exclusion and dedup both key on the INVOCABLE name (what the Skill
        # tool accepts), not the frontmatter name — they differ whenever a
        # skill's directory disagrees with its `name:`. Normalised on top so a
        # bare `loop` and a slash-prefixed `/loop` still exclude each other.
        invocable = entry.invoke_name or entry.name
        if _norm_name(invocable) in exclude:
            return
        key = (entry.kind, invocable)
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
            for entry in _scan_plugin_skills(root, table, plugin_whitelist):
                _accept(entry)
        elif root.name == "cache":
            # The whitelist gates this root too. The installed-plugin cache is a
            # SECOND channel for the same plugin skills the marketplaces root
            # already gates; leaving it ungated would let every installed plugin
            # in through the back door and silently void the manifest's
            # `plugin_whitelist` the moment upstream added this scanner.
            for entry in _scan_plugin_cache_skills(root, table, plugin_whitelist):
                _accept(entry)

    for root in paths.agent_roots():
        for entry in _scan_md_agents(root):
            _accept(entry)

    for root in paths.command_roots():
        for entry in _scan_md_commands(root):
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
