# Catalog refresh & skill rotation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop recommending skills Claude Code cannot invoke (58.6% of all picks today), then let the advisor maintain its own active set by rotating skills in and out of `skillOverrides`.

**Architecture:** The catalog stops being one flat list and carries state per entry: `enabled` (resolved from `skillOverrides`) and `invoke_name` (the string Claude Code's Skill tool actually accepts, which is *not* the frontmatter name). The matcher and the lifecycle phase-preference list both filter to the pickable set; the rotation operates over the whole pool. Rotation scores the pool on `pick_rate` plus `semantic_fit` against an 8-centroid online sketch of prompt embeddings, and writes `skillOverrides` into the advisor's own `--settings` file — never the user's `settings.json`.

**Tech Stack:** Python 3.12, `uv`, pytest, numpy, fastembed (BGE-small, 384-dim). No new dependencies.

## Global Constraints

- **Never write `~/.claude/settings.json`.** Every write goes to `paths.settings_file()`, honouring `SKILL_ADVISOR_SETTINGS_FILE`. This is a standing promise of the project.
- Hook paths are **silent on error** and must never raise. `rotate` and `migrate-excludes` are CLI verbs and **should fail loudly** with a clear message.
- Settings writes reuse the pattern established at `baseline.py:212-255`: read-modify-write, `json.dumps(data, indent=2) + "\n"`, round-trip `json.loads()` validation *before* touching the real path, temp file named `.tmp.<pid>`, atomic `Path.replace()`, unlink the temp on failure. The documented race with `install.render_settings()` applies unchanged.
- Test suite baseline is **411 passing in 0.66 s**. Keep it fast — no test may embed real text with fastembed or invoke `claude`.
- Active-set band from the spec: **50–100 skills**, default target **75**, hard floor enforced before any write.
- Privacy: no prompt text and no per-prompt vectors are ever stored. `centroids.npz` is fixed-size, written only when `telemetry.events_enabled` is already true, and removed by `skill-advisor uninstall`.

---

## Audit corrections to the spec

Verified against live code, live `~/.claude/settings.json`, live `config.toml` and the 8,536-event log on 2026-07-30. These are settled — do not re-litigate them mid-implementation.

**Confirmed exactly as written:** `catalog.py` never reads `skillOverrides`. 889 `off`, 428 `exclude_names`, **483 off-but-not-excluded**, **62 enabled**.

| Spec says | Reality | Task |
|---|---|---|
| "The matcher filters to `enabled == True`" — implying `matcher.py` is the fix site | Three of the four named worst offenders are **not matcher picks at all**. They come from the hardcoded `lifecycle.PHASE_CANDIDATES` list via `pick_candidates_for_phase()` (`lifecycle.py:396`), which takes the first 3 entries *present in the catalog* with no regard for enabled state. Fixing only the matcher leaves the single largest offender untouched. | 4 |
| Rotation pool = "every skill on disk", 947 | **216 of 947 `SKILL.md` files (23%) have no parseable YAML frontmatter** and are invisible to `catalog.scan()`. The real pool is **727** distinct parseable names. Per the 2026-07-30 decision: report this, do not widen the parser. | 6 |
| `enabled` is "resolved from `skillOverrides`" — join key unstated | `skillOverrides` is keyed by **directory name**; `CatalogEntry.name` is the **frontmatter name**. They differ for 7 skills. This is already a live bug independent of rotation: `xlsx` is enabled but the catalog calls it `xlsx-official`, a name the Skill tool rejects. Plugin skills are worse — invocable as `dc-sprints:dc-board-overview`, catalogued as `dc-board-overview`. | 1 |
| `invocation_rate` is a scoring signal | **No data source exists.** `record_stop` logs tool *names* only: `"Skill"` appears 1,482 times across 3,971 stop events, never which skill; `subagents` is empty in every event. Per the 2026-07-30 decision: start capturing now, score rotation v1 on `pick_rate` + `semantic_fit` only. | 7, 11 |
| (unstated) | `compute_hash()` fingerprints file mtimes and builtin descriptions. Toggling `skillOverrides` changes no mtime, so `build` would no-op and `rotate --apply` would appear to do nothing. | 2 |
| (unstated) | The scanner's plugin glob is `*/plugins/*/skills/*/SKILL.md` under `plugins/marketplaces/`. **55 further SKILL.md files live under `plugins/cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md`** — a versioned layout the glob never matches. The entire `superpowers` plugin is invisible to the advisor. | 5 |
| "Excluded but enabled → 11" | 22 today. | 6 |
| (unstated) | The cached catalog on this machine is from **Jun 16** — 6 weeks stale, 338 entries against 346 on a fresh scan. Any measurement taken before `skill-advisor build` is measuring a June catalog. | 6 |

### The 490 `plan-writing` picks, mechanically

`pick_candidates_for_phase` takes the first 3 preference entries that exist in the catalog. Traced on live data:

```
PLANNING     Plan (subagent, enabled) · writing-plans (enabled) · plan-writing (OFF)    ← pick #3, 490 times
COMPLETE     commit (enabled) · create-pr (OFF) · pr-creator (OFF)                      ← picks #2 and #3
CORRECTION   fix-review (enabled) · address-github-comments (enabled) · iterate-pr (OFF) ← pick #3, 116 times
```

Every `superpowers:*` preference entry resolves to *not in catalog* because of the plugin-cache gap (Task 5), so the list falls through past the good candidates to the disabled ones. Filtering to enabled (Task 4) makes the ordered list fall through correctly instead — `finishing-a-development-branch` and `systematic-debugging` are both present and enabled and are simply never reached today.

### Cross-plan dependency

This plan shrinks the embedding index from 338 entries to roughly 93 (56 parseable-and-enabled user skills + ~22 plugin skills + 15 builtins, before Task 5 recovers more). Every cosine ranking changes. **`docs/superpowers/plans/2026-07-30-latency-judge-escalation.md` must be calibrated after this plan lands, not before.** The fast-fail plan (`2026-07-30-latency-fast-fail.md`) is unaffected and ships independently.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/skill_advisor/overrides.py` | **New.** Sole owner of `skillOverrides`: the directory-name join key, reading the merged view, and the atomic write. | Create |
| `src/skill_advisor/centroids.py` | **New.** The fixed-size online prompt sketch. Knows nothing about skills. | Create |
| `src/skill_advisor/rotate.py` | **New.** Scoring and swap proposals. Pure functions over data; no I/O beyond loading what it is given. | Create |
| `src/skill_advisor/catalog.py` | Scanning. Gains `enabled` + `invoke_name` resolution and the plugin-cache root. | Modify |
| `src/skill_advisor/paths.py` | Path SSoT. | Modify: add `centroids_file()`, `plugin_cache_roots()`. |
| `src/skill_advisor/index.py` | Embedding index. Gains a pickable mask so `top_k` returns K *pickable* entries. | Modify |
| `src/skill_advisor/matcher.py` | Filters to pickable. | Modify |
| `src/skill_advisor/lifecycle.py` | Phase preferences filter to pickable. | Modify |
| `src/skill_advisor/hook.py` | Capture the invoked skill name; feed the sketch. | Modify |
| `src/skill_advisor/cli.py` | `rotate`, `migrate-excludes`, richer `doctor`. | Modify |
| `src/skill_advisor/config.py` | `RotationConfig`. | Modify |

Rotation lives in three small modules rather than one because their test needs differ sharply: `overrides.py` needs filesystem fixtures, `centroids.py` needs numeric fixtures, `rotate.py` needs neither and should be testable with plain dataclasses.

---

# Phase A — stop recommending un-invocable skills

Independent of the verification gate. Worth ~2,334 bad recommendations immediately. The spec calls the gate "implementation task 1"; it is Task 8 here precisely because Phase A does not depend on it. Run the gate whenever convenient — but **do not start Phase C until it passes.**

---

### Task 1: Resolve `enabled` and `invoke_name` on catalog entries

**Files:**
- Create: `src/skill_advisor/overrides.py`
- Modify: `src/skill_advisor/catalog.py:20-37` (`CatalogEntry`), `56-70` (`_entries_from_skill_file`), `106-156` (`_builtin_entries`, `scan`)
- Test: `tests/test_overrides.py` (create), `tests/test_catalog.py`

**Interfaces:**
- Produces:
  - `overrides.override_key(kind: str, namespace: str, path: str, name: str) -> str | None` — the `skillOverrides` key for an entry, or `None` for kinds `skillOverrides` does not govern (subagents, commands). For a user skill it is the **directory name** (`Path(path).parent.name`); for a plugin skill, the same directory name (plugin skills carry no `skillOverrides` entry today — verified: zero of 889 keys contain `:`).
  - `overrides.invoke_name(kind: str, namespace: str, path: str, name: str) -> str` — the string the Skill tool accepts. User/extra skill → directory name. Plugin skill → `f"{plugin}:{dirname}"`. Subagent/command → `name` unchanged.
  - `overrides.read(settings_path: Path | None = None) -> dict[str, str]` — the merged `skillOverrides` map. Reads the advisor's `paths.settings_file()` first, then `~/.claude/settings.json`, with the advisor's file winning per key. Returns `{}` on a missing or malformed file, never raises.
  - `overrides.is_enabled(key: str | None, table: dict[str, str]) -> bool` — `True` when `key is None` or the value is anything other than `"off"`. **Missing key means enabled** — degrading to today's behaviour, never to an empty catalog.
  - `catalog.CatalogEntry` gains `enabled: bool = True` and `invoke_name: str = ""` (empty means "same as `name`"; `embed_text()` is unchanged so existing embeddings stay comparable).
  - `catalog.scan(config=None, *, overrides_table: dict[str, str] | None = None)` — the table is injectable so tests need no real settings file.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_overrides.py`:

```python
import json

from skill_advisor import overrides, paths


def test_override_key_is_the_directory_name_not_the_frontmatter_name():
    """settings.json keys by directory; the catalog keys by frontmatter `name`.
    They differ for 7 skills on the author's machine — `xlsx` vs `xlsx-official`."""
    key = overrides.override_key(
        kind="skill", namespace="user",
        path="/home/u/.claude/skills/xlsx/SKILL.md", name="xlsx-official",
    )
    assert key == "xlsx"


def test_override_key_is_none_for_builtins():
    assert overrides.override_key(kind="subagent", namespace="builtin", path="", name="Plan") is None
    assert overrides.override_key(kind="command", namespace="builtin", path="", name="review") is None


def test_invoke_name_namespaces_plugin_skills():
    """Claude Code invokes plugin skills as `<plugin>:<skill>`; the catalog
    currently emits the bare frontmatter name, which the Skill tool rejects."""
    got = overrides.invoke_name(
        kind="skill", namespace="plugin:dc-sprints",
        path="/x/marketplaces/m/plugins/dc-sprints/skills/dc-board-overview/SKILL.md",
        name="dc-board-overview",
    )
    assert got == "dc-sprints:dc-board-overview"


def test_invoke_name_is_the_directory_name_for_user_skills():
    got = overrides.invoke_name(
        kind="skill", namespace="user",
        path="/home/u/.claude/skills/xlsx/SKILL.md", name="xlsx-official",
    )
    assert got == "xlsx"


def test_missing_key_means_enabled():
    assert overrides.is_enabled("never-seen", {}) is True
    assert overrides.is_enabled(None, {"anything": "off"}) is True


def test_off_means_disabled():
    assert overrides.is_enabled("muted", {"muted": "off"}) is False


def test_read_merges_advisor_settings_over_claude_settings(isolated_paths):
    (isolated_paths["claude_home"] / "settings.json").write_text(
        json.dumps({"skillOverrides": {"a": "off", "b": "off"}}), encoding="utf-8"
    )
    paths.settings_file().write_text(
        json.dumps({"skillOverrides": {"b": "on"}}), encoding="utf-8"
    )
    table = overrides.read()
    assert table["a"] == "off"
    assert table["b"] == "on"


def test_read_survives_malformed_settings(isolated_paths):
    paths.settings_file().write_text("{ not json", encoding="utf-8")
    assert overrides.read() == {}
```

Append to `tests/test_catalog.py`:

```python
def test_scan_marks_disabled_skills(fake_claude_home):
    from skill_advisor import catalog
    from skill_advisor.config import Config

    entries = catalog.scan(Config(), overrides_table={"demo-skill": "off"})
    demo = next(e for e in entries if e.name == "demo-skill")
    assert demo.enabled is False
    # Still present — the rotation pool needs it.
    assert demo in entries


def test_scan_defaults_to_enabled_with_no_overrides(fake_claude_home):
    from skill_advisor import catalog
    from skill_advisor.config import Config

    entries = catalog.scan(Config(), overrides_table={})
    assert all(e.enabled for e in entries)


def test_scan_sets_invoke_name_for_plugin_skills(fake_claude_home):
    from skill_advisor import catalog
    from skill_advisor.config import Config

    entries = catalog.scan(Config(), overrides_table={})
    plug = next(e for e in entries if e.namespace == "plugin:x")
    assert plug.invoke_name == "x:y"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_overrides.py tests/test_catalog.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'skill_advisor.overrides'`.

- [ ] **Step 3: Create `overrides.py`**

```python
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
```

- [ ] **Step 4: Extend `CatalogEntry` and `scan()`**

In `src/skill_advisor/catalog.py`, add to the dataclass after `path`:

```python
    enabled: bool = True      # resolved from skillOverrides at scan time
    invoke_name: str = ""     # what the Skill tool accepts; "" ⟹ same as `name`
```

Leave `embed_text()` alone — it must keep returning `f"{self.name}: {self.description}"` so existing embeddings stay comparable.

Change `_entries_from_skill_file` to stamp both fields:

```python
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
    key = overrides.override_key(kind="skill", namespace=namespace, path=path, name=name)
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
```

Thread `table` through `_scan_user_skills`, `_scan_plugin_skills` and `_scan_extra_root` (each gains a `table: dict[str, str]` parameter and forwards it). Builtins keep `enabled=True` and `invoke_name=name`. Add `from . import overrides` to the imports.

Then `scan()`:

```python
def scan(
    config: Config | None = None,
    *,
    overrides_table: dict[str, str] | None = None,
) -> list[CatalogEntry]:
    cfg = config or load_config()
    table = overrides_table if overrides_table is not None else overrides.read()
    exclude = set(cfg.catalog.exclude_names)
    ...
```

and pass `table` to each `_scan_*` call.

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_overrides.py tests/test_catalog.py -q && uv run pytest -q
```

Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/skill_advisor/overrides.py src/skill_advisor/catalog.py tests/test_overrides.py tests/test_catalog.py && uv run ruff format --check src/skill_advisor/overrides.py src/skill_advisor/catalog.py tests/test_overrides.py tests/test_catalog.py
git add src/skill_advisor/overrides.py src/skill_advisor/catalog.py tests/test_overrides.py tests/test_catalog.py
git commit -m "feat(catalog): resolve enabled + invoke_name from skillOverrides"
```

---

### Task 2: Make `compute_hash()` notice an enabled-set change

Without this, `skill-advisor build` no-ops after a rotation — toggling `skillOverrides` changes no file mtime — and `rotate --apply` appears to do nothing.

**Files:**
- Modify: `src/skill_advisor/catalog.py:159-179` (`compute_hash`)
- Test: `tests/test_catalog.py`

**Interfaces:**
- Consumes: `CatalogEntry.enabled` from Task 1.
- Produces: no signature change. `compute_hash(entries)` now varies with `enabled`.

- [ ] **Step 1: Write the failing test**

```python
def test_hash_changes_when_a_skill_is_disabled(fake_claude_home):
    """Toggling skillOverrides changes no file mtime. If the hash misses it,
    `build` no-ops and `rotate --apply` silently does nothing."""
    from skill_advisor import catalog
    from skill_advisor.config import Config

    on = catalog.scan(Config(), overrides_table={})
    off = catalog.scan(Config(), overrides_table={"demo-skill": "off"})
    assert catalog.compute_hash(on) != catalog.compute_hash(off)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_catalog.py -q -k hash_changes
```

Expected: FAIL — the two hashes are equal.

- [ ] **Step 3: Fold `enabled` into the digest**

In `compute_hash`, inside the loop, immediately after `h.update(e.name.encode())` and its separator:

```python
        h.update(b"1" if e.enabled else b"0")
        h.update(b"\x00")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS. Note that this invalidates every existing `catalog.hash`, so the next `build` does real work once — which is correct, since the cached catalog on this machine is 6 weeks stale anyway.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/skill_advisor/catalog.py tests/test_catalog.py && uv run ruff format --check src/skill_advisor/catalog.py tests/test_catalog.py
git add src/skill_advisor/catalog.py tests/test_catalog.py
git commit -m "fix(catalog): fingerprint the enabled set so build notices a rotation"
```

---

### Task 3: Filter the matcher to the pickable set

**Files:**
- Modify: `src/skill_advisor/index.py:20-23` (`Index`), `54-68` (`load`), `78-88` (`top_k`)
- Test: `tests/test_index.py`, `tests/test_matcher.py`

**Interfaces:**
- Consumes: `CatalogEntry.enabled`.
- Produces:
  - `index.Index(catalog, embeddings, pickable: np.ndarray | None = None)` — `pickable` is a bool array of shape `(N,)`, derived in `load()` from `entry.enabled`. `None` means "everything pickable" so existing test constructions keep working.
  - `index.top_k(prompt, index, k, *, pickable_only: bool = True) -> list[tuple[CatalogEntry, float]]` — masks disabled rows **before** selecting, so K pickable results come back rather than K-minus-however-many-were-disabled.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_index.py`:

```python
def test_top_k_skips_disabled_entries(isolated_paths):
    """The 58.6% bug, at the index layer. Must fail against today's code."""
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="best", namespace="user", description="d",
                     path="/s/best/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="ok", namespace="user", description="d",
                     path="/s/ok/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, True]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        got = index_mod.top_k("q", idx, 2)

    assert [e.name for e, _ in got] == ["ok"]


def test_top_k_returns_k_pickable_not_k_minus_disabled(isolated_paths):
    """Masking must happen before selection, or a disabled top-scorer eats a slot."""
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="off1", namespace="user", description="d",
                     path="/s/off1/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="on1", namespace="user", description="d",
                     path="/s/on1/SKILL.md", enabled=True),
        CatalogEntry(kind="skill", name="on2", namespace="user", description="d",
                     path="/s/on2/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.9, 0.436], [0.8, 0.6]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, True, True]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        got = index_mod.top_k("q", idx, 2)

    assert [e.name for e, _ in got] == ["on1", "on2"]


def test_top_k_returns_empty_when_everything_is_disabled(isolated_paths):
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="x", namespace="user", description="d",
                     path="/s/x/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="y", namespace="user", description="d",
                     path="/s/y/SKILL.md", enabled=False),
    ]
    emb = np.array([[1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, False]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        assert index_mod.top_k("q", idx, 2) == []


def test_top_k_clamps_when_k_exceeds_the_pickable_count(isolated_paths):
    """max_candidates is 15 but the pickable set may be smaller. The masked
    scores are -inf, and those must never reach the caller as picks."""
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="off", namespace="user", description="d",
                     path="/s/off/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="on", namespace="user", description="d",
                     path="/s/on/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, True]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        got = index_mod.top_k("q", idx, 15)

    assert [e.name for e, _ in got] == ["on"]
    assert all(np.isfinite(s) for _, s in got)


def test_load_derives_the_pickable_mask(isolated_paths):
    import numpy as np

    from skill_advisor import catalog as catalog_mod
    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="a", namespace="user", description="d",
                     path="/s/a/SKILL.md", enabled=True),
        CatalogEntry(kind="skill", name="b", namespace="user", description="d",
                     path="/s/b/SKILL.md", enabled=False),
    ]
    emb = np.eye(2, dtype=np.float32)
    index_mod.save(entries, emb, "h")
    loaded = index_mod.load()
    assert list(loaded.pickable) == [True, False]
