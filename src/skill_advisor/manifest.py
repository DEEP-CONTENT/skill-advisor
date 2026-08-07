"""Optional curated catalog manifest loader (fail-soft).

A manifest is the *structural source of truth* for catalog exclusions. It lets a
team or org publish one JSON file that names which sub-skills/sub-agents to hide
and which plugin skills to allow — instead of every user editing their local
`config.toml`. The manifest is strictly optional: when `catalog.catalog_manifest`
is unset, or the file is missing / unreadable / malformed / oversized, the loader
returns an *empty* filter and the catalog behaves exactly as before. A broken
manifest must never take the advisor down (fail-soft, WARN-logged).

Design choices:
- stdlib only (`json`, `logging`, `pathlib`) — no new config/logging subsystem (P5).
- `plugin_whitelist` is Tri-State and that distinction is carried here:
  field absent → ``None`` (caller keeps today's "scan all plugins" behaviour);
  present (even empty) → a concrete tuple of allowed plugin names. MAN-003 in
  `catalog.py` consumes this to gate `_scan_plugin_skills`.
- Each `plugin_whitelist[].path` is confined to `claude_home` via
  `resolve()` + `is_relative_to()` (SEC-MIT-003): an entry pointing outside the
  Claude config tree (e.g. via ``../``) is dropped, never trusted.
- Files larger than 1 MiB are rejected outright (SEC-MIT-004) — a curated
  manifest is tiny; an oversized one is treated as hostile/garbage.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Curated manifests are tiny (a handful of names). Anything larger is rejected
# before it is read into memory (DoS / garbage guard, SEC-MIT-004).
MAX_MANIFEST_BYTES = 1024 * 1024  # 1 MiB


@dataclass(frozen=True)
class ManifestFilter:
    """Immutable result of loading a catalog manifest.

    `plugin_whitelist` is the Tri-State carrier: ``None`` means the manifest did
    not declare the field (caller keeps default plugin-scan behaviour), while a
    tuple (possibly empty) means the field was present and should be honoured.
    """

    schema_version: int = 0
    excluded_subskills: frozenset[str] = field(default_factory=frozenset)
    excluded_subagents: frozenset[str] = field(default_factory=frozenset)
    plugin_whitelist: tuple[str, ...] | None = None

    @property
    def excluded_names(self) -> frozenset[str]:
        """Union of every excluded name, regardless of kind."""
        return self.excluded_subskills | self.excluded_subagents

    @classmethod
    def empty(cls) -> "ManifestFilter":
        """The neutral filter: no exclusions, no plugin gating."""
        return cls()


def _str_set(raw: object) -> frozenset[str]:
    """Coerce a manifest list field into a clean set of non-empty strings.

    Non-list inputs, non-string members and blanks are silently dropped — the
    loader is lenient because a slightly-wrong field should degrade, not crash.
    """
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item.strip() for item in raw if isinstance(item, str) and item.strip())


def _confined_plugin_names(raw: object, claude_home: Path) -> tuple[str, ...] | None:
    """Resolve `plugin_whitelist` into allowed plugin names, confined to home.

    Tri-State: a missing field arrives here only when `raw` is the sentinel-free
    absence handled by the caller, so this function is invoked solely when the key
    exists. A non-list value is treated as an empty (present) whitelist.

    Each entry may be a bare string (a plugin name, kept as-is) or a mapping with
    a required ``name`` and an optional ``path``. When a ``path`` is given it must
    resolve to a location *inside* `claude_home`; otherwise the entry is dropped
    (SEC-MIT-003). Order is preserved and duplicates collapsed.
    """
    if not isinstance(raw, list):
        return ()

    try:
        home = claude_home.resolve()
    except OSError:
        home = claude_home

    names: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        name: str | None = None
        path_value: object = None
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            candidate = entry.get("name")
            if isinstance(candidate, str):
                name = candidate.strip()
            path_value = entry.get("path")

        if not name:
            continue

        if isinstance(path_value, str) and path_value.strip():
            candidate_path = Path(path_value)
            if not candidate_path.is_absolute():
                candidate_path = home / candidate_path
            try:
                resolved = candidate_path.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(home):
                log.warning(
                    "manifest: dropping plugin_whitelist entry %r — path %r escapes %s",
                    name, path_value, home,
                )
                continue

        if name not in seen:
            seen.add(name)
            names.append(name)

    return tuple(names)


def load_manifest(path: str | Path, claude_home: Path) -> ManifestFilter:
    """Load a catalog manifest, returning an empty filter on any failure.

    Args:
        path: Filesystem path to the manifest JSON (may be ``""`` / nonexistent).
        claude_home: The active Claude config dir, used to confine
            `plugin_whitelist[].path` entries.

    Returns:
        A populated `ManifestFilter` on success, or `ManifestFilter.empty()` for
        any error (missing file, unreadable, >1 MiB, invalid JSON, wrong top-level
        type). Never raises.
    """
    if not path:
        return ManifestFilter.empty()

    manifest_path = Path(path)
    try:
        size = manifest_path.stat().st_size
    except OSError as exc:
        log.warning("manifest: cannot stat %s (%s) — ignoring manifest", manifest_path, exc)
        return ManifestFilter.empty()

    if size > MAX_MANIFEST_BYTES:
        log.warning(
            "manifest: %s is %d bytes (> %d limit) — ignoring manifest",
            manifest_path, size, MAX_MANIFEST_BYTES,
        )
        return ManifestFilter.empty()

    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("manifest: cannot read %s (%s) — ignoring manifest", manifest_path, exc)
        return ManifestFilter.empty()

    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("manifest: %s is not valid JSON (%s) — ignoring manifest", manifest_path, exc)
        return ManifestFilter.empty()

    if not isinstance(data, dict):
        log.warning(
            "manifest: %s top-level is %s, expected object — ignoring manifest",
            manifest_path, type(data).__name__,
        )
        return ManifestFilter.empty()

    schema_version = data.get("schema_version", 0)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        schema_version = 0

    plugin_whitelist: tuple[str, ...] | None = None
    if "plugin_whitelist" in data:
        plugin_whitelist = _confined_plugin_names(data.get("plugin_whitelist"), claude_home)

    return ManifestFilter(
        schema_version=schema_version,
        excluded_subskills=_str_set(data.get("excluded_subskills")),
        excluded_subagents=_str_set(data.get("excluded_subagents")),
        plugin_whitelist=plugin_whitelist,
    )