```

Add `from unittest.mock import patch` to the top of `tests/test_index.py` if absent.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_index.py -q -k "pickable or disabled"
```

Expected: FAIL — `TypeError: Index.__init__() got an unexpected keyword argument 'pickable'`.

- [ ] **Step 3: Add the mask**

In `src/skill_advisor/index.py`:

```python
@dataclass
class Index:
    catalog: list[CatalogEntry]
    embeddings: np.ndarray  # shape (N, D), float32, L2-normalised
    # Bool mask of shape (N,) — True where the entry is invocable in Claude Code.
    # The index deliberately holds disabled entries too: the rotation pool needs
    # their embeddings to score a skill that has never been used.
    pickable: np.ndarray | None = None
```

In `load()`, just before the `return`:

```python
    mask = np.array([e.enabled for e in catalog], dtype=bool)
    return Index(catalog=catalog, embeddings=embeddings, pickable=mask)
```

Replace `top_k`:

```python
def top_k(
    prompt: str, index: Index, k: int, *, pickable_only: bool = True
) -> list[tuple[CatalogEntry, float]]:
    if index.embeddings.shape[0] == 0 or k <= 0:
        return []
    model = _embed_model()
    q = np.array(list(model.embed([prompt])), dtype=np.float32)
    q = _normalise(q)[0]
    scores = index.embeddings @ q  # cosine because both sides are unit vectors

    if pickable_only and index.pickable is not None:
        # Mask BEFORE selection. Masking after would let a disabled top-scorer
        # consume one of the K slots and silently shorten the shortlist.
        if not index.pickable.any():
            return []
        scores = np.where(index.pickable, scores, -np.inf)

    k = min(k, int(np.isfinite(scores).sum()) if pickable_only else scores.shape[0])
    if k <= 0:
        return []
    top_idx = np.argpartition(-scores, k - 1)[:k]
    top_idx = top_idx[np.argsort(-scores[top_idx])]
    return [(index.catalog[i], float(scores[i])) for i in top_idx]
```

- [ ] **Step 4: Add the matcher-level regression test**

This is the spec's headline test — "a test that fails against today's code". Append to `tests/test_matcher.py`:

```python
def test_matcher_never_picks_a_disabled_skill(isolated_paths):
    """58.6% of live picks named a skill Claude Code cannot invoke. Zero now."""
    import numpy as np

    from skill_advisor import catalog as catalog_mod
    from skill_advisor.catalog import CatalogEntry
    from skill_advisor.config import Config

    entries = [
        CatalogEntry(kind="skill", name="disabled-but-relevant", namespace="user",
                     description="d", path="/s/disabled-but-relevant/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="enabled-alternative", namespace="user",
                     description="d", path="/s/enabled-alternative/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.7, 0.714]], dtype=np.float32)
    catalog_mod.save(entries)
    index_mod.save(entries, emb, "h")

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        result = matcher.pick_stateless("q", Config(), top_k=2, candidates=2, threshold=0.0)

    names = [p.entry.name for p in result.picks]
    assert "disabled-but-relevant" not in names
    assert names == ["enabled-alternative"]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/skill_advisor/index.py tests/test_index.py tests/test_matcher.py && uv run ruff format --check src/skill_advisor/index.py tests/test_index.py tests/test_matcher.py
git add src/skill_advisor/index.py tests/test_index.py tests/test_matcher.py
git commit -m "fix(index): mask disabled skills out of top_k before selection"
```

---

### Task 4: Filter the lifecycle phase preferences to pickable

The single largest offender. `plan-writing` alone is 490 picks — 21% of all bad recommendations — and Task 3 does not touch it.

**Files:**
- Modify: `src/skill_advisor/lifecycle.py:396-414` (`pick_candidates_for_phase`)
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `CatalogEntry.enabled`.
- Produces: `pick_candidates_for_phase(phase, catalog, limit=3, config=None, *, pickable_only: bool = True)` — default-on so every existing caller (`matcher._phase_picks`, `matcher._parallelization_picks`) is fixed without edits.

- [ ] **Step 1: Write the failing tests**

```python
def test_phase_candidates_skip_disabled_and_fall_through(isolated_paths):
    """Live trace 2026-07-30: PLANNING resolved to
       Plan (enabled) · writing-plans (enabled) · plan-writing (OFF)
    so every planning turn emitted a disabled skill as pick #3 — 490 times.
    The ordered list already contains enabled alternatives further down; they
    were simply never reached."""
    from skill_advisor import lifecycle
    from skill_advisor.catalog import CatalogEntry

    def _skill(name, enabled):
        return CatalogEntry(kind="skill", name=name, namespace="user",
                            description="d", path=f"/s/{name}/SKILL.md", enabled=enabled)

    catalog = [
        CatalogEntry(kind="subagent", name="Plan", namespace="builtin", description="d"),
        _skill("writing-plans", True),
        _skill("plan-writing", False),
        _skill("brainstorming", True),
    ]
    picks = lifecycle.pick_candidates_for_phase(lifecycle.PLANNING, catalog, limit=3)
    names = [e.name for e in picks]
    assert "plan-writing" not in names
    assert names == ["Plan", "writing-plans", "brainstorming"]


def test_phase_candidates_return_empty_when_all_are_disabled(isolated_paths):
    """Callers already fall back to the embedding matcher on an empty list."""
    from skill_advisor import lifecycle
    from skill_advisor.catalog import CatalogEntry

    catalog = [
        CatalogEntry(kind="skill", name="plan-writing", namespace="user", description="d",
                     path="/s/plan-writing/SKILL.md", enabled=False),
    ]
    assert lifecycle.pick_candidates_for_phase(lifecycle.PLANNING, catalog, limit=3) == []


def test_phase_candidates_can_opt_out_of_filtering(isolated_paths):
    """The rotation needs to see what a phase WOULD pick from the whole pool."""
    from skill_advisor import lifecycle
    from skill_advisor.catalog import CatalogEntry

    catalog = [
        CatalogEntry(kind="skill", name="plan-writing", namespace="user", description="d",
                     path="/s/plan-writing/SKILL.md", enabled=False),
    ]
    picks = lifecycle.pick_candidates_for_phase(
        lifecycle.PLANNING, catalog, limit=3, pickable_only=False
    )
    assert [e.name for e in picks] == ["plan-writing"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_lifecycle.py -q -k phase_candidates
```

Expected: FAIL — the first asserts `"plan-writing" not in names` and it is in names.

- [ ] **Step 3: Filter**

Replace `pick_candidates_for_phase`:

```python
def pick_candidates_for_phase(
    phase: str,
    catalog: Iterable[CatalogEntry],
    limit: int = 3,
    config: LifecycleConfig | None = None,
    *,
    pickable_only: bool = True,
) -> list[CatalogEntry]:
    """Pick preferred catalog entries for a phase, honoring config overrides.

    Disabled skills are skipped rather than consuming a slot, so the ordered
    preference list falls through to the next enabled alternative — which is
    what the list was always for. Measured 2026-07-29: without this filter the
    PLANNING list stopped at `plan-writing` (disabled) and never reached
    `brainstorming`, producing 490 un-invocable recommendations.
    """
    prefs = _resolve_phase_prefs(phase, config)
    by_name: dict[str, CatalogEntry] = {e.name: e for e in catalog}
    picks: list[CatalogEntry] = []
    seen: set[str] = set()
    for kind, name in prefs:
        entry = by_name.get(name)
        if entry is None or entry.kind != kind or entry.name in seen:
            continue
        if pickable_only and not entry.enabled:
            continue
        picks.append(entry)
        seen.add(entry.name)
        if len(picks) >= limit:
            break
    return picks
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/skill_advisor/lifecycle.py tests/test_lifecycle.py && uv run ruff format --check src/skill_advisor/lifecycle.py tests/test_lifecycle.py
git add src/skill_advisor/lifecycle.py tests/test_lifecycle.py
git commit -m "fix(lifecycle): skip disabled skills in phase preferences

plan-writing alone was 490 un-invocable picks — the PLANNING list took it as
entry #3 and never reached brainstorming."
```

---

### Task 5: Scan the versioned plugin-cache layout

55 `SKILL.md` files — including every `superpowers` skill the phase-preference list explicitly asks for — live under a path the scanner's glob never matches.

**Files:**
- Modify: `src/skill_advisor/paths.py:37-53` (`skill_roots`)
- Modify: `src/skill_advisor/catalog.py:82-93` (`_scan_plugin_skills`), `141-147` (root dispatch in `scan`)
- Test: `tests/test_paths.py`, `tests/test_catalog.py`

**Interfaces:**
- Produces: `skill_roots()` additionally yields `<claude_home>/plugins/cache` (and the `~/.claude` fallback equivalent). `catalog.scan()`'s root dispatch gains a `cache` branch calling `_scan_plugin_cache_skills(root, table)`, which globs `*/*/*/skills/*/SKILL.md` (marketplace / plugin / version / skills / skill) and derives the plugin name from `skill_md.parents[3].name`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_catalog.py`:

```python
def test_scan_finds_versioned_plugin_cache_skills(isolated_paths):
    """55 SKILL.md files live under plugins/cache/<marketplace>/<plugin>/<version>/skills/,
    a layout the marketplaces glob never matched. The whole superpowers plugin
    was invisible, which is why every `superpowers:*` phase preference missed."""
    from skill_advisor import catalog
    from skill_advisor.config import Config

    d = (isolated_paths["claude_home"] / "plugins" / "cache" / "official"
         / "superpowers" / "6.2.0" / "skills" / "writing-plans")
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: writing-plans\ndescription: \"Use when you have a spec.\"\n---\n",
        encoding="utf-8",
    )

    entries = catalog.scan(Config(), overrides_table={})
    got = next(e for e in entries if e.name == "writing-plans")
    assert got.namespace == "plugin:superpowers"
    assert got.invoke_name == "superpowers:writing-plans"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/test_catalog.py -q -k plugin_cache
```

Expected: FAIL — `StopIteration`.

- [ ] **Step 3: Add the root**

In `paths.skill_roots()`, extend both the primary and fallback root lists:

```python
    roots = [
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
            if extra not in roots:
                roots.append(extra)
    return roots
```

- [ ] **Step 4: Add the scanner**

In `catalog.py`, next to `_scan_plugin_skills`:

```python
def _scan_plugin_cache_skills(root: Path, table: dict[str, str]) -> Iterable[CatalogEntry]:
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
```

In `scan()`'s root loop, add the branch:

```python
        elif root.name == "cache":
            for entry in _scan_plugin_cache_skills(root, table):
                _accept(entry)
```

`_accept` already dedupes on `(kind, name)` with the primary root winning, so multiple installed versions of the same plugin collapse to the first — `sorted()` makes that deterministic. Note this favours the *lowest* version string; acceptable, because descriptions rarely change between patch versions and the alternative (semver parsing) is not worth it here.

- [ ] **Step 5: Run tests and re-measure**

```bash
uv run pytest -q
skill-advisor build
skill-advisor doctor
```

Expected: PASS. The catalog should grow by roughly 50 plugin entries and shrink dramatically overall once disabled skills are excluded from picks.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/skill_advisor/paths.py src/skill_advisor/catalog.py tests/test_catalog.py tests/test_paths.py && uv run ruff format --check src/skill_advisor/paths.py src/skill_advisor/catalog.py tests/test_catalog.py tests/test_paths.py
git add src/skill_advisor/paths.py src/skill_advisor/catalog.py tests/test_catalog.py tests/test_paths.py
git commit -m "fix(catalog): scan the versioned plugins/cache layout

The entire superpowers plugin was invisible, which is why every
superpowers:* lifecycle phase preference silently missed."
```

---

### Task 6: `doctor` reports catalog health honestly

Per the 2026-07-30 decision: report the 216 unparseable files, do not widen the parser.

**Files:**
- Modify: `src/skill_advisor/cli.py:162-227` (`_cmd_doctor`)
- Test: `tests/test_doctor_cli.py`

**Interfaces:**
- Produces: `catalog.pool_health(config=None, *, overrides_table=None) -> dict` with keys `skill_md_files: int`, `parseable: int`, `unparseable: list[str]` (directory names, sorted), `pool: int`, `pickable: int`, `excluded_but_enabled: list[str]`.

- [ ] **Step 1: Write the failing test**

```python
def test_doctor_reports_unparseable_skill_files(isolated_paths, fake_claude_home, capsys):
    """23% of SKILL.md files on the author's machine have no frontmatter and are
    invisible to the scanner. Silence about that reads as full coverage."""
    from skill_advisor import cli

    broken = fake_claude_home / "skills" / "no-frontmatter"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# Just a heading, no frontmatter\n", encoding="utf-8")

    try:
        cli.main(["doctor"])   # ends in sys.exit; tests/test_doctor_cli.py:23 idiom
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "unparseable" in out.lower()
    assert "no-frontmatter" in out


def test_doctor_reports_pickable_versus_pool(isolated_paths, fake_claude_home, capsys):
    from skill_advisor import cli

    paths_settings = __import__("skill_advisor.paths", fromlist=["paths"]).settings_file()
    paths_settings.write_text('{"skillOverrides": {"demo-skill": "off"}}', encoding="utf-8")

    try:
        cli.main(["doctor"])   # ends in sys.exit; tests/test_doctor_cli.py:23 idiom
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "pickable" in out.lower()
    assert "pool" in out.lower()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_doctor_cli.py -q -k "unparseable or pickable"
```

Expected: FAIL — neither string appears.

- [ ] **Step 3: Add `pool_health()` to `catalog.py`**

```python
def pool_health(
    config: Config | None = None,
    *,
    overrides_table: dict[str, str] | None = None,
) -> dict:
    """Counts for `doctor`: how much of the disk the scanner can actually see.

    Measured 2026-07-30: 216 of 947 SKILL.md files (23%) carry no parseable YAML
    frontmatter and are silently invisible. Reporting that is deliberate — the
    files belong to third-party skill libraries and fixing them is out of scope,
    but a rotation pool that silently excludes a quarter of the disk should say so.
    """
    cfg = config or load_config()
    table = overrides_table if overrides_table is not None else overrides.read()
    files = 0
    unparseable: list[str] = []
    for root in paths.skill_roots():
        for skill_md in root.rglob("SKILL.md"):
            files += 1
            fm = parse_frontmatter(skill_md)
            if not fm or not fm.get("name") or not fm.get("description"):
                unparseable.append(skill_md.parent.name)
    entries = scan(cfg, overrides_table=table)
    off_keys = {k for k, v in table.items() if v.strip().lower() == overrides.OFF}
    excluded = set(cfg.catalog.exclude_names)
    return {
        "skill_md_files": files,
        "parseable": files - len(unparseable),
        "unparseable": sorted(set(unparseable)),
        "pool": len(entries),
        "pickable": sum(1 for e in entries if e.enabled),
        "excluded_but_enabled": sorted(excluded - off_keys),
    }
```

- [ ] **Step 4: Print it in `_cmd_doctor`**

Insert before the parallelization warning block:

```python
    health = catalog_mod.pool_health(cfg)
    print(f"skill files    : {health['skill_md_files']} on disk, {health['parseable']} parseable")
    print(f"catalog        : {health['pool']} in pool, {health['pickable']} pickable")
    if health["unparseable"]:
        shown = ", ".join(health["unparseable"][:5])
        more = len(health["unparseable"]) - 5
        print(
            f"WARN: {len(health['unparseable'])} SKILL.md files are unparseable "
            f"(no YAML frontmatter) and invisible to the advisor: {shown}"
            + (f", +{more} more" if more > 0 else "")
        )
    if health["excluded_but_enabled"]:
        print(
            f"NOTE: {len(health['excluded_but_enabled'])} names in catalog.exclude_names "
            f"are enabled in Claude Code — muted in the advisor but invocable. "
            f"`skill-advisor migrate-excludes` reconciles this."
        )
```

- [ ] **Step 5: Run tests and the real doctor**

```bash
uv run pytest -q && skill-advisor doctor
```

Expected: PASS, and the real run should report roughly `947 on disk, 731 parseable`, `~93 pickable`, and 22 excluded-but-enabled.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/skill_advisor/catalog.py src/skill_advisor/cli.py tests/test_doctor_cli.py && uv run ruff format --check src/skill_advisor/catalog.py src/skill_advisor/cli.py tests/test_doctor_cli.py
git add src/skill_advisor/catalog.py src/skill_advisor/cli.py tests/test_doctor_cli.py
git commit -m "feat(doctor): report pool size, pickable count, and unparseable SKILL.md files"
```

---

### Task 7: Capture which skill was invoked

`record_stop` logs `"Skill"` 1,482 times and never which one. Without this there is no `invocation_rate` signal — ever. The fix mirrors the `subagent_type` extraction already at `hook.py:279-284`. It starts cold: rotation v1 scores without it.

**Files:**
- Modify: `src/skill_advisor/hook.py:268-315` (`run_posttooluse`), `318+` (`run_stop`)
- Modify: `src/skill_advisor/lifecycle.py` (`TurnState`, `record_tool`)
- Modify: `src/skill_advisor/telemetry.py:129+` (`record_stop`)
- Test: `tests/test_hook.py`, `tests/test_telemetry.py`

**Interfaces:**
- Produces:
  - `lifecycle.TurnState.skills_invoked: list[str]` (default empty).
  - `lifecycle.record_tool(session_id, tool_name, *, subagent_type=None, skill_name=None)`.
  - `telemetry.record_stop(..., skills: Iterable[str] = ())` → event key `"skills"`.

- [ ] **Step 1: Write the failing test**

Match the established posttooluse idiom at `tests/test_hook.py:125` — patch `sys.stdin`, not `_read_input`:

```python
def test_posttooluse_captures_the_invoked_skill_name(monkeypatch):
    """`tools` records only the string "Skill" — 1,482 times across 3,971 stop
    events, never which one. invocation_rate is uncomputable without this."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle

    event = {
        "session_id": "sess-1",
        "tool_name": "Skill",
        "tool_input": {"skill": "superpowers:writing-plans"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-1")   # lifecycle.py:453, returns TurnState | None
    assert turn is not None
    assert turn.skills_invoked == ["superpowers:writing-plans"]
    assert turn.tool_names == ["Skill"]    # existing path still records the tool


def test_posttooluse_ignores_a_missing_skill_field(monkeypatch):
    """Non-Skill tools carry no `skill` key; that must not append an empty name."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle

    event = {"session_id": "sess-2", "tool_name": "Read", "tool_input": {"file_path": "/x"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-2")
    assert turn is not None
    assert turn.skills_invoked == []


def test_stop_event_records_invoked_skills(isolated_paths):
    import json as _json

    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s1", tools=["Skill"], subagents=[],
        skills=["superpowers:writing-plans"],
        config=TelemetryConfig(events_enabled=True),
    )
    events = [
        _json.loads(line)
        for line in paths.events_file().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["skills"] == ["superpowers:writing-plans"]
```

`lifecycle.load_turn(session_id)` is at `lifecycle.py:453` and returns `TurnState | None` — `run_stop` uses it to read the turn back.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_hook.py tests/test_telemetry.py -q -k skill
```

Expected: FAIL — `AttributeError: 'TurnState' object has no attribute 'skills_invoked'`.

- [ ] **Step 3: Extend `TurnState`**

Add to the dataclass at `lifecycle.py:428`:

```python
    skills_invoked: list[str] = field(default_factory=list)
```

and append to it in `record_tool` when `skill_name` is given, mirroring how `subagents_invoked` is appended.

- [ ] **Step 4: Extract the skill name in the hook**

In `run_posttooluse`, immediately after the `subagent_type` block:

```python
    skill_name = None
    if isinstance(tool_input, dict):
        raw_skill = tool_input.get("skill")
        if raw_skill is not None:
            skill_name = str(raw_skill).strip() or None
```

and pass `skill_name=skill_name` to `lifecycle.record_tool(...)`.

- [ ] **Step 5: Emit it from the Stop hook**

Add `skills: Iterable[str] = ()` to `telemetry.record_stop`, put `"skills": [str(s) for s in skills]` in the event dict, and pass `skills=turn.skills_invoked` from `run_stop`.

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src/skill_advisor/hook.py src/skill_advisor/lifecycle.py src/skill_advisor/telemetry.py tests/test_hook.py tests/test_telemetry.py && uv run ruff format --check src/skill_advisor/hook.py src/skill_advisor/lifecycle.py src/skill_advisor/telemetry.py tests/test_hook.py tests/test_telemetry.py
git add src/skill_advisor/hook.py src/skill_advisor/lifecycle.py src/skill_advisor/telemetry.py tests/test_hook.py tests/test_telemetry.py
git commit -m "feat(telemetry): record which skill was invoked, not just that one was

Prerequisite for invocation_rate. Starts cold — no history exists."
```

---

# Phase B — the verification gate

---

### Task 8: Verify `skillOverrides` is honoured via `--settings`

**STOP HERE if this fails.** The spec is explicit: "Do not proceed past task 1 on an assumption." Phase C's entire write mechanism depends on the answer.

**Files:**
- Create: `docs/superpowers/notes/2026-07-30-skilloverrides-verification.md`
- No source changes.

**Interfaces:** none. This task produces a written finding.

- [ ] **Step 1: Pick a currently-disabled skill**

```bash
python3 -c "
import json
from pathlib import Path
ov = json.loads((Path.home()/'.claude/settings.json').read_text())['skillOverrides']
print([k for k,v in ov.items() if v=='off'][:5])
"
```

Choose one that exists on disk with parseable frontmatter — verify with `head -5 ~/.claude/skills/<name>/SKILL.md`.

- [ ] **Step 2: Build a test settings file that enables exactly that one**

```bash
cat > /tmp/so-test-settings.json <<'JSON'
{
  "skillOverrides": {
    "<CHOSEN-SKILL>": "on"
  }
}
JSON
```

- [ ] **Step 3: Launch Claude Code with it and check the skill list**

```bash
claude --settings /tmp/so-test-settings.json -p 'List every skill name available to you, one per line. No prose.' \
  | tee /tmp/so-test-with.txt
claude -p 'List every skill name available to you, one per line. No prose.' \
  | tee /tmp/so-test-without.txt
diff /tmp/so-test-without.txt /tmp/so-test-with.txt
```

- [ ] **Step 4: Classify the result and record it**

Write `docs/superpowers/notes/2026-07-30-skilloverrides-verification.md` recording the exact commands, the diff, and which of these three it was:

- **Merges per-key** — the chosen skill appears and the other ~61 enabled ones remain. Phase C proceeds as designed: write only the delta.
- **Replaces wholesale** — the chosen skill appears and the other enabled ones vanish. Phase C proceeds, but `overrides.write()` must emit the **complete** map (all ~727 pool entries), and Task 12 must add a floor check that refuses to write a map that would leave fewer than `min_active` skills enabled.
- **Ignored entirely** — no change. **Stop.** Phase C as designed is dead. Report back with the finding; the decision (write `~/.claude/settings.json` directly with safeguards, versus falling back to propose-only output) returns to the user.

- [ ] **Step 5: Commit the finding**

```bash
git add docs/superpowers/notes/2026-07-30-skilloverrides-verification.md
git commit -m "docs: record the skillOverrides --settings verification result"
```

---

# Phase C — rotation

**Gated on Task 8 passing.** Do not begin otherwise.

---

### Task 9: `migrate-excludes` — one source of truth

**Files:**
- Modify: `src/skill_advisor/overrides.py` (add `write`)
- Modify: `src/skill_advisor/cli.py` (add `_cmd_migrate_excludes` + parser)
- Test: `tests/test_overrides.py`, `tests/test_migrate_cli.py` (create)

**Interfaces:**
- Produces:
  - `overrides.write(updates: dict[str, str], *, settings_path: Path | None = None) -> bool` — merges `updates` into `skillOverrides` in the advisor's settings file. Read-modify-write, round-trip validated, atomic replace, per `baseline.py:212-255`. Returns `False` on any failure and leaves the file byte-identical.
  - `cli` verb `skill-advisor migrate-excludes [--revert]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_write_merges_and_leaves_other_keys_alone(isolated_paths):
    import json

    from skill_advisor import overrides, paths

    paths.settings_file().write_text(
        json.dumps({"effortLevel": "high", "skillOverrides": {"a": "off"}}), encoding="utf-8"
    )
    assert overrides.write({"b": "off"}) is True

    data = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert data["effortLevel"] == "high"
    assert data["skillOverrides"] == {"a": "off", "b": "off"}


def test_write_aborts_on_unparseable_settings_leaving_bytes_identical(isolated_paths):
    from skill_advisor import overrides, paths

    paths.settings_file().write_text("{ not json", encoding="utf-8")
    before = paths.settings_file().read_bytes()
    assert overrides.write({"b": "off"}) is False
    assert paths.settings_file().read_bytes() == before
```

Create `tests/test_migrate_cli.py`:

```python
import json
import tomllib

from skill_advisor import cli, paths


def _write_config(config_home, names):
    body = "[catalog]\nexclude_names = [" + ", ".join(f'"{n}"' for n in names) + "]\n"
    (config_home / "config.toml").write_text(body, encoding="utf-8")


def _ns(**kw):
    base = {"revert": False, "verbose": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_migrate_moves_excludes_into_skill_overrides(isolated_paths, capsys):
    _write_config(isolated_paths["config_home"], ["alpha", "beta"])

    assert cli._cmd_migrate_excludes(_ns()) == 0

    settings = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert settings["skillOverrides"] == {"alpha": "off", "beta": "off"}

    cfg = tomllib.loads((isolated_paths["config_home"] / "config.toml").read_text())
    assert cfg["catalog"]["exclude_names"] == []


def test_migrate_backs_up_both_files_and_prints_a_revert_command(isolated_paths, capsys):
    _write_config(isolated_paths["config_home"], ["alpha"])
    assert cli._cmd_migrate_excludes(_ns()) == 0
    out = capsys.readouterr().out

    assert (isolated_paths["config_home"] / "config.toml.pre-migrate.bak").is_file()
    assert "migrate-excludes --revert" in out


def test_revert_restores_both_files_exactly(isolated_paths):
    _write_config(isolated_paths["config_home"], ["alpha", "beta"])
    original = (isolated_paths["config_home"] / "config.toml").read_bytes()

    assert cli._cmd_migrate_excludes(_ns()) == 0
    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 0

    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == original


def test_revert_with_no_backups_fails_loudly(isolated_paths, capsys):
    """`rotate` and `migrate-excludes` are CLI verbs, not hooks — they must not
    be silent on error the way the hook paths are."""
    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 1
    assert "nothing to revert" in capsys.readouterr().out
```

Add `import argparse` at the top. These call the command functions directly rather than `cli.main([...])`, which ends in `sys.exit` — the idiom at `tests/test_report_cli.py:74`.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_migrate_cli.py tests/test_overrides.py -q
```

Expected: FAIL — `invalid choice: 'migrate-excludes'`.

- [ ] **Step 3: Add `overrides.write`**

```python
def write(updates: dict[str, str], *, settings_path: Path | None = None) -> bool:
    """Merge `updates` into skillOverrides in the ADVISOR's settings file.

    Never touches ~/.claude/settings.json — that promise is why this tool is
    safe to run. Same read-modify-write contract as baseline._write_settings_effort,
    including the documented race with install.render_settings(): the later
    writer wins and no lock is taken, judged acceptable for a single-user tool.
    """
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
```

Add `import os` to the module imports.

- [ ] **Step 4: Add the CLI verb**

```python
_MIGRATE_BACKUP_SUFFIX = ".pre-migrate.bak"


def _cmd_migrate_excludes(args: argparse.Namespace) -> int:
    """Move catalog.exclude_names into skillOverrides, once.

    Deliberate, accepted consequence: the migrated names become
    rotation-eligible. Some were muted for irrelevance and may come back.
    Hence the backups and the printed revert command — read the first
    `rotate --dry-run` after migrating rather than applying it blind.
    """
    cfg_path = paths.config_file()
    settings_path = paths.settings_file()
    cfg_bak = cfg_path.with_name(cfg_path.name + _MIGRATE_BACKUP_SUFFIX)
    settings_bak = settings_path.with_name(settings_path.name + _MIGRATE_BACKUP_SUFFIX)

    if args.revert:
        restored = []
        for bak, live in ((cfg_bak, cfg_path), (settings_bak, settings_path)):
            if bak.is_file():
                live.write_bytes(bak.read_bytes())
                bak.unlink()
                restored.append(str(live))
        if not restored:
            print("nothing to revert: no .pre-migrate.bak files found")
            return 1
        print("restored:\n  " + "\n  ".join(restored))
        return 0

    cfg = load_config()
    names = list(cfg.catalog.exclude_names)
    if not names:
        print("catalog.exclude_names is already empty; nothing to migrate")
        return 0

    paths.ensure_dirs()
    if cfg_path.is_file():
        cfg_bak.write_bytes(cfg_path.read_bytes())
    if settings_path.is_file():
        settings_bak.write_bytes(settings_path.read_bytes())

    if not overrides.write({n: overrides.OFF for n in names}):
        print("ERROR: could not write skillOverrides; config.toml left unchanged")
        return 1

    text = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
    text = re.sub(
        r"exclude_names\s*=\s*\[[^\]]*\]",
        "exclude_names = []  # migrated to skillOverrides; see `skill-advisor rotate`",
        text,
        count=1,
        flags=re.DOTALL,
    )
    if "exclude_names" not in text:
        text += "\n[catalog]\nexclude_names = []  # migrated to skillOverrides\n"
    cfg_path.write_text(text, encoding="utf-8")

    print(f"migrated {len(names)} names from catalog.exclude_names into skillOverrides")
    print(f"backups: {cfg_bak}\n         {settings_bak}")
    print("revert with: skill-advisor migrate-excludes --revert")
    print("NEXT: run `skill-advisor build`, then read `skill-advisor rotate` carefully "
          "before applying — the migrated names are now rotation-eligible.")
    return 0
```

Register it in `_build_parser`:

```python
    p_migrate = sub.add_parser(
        "migrate-excludes",
        help="move catalog.exclude_names into skillOverrides (one-time, reversible)",
    )
    p_migrate.add_argument("--revert", action="store_true", help="restore the pre-migration backups")
    p_migrate.set_defaults(func=_cmd_migrate_excludes)
```

Add `import re` and `from . import overrides` to `cli.py` if absent.

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check src/skill_advisor/overrides.py src/skill_advisor/cli.py tests/test_overrides.py tests/test_migrate_cli.py && uv run ruff format --check src/skill_advisor/overrides.py src/skill_advisor/cli.py tests/test_overrides.py tests/test_migrate_cli.py
git add src/skill_advisor/overrides.py src/skill_advisor/cli.py tests/test_overrides.py tests/test_migrate_cli.py
git commit -m "feat(cli): migrate-excludes makes skillOverrides the single source of truth"
```

---

### Task 10: The centroid sketch

**Files:**
- Create: `src/skill_advisor/centroids.py`
- Modify: `src/skill_advisor/paths.py` (add `centroids_file()`), `src/skill_advisor/cli.py` (`uninstall` removes it)
- Test: `tests/test_centroids.py` (create)

**Interfaces:**
- Produces:
  - `centroids.DIM = 384`, `centroids.DEFAULT_K = 8`
  - `centroids.Sketch(vectors: np.ndarray, counts: np.ndarray, observed: int)` — `vectors` is `(K, 384)` float32, L2-normalised, all-zero rows are unclaimed slots.
  - `centroids.empty(k: int = DEFAULT_K) -> Sketch`
  - `centroids.load() -> Sketch` / `centroids.save(sketch: Sketch) -> bool` — `load` returns `empty()` on a missing or wrong-shaped file and never raises.
  - `centroids.observe(sketch: Sketch, vec: np.ndarray) -> None` — mutates in place. Claims an unused slot if one exists (seeding), else nudges the nearest by `c += (v - c) / (count + 1)` then renormalises.
  - `centroids.fit(sketch: Sketch, embeddings: np.ndarray, *, min_observed: int) -> np.ndarray | None` — `max_i cosine(embeddings, centroid_i)` over *claimed* centroids only, shape `(N,)`. Returns `None` when `sketch.observed < min_observed` — the cold-start suppression.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_centroids.py`:

```python
import numpy as np

from skill_advisor import centroids, paths


def _unit(*xs):
    v = np.zeros(centroids.DIM, dtype=np.float32)
    for i, x in enumerate(xs):
        v[i] = x
    n = np.linalg.norm(v)
    return v / (n or 1.0)


def test_file_size_is_fixed_across_many_updates(isolated_paths):
    """Privacy claim: fixed-size lossy aggregate, not a record of any prompt."""
    s = centroids.empty()
    for i in range(200):
        centroids.observe(s, _unit(1.0, i % 7))
    centroids.save(s)
    first = paths.centroids_file().stat().st_size

    for i in range(2000):
        centroids.observe(s, _unit(1.0, i % 11))
    centroids.save(s)
    assert paths.centroids_file().stat().st_size == first
    assert s.vectors.shape == (centroids.DEFAULT_K, centroids.DIM)


def test_centroids_stay_unit_length(isolated_paths):
    s = centroids.empty()
    for i in range(100):
        centroids.observe(s, _unit(1.0, i % 5, 0.3))
    claimed = s.counts > 0
    norms = np.linalg.norm(s.vectors[claimed], axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_seeding_claims_distinct_slots_before_nudging(isolated_paths):
    s = centroids.empty(k=3)
    centroids.observe(s, _unit(1.0))
    centroids.observe(s, _unit(0.0, 1.0))
    centroids.observe(s, _unit(0.0, 0.0, 1.0))
    assert int((s.counts > 0).sum()) == 3


def test_identical_prompts_do_not_each_claim_a_slot(isolated_paths):
    """Seeding one slot per prompt burns all K on near-duplicates: a user whose
    first eight prompts are similar gets eight copies of one region and no
    capacity for the rest, which defeats having K>1 at all."""
    s = centroids.empty(k=4)
    for _ in range(10):
        centroids.observe(s, _unit(1.0))
    assert int((s.counts > 0).sum()) == 1
    assert int(s.counts[0]) == 10


def test_a_second_distinct_region_still_claims_its_own_slot(isolated_paths):
    """The dedup guard must not go so far that genuinely new work is absorbed
    into an existing centroid."""
    s = centroids.empty(k=4)
    for _ in range(10):
        centroids.observe(s, _unit(1.0))
    for _ in range(10):
        centroids.observe(s, _unit(0.0, 1.0))
    assert int((s.counts > 0).sum()) == 2
    assert sorted(int(c) for c in s.counts if c) == [10, 10]


def test_nearest_assignment_is_stable(isolated_paths):
    s = centroids.empty(k=2)
    for _ in range(20):
        centroids.observe(s, _unit(1.0))
    for _ in range(20):
        centroids.observe(s, _unit(0.0, 1.0))
    before = s.vectors.copy()
    centroids.observe(s, _unit(0.99, 0.14))
    moved = np.linalg.norm(s.vectors - before, axis=1)
    assert moved[0] > moved[1]  # the near-x prompt moved the x centroid, not the y one


def test_fit_scores_a_close_skill_above_an_unrelated_one(isolated_paths):
    s = centroids.empty(k=2)
    for _ in range(10):
        centroids.observe(s, _unit(1.0))
    embeddings = np.stack([_unit(0.98, 0.2), _unit(0.0, 1.0)])
    scores = centroids.fit(s, embeddings, min_observed=5)
    assert scores is not None
    assert scores[0] > scores[1]


def test_fit_is_suppressed_before_the_cold_start_floor(isolated_paths):
    s = centroids.empty()
    centroids.observe(s, _unit(1.0))
    assert centroids.fit(s, np.stack([_unit(1.0)]), min_observed=200) is None


def test_load_returns_empty_on_a_wrong_shaped_file(isolated_paths):
    paths.ensure_dirs()
    np.savez(paths.centroids_file(), vectors=np.zeros((3, 5), dtype=np.float32),
             counts=np.zeros(3, dtype=np.int64), observed=np.int64(1))
    s = centroids.load()
    assert s.observed == 0
    assert s.vectors.shape == (centroids.DEFAULT_K, centroids.DIM)
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_centroids.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'skill_advisor.centroids'`.

- [ ] **Step 3: Add `paths.centroids_file()`**

```python
def centroids_file() -> Path:
    """Fixed-size online sketch of prompt embeddings (8 x 384 float32, ~12 KB).

    Holds no prompt text and no per-prompt vectors — it is a lossy aggregate of
    thousands of prompts, not a record of any one of them. Written only when
    telemetry.events_enabled is already true; removed by `uninstall`.
    """
    return cache_dir() / "centroids.npz"
```

- [ ] **Step 4: Create `centroids.py`**

```python
"""Fixed-size online sketch of the user's prompt distribution.

Rotation needs to score a skill that has never been used, which usage data
cannot do. The escape is that relevance does not require usage: every skill
already has an embedding, so its fit can be measured against a sketch of the
work the user actually does.

Eight centroids rather than one because the work is multi-modal — Kubernetes,
frontend, Python services and documentation occupy different regions of
embedding space, and a single mean would sit between all of them describing
none.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from . import paths

log = logging.getLogger(__name__)

DIM = 384
DEFAULT_K = 8

# A prompt within this cosine of an existing centroid is already represented, so
# it nudges that centroid instead of burning one of only K slots on a near
# duplicate. Without this guard, seeding claims one slot per prompt until all K
# are gone — two IDENTICAL prompts take two slots — and a user whose first eight
# prompts are similar ends up with eight copies of the same region and no
# capacity left for the others. That is the exact failure the K>1 design exists
# to avoid. Verified by `test_nearest_assignment_is_stable`.
SEED_SIMILARITY = 0.9


@dataclass
class Sketch:
    vectors: np.ndarray  # (K, DIM) float32, L2-normalised; all-zero row = unclaimed
    counts: np.ndarray   # (K,) int64
    observed: int        # total prompts folded in


def empty(k: int = DEFAULT_K) -> Sketch:
    return Sketch(
        vectors=np.zeros((k, DIM), dtype=np.float32),
        counts=np.zeros(k, dtype=np.int64),
        observed=0,
    )


def load() -> Sketch:
    """Never raises. A missing or wrong-shaped file degrades to an empty sketch,
    which suppresses semantic_fit and makes rotation refuse to run."""
    f = paths.centroids_file()
    if not f.is_file():
        return empty()
    try:
        with np.load(f) as data:
            vectors = data["vectors"].astype(np.float32, copy=False)
            counts = data["counts"].astype(np.int64, copy=False)
            observed = int(data["observed"])
    except (OSError, ValueError, KeyError) as exc:
        log.warning("centroids unreadable (%s); starting empty", exc)
        return empty()
    if vectors.ndim != 2 or vectors.shape[1] != DIM or counts.shape != (vectors.shape[0],):
        log.warning("centroids wrong shape %s; starting empty", vectors.shape)
        return empty()
    return Sketch(vectors=vectors, counts=counts, observed=observed)


def save(sketch: Sketch) -> bool:
    try:
        paths.ensure_dirs()
        np.savez(
            paths.centroids_file(),
            vectors=sketch.vectors,
            counts=sketch.counts,
            observed=np.int64(sketch.observed),
        )
        return True
    except OSError as exc:
        log.debug("centroids save failed: %s", exc)
        return False


def observe(sketch: Sketch, vec: np.ndarray) -> None:
    """Fold one prompt embedding in, mutating `sketch`.

    A prompt that no existing centroid covers claims a free slot, so the K
    regions spread out instead of collapsing into one blob. A prompt that IS
    covered nudges its nearest centroid rather than consuming a slot — see
    SEED_SIMILARITY for why that guard is load-bearing.
    """
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return
    v = v / n

    sketch.observed += 1
    claimed = sketch.counts > 0
    sims = sketch.vectors @ v
    best_sim = float(sims[claimed].max()) if claimed.any() else -1.0

    unclaimed = np.flatnonzero(~claimed)
    if unclaimed.size and best_sim < SEED_SIMILARITY:
        i = int(unclaimed[0])
        sketch.vectors[i] = v
        sketch.counts[i] = 1
        return

    # Restrict argmax to claimed slots: an unclaimed row is all zeros and scores
    # cosine 0, which would beat a genuinely-nearest centroid on a prompt that
    # happens to sit at a negative cosine to it.
    masked = np.where(claimed, sims, -np.inf)
    i = int(np.argmax(masked))
    sketch.counts[i] += 1
    sketch.vectors[i] += (v - sketch.vectors[i]) / float(sketch.counts[i])
    norm = float(np.linalg.norm(sketch.vectors[i]))
    if norm:
        sketch.vectors[i] /= norm


def fit(
    sketch: Sketch, embeddings: np.ndarray, *, min_observed: int
) -> np.ndarray | None:
    """max_i cosine(embedding, centroid_i) per row, or None before cold start."""
    if sketch.observed < min_observed:
        return None
    claimed = sketch.counts > 0
    if not claimed.any() or embeddings.size == 0:
        return None
    sims = np.asarray(embeddings, dtype=np.float32) @ sketch.vectors[claimed].T
    return sims.max(axis=1)
```

- [ ] **Step 5: Remove it on uninstall**

In `_cmd_uninstall` (`cli.py:68`), add `paths.centroids_file()` to whatever list of per-user state files it already unlinks. Add an assertion to the existing uninstall test:

Match the file's existing idiom (`tests/test_uninstall_cli.py:28`): `cli.main` ends in `sys.exit`, so wrap it in `pytest.raises(SystemExit)`. Note `uninstall`'s only flag is `--purge-config` — there is no `--yes`.

```python
def test_uninstall_removes_the_centroid_sketch(isolated_paths):
    import pytest

    from skill_advisor import centroids, cli, paths

    centroids.save(centroids.empty())
    assert paths.centroids_file().is_file()

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["uninstall"])
    assert exc_info.value.code == 0

    assert not paths.centroids_file().is_file()
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src/skill_advisor/centroids.py src/skill_advisor/paths.py src/skill_advisor/cli.py tests/test_centroids.py tests/test_uninstall_cli.py && uv run ruff format --check src/skill_advisor/centroids.py src/skill_advisor/paths.py src/skill_advisor/cli.py tests/test_centroids.py tests/test_uninstall_cli.py
git add src/skill_advisor/centroids.py src/skill_advisor/paths.py src/skill_advisor/cli.py tests/test_centroids.py tests/test_uninstall_cli.py
git commit -m "feat(centroids): fixed-size online sketch of the prompt distribution"
```

---

### Task 11: Feed the sketch from the hook

**Files:**
- Modify: `src/skill_advisor/index.py` (expose `embed_one`), `src/skill_advisor/hook.py`
- Modify: `src/skill_advisor/config.py` (add `RotationConfig`)
- Test: `tests/test_hook.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `index.embed_one(text: str) -> np.ndarray` — a single L2-normalised `(384,)` vector. Extracted from `top_k` so the hook can reuse the query vector it already computed rather than embedding twice.
  - `config.RotationConfig(enabled=False, target_active=75, min_active=25, hysteresis=0.05, exploration_fraction=0.10, recency_days=30, min_observed_prompts=200, centroid_count=8)`, wired as `Config.rotation`.

- [ ] **Step 1: Write the failing tests**

Use `_run_with_stdin` and `_enable_telemetry_in_config`, the helpers already at `tests/test_hook.py:10` and `:63`:

```python
def test_hook_folds_the_prompt_into_the_sketch(isolated_paths):
    import numpy as np

    from skill_advisor import centroids

    _enable_telemetry_in_config(isolated_paths)
    unit = np.zeros(centroids.DIM, dtype=np.float32)
    unit[0] = 1.0

    with patch("skill_advisor.hook.matcher.pick", return_value=None), \
         patch("skill_advisor.hook.index_mod.embed_one", return_value=unit):
        _run_with_stdin({"prompt": "why is this pod crashlooping in sydcdev", "session_id": "s1"})

    assert centroids.load().observed == 1


def test_hook_does_not_write_the_sketch_when_telemetry_is_off(isolated_paths):
    from skill_advisor import paths

    # No config.toml → events_enabled defaults to False.
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        _run_with_stdin({"prompt": "why is this pod crashlooping in sydcdev", "session_id": "s1"})

    assert not paths.centroids_file().exists()


def test_a_failing_sketch_update_never_breaks_the_hook(isolated_paths):
    """Hook paths are silent on error. A broken sketch must not cost a pick."""
    _enable_telemetry_in_config(isolated_paths)
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="...")], state=None)

    with patch("skill_advisor.hook.matcher.pick", return_value=result), \
         patch("skill_advisor.hook.index_mod.embed_one", side_effect=RuntimeError("boom")):
        out = _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    assert "brainstorming" in out
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_hook.py -q -k sketch
```

Expected: FAIL — `AttributeError: <module 'skill_advisor.hook'> does not have the attribute 'index_mod'`.

- [ ] **Step 3: Extract `embed_one`**

In `index.py`:

```python
def embed_one(text: str) -> np.ndarray:
    """A single L2-normalised query vector."""
    model = _embed_model()
    q = np.array(list(model.embed([text])), dtype=np.float32)
    return _normalise(q)[0]
```

and use it inside `top_k` in place of the inline three lines.

- [ ] **Step 4: Add `RotationConfig`**

```python
@dataclass(frozen=True)
class RotationConfig:
    # Automatic rotation stays off until the scoring proves itself against real
    # data. A wrong rotation silently removes a skill the user relies on, which
    # is more disruptive than a wrong effort level.
    enabled: bool = False
    target_active: int = 75          # within the spec's 50-100 band
    min_active: int = 25             # hard floor; a write that would go below aborts
    hysteresis: float = 0.05         # score margin required to swap, prevents thrash
    exploration_fraction: float = 0.10  # slots reserved for zero-usage high-fit skills
    recency_days: int = 30           # never demote a skill invoked inside this window
    min_observed_prompts: int = 200  # cold-start floor for the centroid sketch
    centroid_count: int = 8
```

Add `rotation: RotationConfig = field(default_factory=RotationConfig)` to `Config`, and a `rotation` block to the TOML loader mirroring the existing sections.

- [ ] **Step 5: Fold the prompt into the sketch in the hook**

In `hook.run()`, inside the existing `if cfg.telemetry.events_enabled:` telemetry block (so the privacy claim holds by construction — one gate, not two):

```python
        try:
            sketch = centroids.load()
            centroids.observe(sketch, index_mod.embed_one(prompt))
            centroids.save(sketch)
        except Exception as exc:  # pragma: no cover - defensive; hooks never raise
            log.debug("centroid update failed: %s", exc, exc_info=True)
```

Add `from . import centroids` and `from . import index as index_mod` to `hook.py`.

**Note on cost:** this adds one embedding call per prompt (~100 ms warm). It is inside the `SIGALRM` window, so it eats into `budget_seconds`. Acceptable at the 8 s budget from the fast-fail plan; if the wall-clock regression test in that plan starts failing, move this to the Stop hook instead, where nothing waits on it.

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src/skill_advisor/index.py src/skill_advisor/hook.py src/skill_advisor/config.py tests/test_hook.py tests/test_config.py && uv run ruff format --check src/skill_advisor/index.py src/skill_advisor/hook.py src/skill_advisor/config.py tests/test_hook.py tests/test_config.py
git add src/skill_advisor/index.py src/skill_advisor/hook.py src/skill_advisor/config.py tests/test_hook.py tests/test_config.py
git commit -m "feat(rotation): fold each prompt into the centroid sketch"
```

---

### Task 12: Scoring and swap proposals

**Files:**
- Create: `src/skill_advisor/rotate.py`
- Test: `tests/test_rotate.py` (create)

**Interfaces:**
- Consumes: `centroids.fit`, `CatalogEntry.enabled`, `overrides.override_key`.
- Produces:
  - `rotate.Scored(entry: CatalogEntry, semantic_fit: float, pick_rate: float, invocation_rate: float, last_invoked_days: float | None, total: float)`
  - `rotate.score_pool(entries, embeddings, sketch, stats, cfg) -> list[Scored]` — `stats` is `{name: {"picks": int, "invocations": int, "last_invoked_days": float | None}}`. **Raises `RotationRefused`** on cold start or an empty pool; it never returns `None`, so callers have exactly one failure path to handle.
  - `rotate.Proposal(promote: list[Scored], demote: list[Scored], reason_by_name: dict[str, str])`
  - `rotate.propose(scored, cfg) -> Proposal`
  - `rotate.RotationRefused(Exception)` with a human-readable message — raised for cold start, empty pool, or a proposal that would breach `min_active`.

**Scoring, v1:** `total = 0.6 * semantic_fit + 0.4 * pick_rate`. `invocation_rate` is computed and reported but weighted **0.0**, per the 2026-07-30 decision — it has no history yet (Task 7 only started collecting it). Promoting it to a real weight is a follow-up once weeks of data exist; the field is present so that change is a one-line edit rather than a refactor.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rotate.py`:

```python
import numpy as np
import pytest

from skill_advisor import centroids, rotate
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import RotationConfig


def _entry(name, enabled=True):
    return CatalogEntry(kind="skill", name=name, namespace="user", description="d",
                        path=f"/s/{name}/SKILL.md", enabled=enabled)


def _sketch(observed=1000):
    s = centroids.empty(k=2)
    v = np.zeros(centroids.DIM, dtype=np.float32)
    v[0] = 1.0
    s.vectors[0] = v
    s.counts[0] = observed
    s.observed = observed
    return s


def _emb(rows):
    out = np.zeros((len(rows), centroids.DIM), dtype=np.float32)
    for i, x in enumerate(rows):
        out[i, 0] = x
        out[i, 1] = float(np.sqrt(max(0.0, 1.0 - x * x)))
    return out


def test_refuses_before_the_cold_start_floor():
    cold = centroids.empty()
    with pytest.raises(rotate.RotationRefused, match="prompts"):
        rotate.score_pool([_entry("a")], _emb([0.9]), cold, {}, RotationConfig())


def test_a_zero_usage_skill_can_still_score():
    """The feedback trap: a disabled skill is never invoked, so usage data alone
    can only ever confirm the existing selection. semantic_fit is the escape."""
    scored = rotate.score_pool(
        [_entry("never-used", enabled=False)], _emb([0.95]), _sketch(), {}, RotationConfig()
    )
    assert scored[0].semantic_fit > 0.9
    assert scored[0].total > 0.0


def test_hysteresis_prevents_thrash_on_near_ties():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.05,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry("incumbent", enabled=True), _entry("challenger", enabled=False)]
    scored = rotate.score_pool(entries, _emb([0.90, 0.91]), _sketch(), {}, cfg)
    prop = rotate.propose(scored, cfg)
    assert prop.promote == []
    # And critically: a blocked promotion must not still cost the incumbent.
    # Asserting only on `promote` lets a set-shrinking bug through silently.
    assert prop.demote == []


def test_a_clear_winner_does_swap():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.05,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry("incumbent", enabled=True), _entry("challenger", enabled=False)]
    scored = rotate.score_pool(entries, _emb([0.50, 0.99]), _sketch(), {}, cfg)
    prop = rotate.propose(scored, cfg)
    assert [s.entry.name for s in prop.promote] == ["challenger"]
    assert [s.entry.name for s in prop.demote] == ["incumbent"]


def test_a_recently_invoked_skill_is_never_demoted():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.0,
                         exploration_fraction=0.0, recency_days=30)
    entries = [_entry("used-yesterday", enabled=True), _entry("great-fit", enabled=False)]
    stats = {"used-yesterday": {"picks": 0, "invocations": 1, "last_invoked_days": 1.0}}
    scored = rotate.score_pool(entries, _emb([0.10, 0.99]), _sketch(), stats, cfg)
    assert rotate.propose(scored, cfg).demote == []


def test_the_exploration_slice_promotes_a_zero_usage_skill():
    cfg = RotationConfig(target_active=10, min_active=0, hysteresis=0.0,
                         exploration_fraction=0.20, recency_days=0)
    entries = [_entry(f"used{i}", enabled=True) for i in range(9)]
    entries.append(_entry("unused-but-fits", enabled=False))
    stats = {f"used{i}": {"picks": 50, "invocations": 5, "last_invoked_days": None} for i in range(9)}
    scored = rotate.score_pool(entries, _emb([0.4] * 9 + [0.92]), _sketch(), stats, cfg)
    prop = rotate.propose(scored, cfg)
    assert "unused-but-fits" in [s.entry.name for s in prop.promote]


def test_refuses_to_breach_the_active_set_floor():
    cfg = RotationConfig(target_active=1, min_active=5, hysteresis=0.0,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry(f"s{i}", enabled=True) for i in range(3)]
    scored = rotate.score_pool(entries, _emb([0.9, 0.8, 0.7]), _sketch(), {}, cfg)
    with pytest.raises(rotate.RotationRefused, match="floor"):
        rotate.propose(scored, cfg)


def test_every_proposal_carries_a_reason():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.05,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry("incumbent", enabled=True), _entry("challenger", enabled=False)]
    scored = rotate.score_pool(entries, _emb([0.50, 0.99]), _sketch(), {}, cfg)
    prop = rotate.propose(scored, cfg)
    for s in prop.promote + prop.demote:
        assert prop.reason_by_name[s.entry.name]
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_rotate.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'skill_advisor.rotate'`.

- [ ] **Step 3: Create `rotate.py`**

```python
"""Score the rotation pool and propose swaps. Pure functions over data.

No I/O: callers load the catalog, embeddings, sketch and telemetry stats and
hand them in. That keeps the interesting logic — hysteresis, the exploration
slice, the recency shield, the floor — testable with plain dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import centroids as centroids_mod
from .catalog import CatalogEntry
from .config import RotationConfig

# v1 weights. invocation_rate is computed and reported but weighted zero: the
# telemetry that produces it only started being collected in this same release,
# so it has no history. Promote the weight once weeks of data exist.
W_SEMANTIC = 0.6
W_PICK = 0.4
W_INVOCATION = 0.0


class RotationRefused(Exception):
    """Rotation declined to act. Carries a message fit to print to the user."""


@dataclass(frozen=True)
class Scored:
    entry: CatalogEntry
    semantic_fit: float
    pick_rate: float
    invocation_rate: float
    last_invoked_days: float | None
    total: float


@dataclass
class Proposal:
    promote: list[Scored] = field(default_factory=list)
    demote: list[Scored] = field(default_factory=list)
    reason_by_name: dict[str, str] = field(default_factory=dict)


def score_pool(
    entries: list[CatalogEntry],
    embeddings: np.ndarray,
    sketch: centroids_mod.Sketch,
    stats: dict[str, dict],
    cfg: RotationConfig,
) -> list[Scored]:
    if not entries:
        raise RotationRefused("rotation pool is empty; run `skill-advisor build`")
    fits = centroids_mod.fit(sketch, embeddings, min_observed=cfg.min_observed_prompts)
    if fits is None:
        raise RotationRefused(
            f"only {sketch.observed} prompts observed; rotation needs "
            f"{cfg.min_observed_prompts} before the centroid sketch means anything"
        )

    max_picks = max((int(s.get("picks", 0)) for s in stats.values()), default=0) or 1
    out: list[Scored] = []
    for i, entry in enumerate(entries):
        st = stats.get(entry.name, {})
        picks = int(st.get("picks", 0))
        invocations = int(st.get("invocations", 0))
        pick_rate = picks / max_picks
        invocation_rate = (invocations / picks) if picks else 0.0
        fit = float(fits[i])
        out.append(
            Scored(
                entry=entry,
                semantic_fit=fit,
                pick_rate=pick_rate,
                invocation_rate=invocation_rate,
                last_invoked_days=st.get("last_invoked_days"),
                total=W_SEMANTIC * fit + W_PICK * pick_rate + W_INVOCATION * invocation_rate,
            )
        )
    return out


def _shielded(s: Scored, cfg: RotationConfig) -> bool:
    """Recently invoked ⟹ never demote, whatever the score says."""
    d = s.last_invoked_days
    return d is not None and d <= cfg.recency_days


def propose(scored: list[Scored], cfg: RotationConfig) -> Proposal:
    ranked = sorted(scored, key=lambda s: s.total, reverse=True)
    target = max(int(cfg.target_active), 0)

    explore_slots = int(round(target * cfg.exploration_fraction))
    merit_slots = max(target - explore_slots, 0)

    chosen: list[Scored] = list(ranked[:merit_slots])
    chosen_names = {s.entry.name for s in chosen}

    # Exploration slice: highest semantic_fit among skills with no usage at all.
    # This is the deliberate cost of discovering skills the usage data cannot
    # recommend, and it is what makes this more than a leaderboard. Tracked
    # separately because these must BYPASS hysteresis below — an exploration
    # pick is a reserved slot, not a merit contest it has to win. Subjecting it
    # to the margin test would reject it every time (it loses on merit by
    # construction) and silently delete the whole feature.
    explore_names: set[str] = set()
    if explore_slots:
        unused = [
            s for s in sorted(scored, key=lambda s: s.semantic_fit, reverse=True)
            if s.pick_rate == 0.0 and s.invocation_rate == 0.0
            and s.entry.name not in chosen_names
        ]
        for s in unused[:explore_slots]:
            chosen.append(s)
            chosen_names.add(s.entry.name)
            explore_names.add(s.entry.name)

    incumbents = {s.entry.name for s in scored if s.entry.enabled}
    by_name = {s.entry.name: s for s in scored}
    prop = Proposal()

    # Best incumbent that did NOT make the cut — what a promotion displaces.
    displaced = [by_name[n].total for n in incumbents if n not in chosen_names]
    best_displaced = max(displaced, default=None)

    for name in sorted(chosen_names - incumbents):
        s = by_name[name]
        if name in explore_names:
            prop.promote.append(s)
            prop.reason_by_name[name] = (
                f"exploration slot: semantic_fit={s.semantic_fit:.3f}, no usage history"
            )
            continue
        # Hysteresis: a swap needs a score margin, not a bare ordering
        # difference. Without it, near-tied skills thrash every run.
        if best_displaced is not None and s.total < best_displaced + cfg.hysteresis:
            continue
        prop.promote.append(s)
        prop.reason_by_name[name] = (
            f"semantic_fit={s.semantic_fit:.3f} pick_rate={s.pick_rate:.3f} "
            f"total={s.total:.3f} (>= displaced {best_displaced:.3f} + {cfg.hysteresis:.3f})"
            if best_displaced is not None
            else f"semantic_fit={s.semantic_fit:.3f} total={s.total:.3f} (free slot)"
        )

    # Demote only to make room for a promotion that actually happened, or to
    # come back down to target. Demoting everything outside `chosen` would
    # shrink the active set even when hysteresis blocked every promotion — a
    # swap that never happened must not still cost a skill.
    n_demote = max(0, len(incumbents) + len(prop.promote) - target)
    candidates = sorted(
        (by_name[n] for n in incumbents - chosen_names if not _shielded(by_name[n], cfg)),
        key=lambda s: s.total,
    )
    prop.demote = candidates[:n_demote]
    for s in prop.demote:
        last = (
            f"last invoked {s.last_invoked_days:.0f}d ago"
            if s.last_invoked_days is not None
            else "never invoked"
        )
        prop.reason_by_name[s.entry.name] = (
            f"total={s.total:.3f}, outside the top {target}; {last}"
        )

    # The recency shield can legitimately leave the set above target — a skill
    # used yesterday is never demoted whatever it scores. That overshoot is
    # intended; `min_active` is the only hard bound.
    resulting = len(incumbents) + len(prop.promote) - len(prop.demote)
    if resulting < cfg.min_active:
        raise RotationRefused(
            f"proposal would leave {resulting} skills active, below the floor of "
            f"{cfg.min_active}; refusing"
        )
    return prop
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_rotate.py -q && uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/skill_advisor/rotate.py tests/test_rotate.py && uv run ruff format --check src/skill_advisor/rotate.py tests/test_rotate.py
git add src/skill_advisor/rotate.py tests/test_rotate.py
git commit -m "feat(rotate): scoring, hysteresis, exploration slice and the active-set floor"
```

---

### Task 13: The `rotate` CLI

**Files:**
- Modify: `src/skill_advisor/cli.py`
- Test: `tests/test_rotate_cli.py` (create)

**Interfaces:**
- Produces:
  - `cli._rotation_stats(events: list[dict]) -> dict[str, dict]` — folds the event log into `{name: {"picks", "invocations", "last_invoked_days"}}`. Reuses the event-reading helper `_cmd_report` already uses.
  - CLI verb `skill-advisor rotate [--apply] [--target N]`. Exit 0 on a clean dry run or apply; exit 1 on `RotationRefused`, printing the message.

`telemetry` is already imported in `cli.py` (line 19); `iter_events(cutoff=None)` yields the whole log and skips malformed lines silently.

- [ ] **Step 1: Write the failing tests**

`cli.main()` ends in `sys.exit(...)`, so calling it bare raises `SystemExit`. Two idioms exist in the repo: `try: cli.main([...]) / except SystemExit: pass` (`tests/test_doctor_cli.py:23`) and calling the command function directly with a namespace stub (`tests/test_report_cli.py:74`). **Use the second** — it returns the exit code, which these tests need to assert.

```python
import argparse


def _ns(**kw):
    """Namespace stub matching what the `rotate` parser produces."""
    base = {"apply": False, "target": None, "verbose": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_dry_run_writes_nothing(isolated_paths, capsys):
    """The settings file must be byte-identical after a rotate without --apply."""
    from skill_advisor import cli, paths

    paths.settings_file().write_text('{"skillOverrides": {"a": "off"}}\n', encoding="utf-8")
    before = paths.settings_file().read_bytes()

    _prime_rotatable_state(isolated_paths)   # helper: catalog + embeddings + sketch + events
    assert cli._cmd_rotate(_ns()) == 0

    assert paths.settings_file().read_bytes() == before
    assert "dry run" in capsys.readouterr().out.lower()


def test_apply_writes_the_overrides(isolated_paths, capsys):
    import json

    from skill_advisor import cli, paths

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns(apply=True)) == 0

    table = json.loads(paths.settings_file().read_text(encoding="utf-8"))["skillOverrides"]
    assert table  # something changed


def test_rotate_refuses_and_explains_before_cold_start(isolated_paths, capsys):
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths, observed=3)
    assert cli._cmd_rotate(_ns()) == 1
    assert "prompts" in capsys.readouterr().out.lower()


def test_dry_run_shows_score_signal_and_last_invocation(isolated_paths, capsys):
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns()) == 0
    out = capsys.readouterr().out
    assert "semantic_fit" in out
    assert "PROMOTE" in out or "DEMOTE" in out
```

Write `_prime_rotatable_state` at the top of the file: save a small catalog via `catalog_mod.save`, matching embeddings via `index_mod.save`, a sketch via `centroids.save` with `observed` above/below the floor, and a handful of `kind=prompt` / `kind=stop` lines into `paths.events_file()`. Match the fixture style already used in `tests/test_report_cli.py`.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_rotate_cli.py -q
```

Expected: FAIL — `invalid choice: 'rotate'`.

- [ ] **Step 3: Add the command**

```python
def _rotation_stats(events: list[dict]) -> dict[str, dict]:
    """Fold the event log into per-skill picks / invocations / recency.

    `invocations` comes from the `skills` key on kind=stop events, which only
    started being written in this release — expect zeros for a while.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for ev in events:
        if ev.get("kind") == "stop":
            for name in ev.get("skills") or []:
                slot = out.setdefault(name, {"picks": 0, "invocations": 0, "last_invoked_days": None})
                slot["invocations"] += 1
                try:
                    ts = datetime.fromisoformat(str(ev["ts"]).replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                days = (now - ts).total_seconds() / 86400.0
                prev = slot["last_invoked_days"]
                slot["last_invoked_days"] = days if prev is None else min(prev, days)
            continue
        for pick in ev.get("picks") or []:
            name = pick.get("name")
            if not name:
                continue
            slot = out.setdefault(name, {"picks": 0, "invocations": 0, "last_invoked_days": None})
            slot["picks"] += 1
    return out


def _cmd_rotate(args: argparse.Namespace) -> int:
    cfg = load_config()
    rot = cfg.rotation
    if args.target is not None:
        rot = dataclasses.replace(rot, target_active=int(args.target))

    try:
        idx = index_mod.load()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    sketch = centroids.load()
    events = list(telemetry.iter_events())  # cutoff defaults to None = the whole log
    stats = _rotation_stats(events)

    try:
        scored = rotate.score_pool(idx.catalog, idx.embeddings, sketch, stats, rot)
        proposal = rotate.propose(scored, rot)
    except rotate.RotationRefused as exc:
        print(f"rotation refused: {exc}")
        return 1

    if not cfg.telemetry.events_enabled:
        print("NOTE: telemetry is off — pick_rate and invocation_rate are unavailable; "
              "scoring on semantic_fit alone.")

    active = sum(1 for e in idx.catalog if e.enabled)
    print(f"pool {len(idx.catalog)} · active {active} · target {rot.target_active} "
          f"· sketch {sketch.observed} prompts")
    if not proposal.promote and not proposal.demote:
        print("no changes proposed")
        return 0

    for s in proposal.promote:
        print(f"  PROMOTE {s.entry.invoke_name or s.entry.name:45} {proposal.reason_by_name[s.entry.name]}")
    for s in proposal.demote:
        print(f"  DEMOTE  {s.entry.invoke_name or s.entry.name:45} {proposal.reason_by_name[s.entry.name]}")

    if not args.apply:
        print("\n(dry run — nothing written; rerun with --apply)")
        return 0

    updates: dict[str, str] = {}
    for s in proposal.promote:
        key = overrides.override_key(
            kind=s.entry.kind, namespace=s.entry.namespace, path=s.entry.path, name=s.entry.name
        )
        if key:
            updates[key] = "on"
    for s in proposal.demote:
        key = overrides.override_key(
            kind=s.entry.kind, namespace=s.entry.namespace, path=s.entry.path, name=s.entry.name
        )
        if key:
            updates[key] = overrides.OFF

    if not overrides.write(updates):
        print("ERROR: settings write failed; nothing changed")
        return 1
    print(f"\napplied {len(updates)} changes to {paths.settings_file()}")
    print("run `skill-advisor build` to rebuild the catalog")
    return 0
```

Register it:

```python
    p_rotate = sub.add_parser("rotate", help="propose (or apply) active-skill-set changes")
    p_rotate.add_argument("--apply", action="store_true", help="write the proposal to the settings file")
    p_rotate.add_argument("--target", type=int, default=None, help="override rotation.target_active")
    p_rotate.set_defaults(func=_cmd_rotate)
```

Add `import dataclasses` and `from . import centroids, overrides, rotate` to `cli.py`.

**If Task 8 found that `skillOverrides` replaces wholesale rather than merging:** `updates` must be the complete map. Build it from every pool entry — `"on"` for the chosen set, `"off"` for the rest — and add an explicit guard that refuses to write when the `"on"` count is below `rot.min_active`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check src/skill_advisor/cli.py tests/test_rotate_cli.py && uv run ruff format --check src/skill_advisor/cli.py tests/test_rotate_cli.py
git add src/skill_advisor/cli.py tests/test_rotate_cli.py
git commit -m "feat(cli): skill-advisor rotate with a dry run that writes nothing"
```

---

### Task 14: Drive the whole loop in one test

The spec is emphatic about this, and for a reason: "the effort work's worst defect was a dead code path that 390 unit tests reported as healthy, because the tests called functions in an order production never produces."

**Files:**
- Test: `tests/test_rotation_e2e.py` (create)

**Interfaces:** none; consumes everything above.

- [ ] **Step 1: Write the test**

```python
"""End-to-end: observe prompts -> update centroids -> score -> propose -> apply
-> rebuild catalog -> confirm the matcher's pickable set actually changed.

Unit tests calling these in isolation cannot catch an ordering defect. This one
drives the sequence production produces.
"""
import json
from unittest.mock import patch

import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import centroids, cli, index as index_mod, matcher, overrides, paths
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config, RotationConfig


def test_full_rotation_loop_changes_what_the_matcher_can_pick(isolated_paths):
    dim = centroids.DIM

    def _vec(x):
        v = np.zeros(dim, dtype=np.float32)
        v[0] = x
        v[1] = float(np.sqrt(max(0.0, 1.0 - x * x)))
        return v

    entries = [
        CatalogEntry(kind="skill", name="stale", namespace="user", description="unrelated",
                     path="/s/stale/SKILL.md", enabled=True, invoke_name="stale"),
        CatalogEntry(kind="skill", name="fits", namespace="user", description="on topic",
                     path="/s/fits/SKILL.md", enabled=False, invoke_name="fits"),
    ]
    embeddings = np.stack([_vec(0.10), _vec(0.99)])
    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "h0")

    # 1. Observe prompts, all in the region `fits` occupies.
    sketch = centroids.empty()
    for _ in range(250):
        centroids.observe(sketch, _vec(0.99))
    centroids.save(sketch)

    # Before: the matcher can only reach `stale`.
    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield _vec(0.99)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        before = matcher.pick_stateless("q", Config(), top_k=2, candidates=2, threshold=0.0)
    assert [p.entry.name for p in before.picks] == ["stale"]

    # 2. Rotate and apply.
    (isolated_paths["config_home"] / "config.toml").write_text(
        "[rotation]\ntarget_active = 1\nmin_active = 0\nhysteresis = 0.0\n"
        "exploration_fraction = 0.0\nrecency_days = 0\nmin_observed_prompts = 200\n",
        encoding="utf-8",
    )
    import argparse
    assert cli._cmd_rotate(argparse.Namespace(apply=True, target=None, verbose=False)) == 0

    written = json.loads(paths.settings_file().read_text(encoding="utf-8"))["skillOverrides"]
    assert written["fits"] == "on"
    assert written["stale"] == "off"

    # 3. Rebuild the catalog from the new overrides, reusing the same embeddings.
    table = overrides.read()
    rebuilt = [
        CatalogEntry(**{**e.to_json(), "enabled": overrides.is_enabled(
            overrides.override_key(kind=e.kind, namespace=e.namespace, path=e.path, name=e.name),
            table,
        )})
        for e in entries
    ]
    index_mod.save(rebuilt, embeddings, catalog_mod.compute_hash(rebuilt))

    # 4. After: the matcher reaches `fits` and can no longer reach `stale`.
    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        after = matcher.pick_stateless("q", Config(), top_k=2, candidates=2, threshold=0.0)
    assert [p.entry.name for p in after.picks] == ["fits"]


def test_the_catalog_hash_actually_changed(isolated_paths):
    """Guards the Task 2 failure mode: if the hash ignores `enabled`, `build`
    no-ops after a rotation and step 3 above silently does nothing in real use."""
    a = [CatalogEntry(kind="skill", name="x", namespace="user", description="d",
                      path="/s/x/SKILL.md", enabled=True)]
    b = [CatalogEntry(kind="skill", name="x", namespace="user", description="d",
                      path="/s/x/SKILL.md", enabled=False)]
    assert catalog_mod.compute_hash(a) != catalog_mod.compute_hash(b)
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_rotation_e2e.py -q -v
```

Expected: PASS. If it fails, **do not weaken the test** — it is describing the production sequence. Fix the code.

- [ ] **Step 3: Run everything and commit**

```bash
uv run pytest -q && uv run ruff check tests/test_rotation_e2e.py && uv run ruff format --check tests/test_rotation_e2e.py
git add tests/test_rotation_e2e.py
git commit -m "test(rotation): drive the full observe-score-apply-rebuild loop"
```

---

### Task 15: Migrate for real, and document

**Files:**
- Modify: `README.md`, `docs/superpowers/specs/2026-07-29-catalog-refresh-design.md`

- [ ] **Step 1: Run the migration on the live machine**

```bash
skill-advisor doctor                      # record the before numbers
skill-advisor migrate-excludes
skill-advisor build
skill-advisor rotate                      # READ THIS CAREFULLY — do not --apply blind
```

The 428 migrated names are now rotation-eligible. Some were muted for irrelevance and the first dry run may propose bringing them back. If the proposal looks wrong, `skill-advisor migrate-excludes --revert` restores both files exactly.

- [ ] **Step 2: Record the measured effect**

Re-run the 2026-07-29 measurement and put the numbers in the commit message:

```bash
skill-advisor report --json > /tmp/after.json
python3 -c "
import json
from pathlib import Path
from skill_advisor import catalog, config
c = config.load()
e = catalog.scan(c)
print('pool', len(e), 'pickable', sum(1 for x in e if x.enabled))
"
```

The honest measure the spec names is the ingestion rate — 34.2% before. It is affected by much else and is not a number to optimise directly, but it should move.

- [ ] **Step 3: Correct the spec in place**

Add at the top of `docs/superpowers/specs/2026-07-29-catalog-refresh-design.md`:

```markdown
> **Status 2026-07-30:** implemented — see
> `docs/superpowers/plans/2026-07-30-catalog-refresh.md`. Six claims in this
> document were corrected during planning against live code and live data; the
> plan's "Audit corrections" table is authoritative where the two disagree. In
> particular: the fix site is `lifecycle.pick_candidates_for_phase`, not only
> the matcher; the pool is 727 parseable names, not 947; `skillOverrides` joins
> on directory name, not frontmatter name; and `invocation_rate` had no data
> source at the time of writing.
```

Then fix the body: the "Scoring" table should mark `invocation_rate` as weighted zero in v1, and "Two sets, not one" should name `pick_candidates_for_phase` alongside the matcher.

- [ ] **Step 4: Update the README**

Document `migrate-excludes` and `rotate` (including that a dry run writes nothing), the `[rotation]` config block, and the privacy properties of `centroids.npz` — fixed size, no prompt text, gated on `telemetry.events_enabled`, removed by `uninstall`.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-07-29-catalog-refresh-design.md
git commit -m "docs: record the catalog refresh and correct the spec's stale claims"
```

---

## Self-Review

**Spec coverage.** "Two sets, not one" → Tasks 1, 3, 4. "`exclude_names` is migrated and deleted" → Task 9. "Scoring" → Task 12 (with `invocation_rate` at weight 0 per the 2026-07-30 decision, and Task 7 collecting the data it needs). "The centroid sketch" → Tasks 10, 11. "Rotation" incl. hysteresis, exploration slice, recency shield, dry-run output → Task 12, 13. "Cadence: manual first" → `RotationConfig.enabled=False`, Task 11; automatic mode is out of scope by the spec's own "Deliberately out of scope". "Where the write goes" → Task 9 (`overrides.write`). "Verification gate" → Task 8. All six "Failure modes" rows → `overrides.read` returning `{}` (Task 1), `centroids.load` returning `empty()` (Task 10), `RotationRefused` for cold start / empty pool / floor (Task 12), byte-identical abort on unparseable settings (Task 9). All six "Testing" bullets → Tasks 3, 4, 10, 12, 13, 14. Beyond the spec, justified by the audit: Tasks 2, 5, 6, 7.

**Placeholder scan.** Clean, with two deliberate and clearly-marked exceptions where the plan cannot know the answer yet: Task 13 Step 3 branches on Task 8's outcome (the gate exists precisely to decide that), and Task 13 Step 1's `_prime_rotatable_state` helper is described rather than written out, because it must match the fixture idiom in `tests/test_report_cli.py` which the implementer will have open.

**API references verified against the tree on 2026-07-30:** `lifecycle.load_turn` (`lifecycle.py:453`), `telemetry.iter_events(cutoff=None)` (`telemetry.py:176`), `uninstall`'s only flag being `--purge-config` (`cli.py:762`), `baseline._write_settings_effort`'s write contract (`baseline.py:212-255`), and `_cmd_doctor`'s parallelization warning block (`cli.py:215-222`).

**Type consistency.** `override_key`/`invoke_name`/`is_enabled`/`read`/`write` keep the same signatures from Task 1 through Tasks 9, 13, 14. `CatalogEntry.enabled` flows into `compute_hash` (2), `Index.pickable` (3), `pick_candidates_for_phase` (4), `pool_health` (6), `score_pool` (12). `centroids.Sketch`/`observe`/`fit` are consumed unchanged by Tasks 11, 12, 13, 14. `Scored.total` is the field `propose` sorts on and `_cmd_rotate` prints. `RotationConfig` field names are identical in Tasks 11, 12, 13 and the e2e TOML in Task 14.
