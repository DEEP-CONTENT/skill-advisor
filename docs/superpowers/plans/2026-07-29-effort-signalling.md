# Effort Signalling & Baseline Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `skill-advisor` recommend a reasoning-effort level for every prompt, show it persistently in the Claude Code status line, nudge the user when it disagrees with reality, and slowly tune the launch-time default behind a user veto.

**Architecture:** A classifier rides existing `claude -p` round-trips (no new subprocesses) and writes its recommendation to a cache file. A generated POSIX-shell status line reads that file plus Claude Code's own stdin payload, renders `observed → recommended`, and doubles as the *sensor* that records live effort — the only surface where live effort is visible. The `UserPromptSubmit` hook reads the sensor file to decide whether to emit a `systemMessage` nudge. A rolling window of per-session modal recommendations drives an occasional, announced, vetoable write of `effortLevel` into `claudeskill-settings.json`.

**Tech Stack:** Python 3.11+, stdlib only (`tomllib`, `dataclasses`, `json`, `subprocess`); POSIX `sh` + `jq` for the status line; pytest for tests.

**Spec:** `docs/superpowers/specs/2026-07-29-effort-signalling-design.md`

## Global Constraints

- **Silent-on-error contract.** Every new code path must exit 0 and log to `advisor.log` on any exception. Claude Code must never see a hook failure. This mirrors `hook.run()`'s existing broad `try/except`.
- **No new subprocesses in the hot path.** Effort classification rides the existing `judge.rank()` and `parallelization.detect()` calls. Measured baseline latency is already p50 10.2 s / p95 24.8 s per prompt.
- **`~/.claude/settings.json` is never written.** All settings writes target `paths.settings_file()` (`~/.config/skill-advisor/claudeskill-settings.json`) only.
- **Status line must be POSIX `sh` + `jq`, never Python.** It re-renders continuously; a ~400 ms Python cold start there is unacceptable.
- **Observed and recommended enums differ.** Observed (from harness): `low medium high xhigh max`. Recommended (from classifier): `low medium high xhigh ultracode`. `max` is never recommended; `ultracode` is never observed.
- **`ultracode` is never persisted to settings.** A session whose modal recommendation is `ultracode` contributes `xhigh` to the write-back window.
- **All new config lives under `[effort]`** and every sub-behaviour is independently disableable. With `enabled = false` the advisor must behave byte-identically to today.
- **Tests never touch the real `~/.claude`.** Use the existing autouse `isolated_paths` fixture in `tests/conftest.py`.
- **Python 3.11+** (`tomllib` is stdlib from 3.11). CI runs Linux + macOS on 3.11 and 3.12.

---

## File Structure

**Create:**

| File | Responsibility |
|---|---|
| `src/skill_advisor/effort.py` | Level vocabulary, ordering, the observed/recommended asymmetry, the classifier ladder, and read/write of the two state files. |
| `src/skill_advisor/baseline.py` | The effort feature's mutable JSON store: nudge ledger, rolling window of per-session modal recommendations, write-back to `claudeskill-settings.json`, veto and cooldown, announcement queue. Kept out of `LifecycleState` because `lifecycle.save()` refreshes `updated_at`, which the Stop hook's double-advance guard reads. |
| `src/skill_advisor/statusline.py` | Renders the POSIX-shell status line script text (pure function) and writes it to disk. |
| `tests/test_effort.py` | Levels, ordering, asymmetry, state files, classifier ladder. |
| `tests/test_statusline.py` | Executes the generated script under `sh` with synthetic stdin. |
| `tests/test_baseline.py` | Window, write-back, veto, cooldown, announcement. |

**Modify:**

| File | Change |
|---|---|
| `src/skill_advisor/paths.py:96-120` | Add `effort_file()`, `observed_effort_file()`, `baseline_file()`, `statusline_script()`. |
| `src/skill_advisor/config.py:77-98,154-210` | Add `EffortConfig` dataclass + parsing in `load()`. |
| `src/skill_advisor/judge.py:27-43,93-133` | Optional `effort` field in the schema and in `_parse_judge_reply`. |
| `src/skill_advisor/matcher.py:22-26,132-231` | Carry an `EffortRecommendation` on `PickResult`. |
| `src/skill_advisor/hook.py:47-55,74-136` | Write `effort.json`; emit `systemMessage`; feed the baseline window. |
| `src/skill_advisor/install.py:117-148,151-260` | Register `statusLine`; write the script; extend the default config text. |
| `src/skill_advisor/cli.py` (`_cmd_doctor`) | Report `jq` presence and status line registration. |
| `README.md`, `INSTALL.md`, `examples/config.toml`, `examples/claudeskill-settings.json` | Documentation tasks 13–14. |

---

### Task 1: Effort config

**Files:**
- Modify: `src/skill_advisor/config.py:91-98` (add field to `Config`), `src/skill_advisor/config.py:154-210` (parse)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `config.EffortConfig` with fields `enabled: bool`, `statusline: bool`, `nudge: bool`, `write_back: bool`, `write_back_after_sessions: int`, `veto_cooldown_sessions: int`, `ultracode_nudge: bool`. Reachable as `cfg.effort`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_effort_defaults_are_conservative():
    cfg = config.Config()
    assert cfg.effort.enabled is False
    assert cfg.effort.statusline is True
    assert cfg.effort.nudge is True
    assert cfg.effort.write_back is True
    assert cfg.effort.write_back_after_sessions == 5
    assert cfg.effort.veto_cooldown_sessions == 10
    assert cfg.effort.ultracode_nudge is True


def test_effort_parsed_from_toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[effort]\n"
        "enabled = true\n"
        "statusline = false\n"
        "write_back_after_sessions = 3\n",
        encoding="utf-8",
    )
    cfg = config.load(p)
    assert cfg.effort.enabled is True
    assert cfg.effort.statusline is False
    assert cfg.effort.write_back_after_sessions == 3
    # unspecified keys keep their defaults
    assert cfg.effort.nudge is True


def test_effort_section_absent_yields_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[matcher]\nuse_judge = true\n", encoding="utf-8")
    cfg = config.load(p)
    assert cfg.effort.enabled is False
```

`enabled` defaults to `False` so existing installs are unaffected until the user opts in.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k effort -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'effort'`

- [ ] **Step 3: Write minimal implementation**

Insert after `ParallelizationConfig` (`src/skill_advisor/config.py:89`):

```python
@dataclass(frozen=True)
class EffortConfig:
    # Master toggle. When false, nothing in this feature runs: no classification,
    # no status line registration, no nudge, no write-back. Off by default so
    # existing installs are untouched until the user opts in.
    enabled: bool = False
    # Register a `statusLine` command in claudeskill-settings.json.
    statusline: bool = True
    # Emit a systemMessage when the recommendation disagrees with observed effort.
    nudge: bool = True
    # Allow occasional writes of `effortLevel` into claudeskill-settings.json.
    write_back: bool = True
    # Consecutive qualifying sessions of disagreement before a write happens.
    write_back_after_sessions: int = 5
    # Sessions to suppress write-back for after the user manually overrides.
    veto_cooldown_sessions: int = 10
    # Recommend `ultracode` when the parallelization detector says yes.
    # Never persisted — Claude Code treats ultracode as session-only by design.
    ultracode_nudge: bool = True
```

Add the field to `Config` (`src/skill_advisor/config.py:98`):

```python
    effort: EffortConfig = field(default_factory=EffortConfig)
```

In `load()`, add alongside the other section reads (`src/skill_advisor/config.py:165`):

```python
    effort = raw.get("effort", {}) or {}
```

and add to the returned `Config(...)` after `parallelization=...`:

```python
        effort=EffortConfig(
            enabled=bool(effort.get("enabled", EffortConfig.enabled)),
            statusline=bool(effort.get("statusline", EffortConfig.statusline)),
            nudge=bool(effort.get("nudge", EffortConfig.nudge)),
            write_back=bool(effort.get("write_back", EffortConfig.write_back)),
            write_back_after_sessions=int(
                effort.get("write_back_after_sessions", EffortConfig.write_back_after_sessions)
            ),
            veto_cooldown_sessions=int(
                effort.get("veto_cooldown_sessions", EffortConfig.veto_cooldown_sessions)
            ),
            ultracode_nudge=bool(effort.get("ultracode_nudge", EffortConfig.ultracode_nudge)),
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all existing config tests still green)

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/config.py tests/test_config.py
git commit -m "feat(config): add [effort] section, disabled by default"
```

---

### Task 2: Level vocabulary and the observed/recommended asymmetry

**Files:**
- Create: `src/skill_advisor/effort.py`
- Test: `tests/test_effort.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Constants `LOW MEDIUM HIGH XHIGH MAX ULTRACODE` (all `str`).
  - `RECOMMENDABLE: tuple[str, ...]`, `OBSERVABLE: tuple[str, ...]`.
  - `rank(level: str) -> int | None` — `None` for unknown levels.
  - `to_persistable(level: str) -> str | None` — `ULTRACODE → XHIGH`, `MAX → None`, else identity.
  - `should_nudge(observed: str | None, recommended: str | None) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_effort.py`:

```python
from skill_advisor import effort


def test_enums_are_asymmetric():
    assert effort.ULTRACODE in effort.RECOMMENDABLE
    assert effort.ULTRACODE not in effort.OBSERVABLE
    assert effort.MAX in effort.OBSERVABLE
    assert effort.MAX not in effort.RECOMMENDABLE


def test_rank_ordering():
    assert effort.rank(effort.LOW) < effort.rank(effort.MEDIUM)
    assert effort.rank(effort.MEDIUM) < effort.rank(effort.HIGH)
    assert effort.rank(effort.HIGH) < effort.rank(effort.XHIGH)
    assert effort.rank(effort.XHIGH) < effort.rank(effort.MAX)
    # ultracode resolves to xhigh effort, so it ranks equal to xhigh
    assert effort.rank(effort.ULTRACODE) == effort.rank(effort.XHIGH)


def test_rank_unknown_is_none():
    assert effort.rank("turbo") is None
    assert effort.rank("") is None


def test_to_persistable():
    assert effort.to_persistable(effort.ULTRACODE) == effort.XHIGH
    assert effort.to_persistable(effort.MAX) is None
    assert effort.to_persistable(effort.HIGH) == effort.HIGH
    assert effort.to_persistable("turbo") is None


def test_should_nudge_on_disagreement():
    assert effort.should_nudge(effort.MEDIUM, effort.XHIGH) is True
    assert effort.should_nudge(effort.XHIGH, effort.MEDIUM) is True


def test_should_not_nudge_on_agreement():
    assert effort.should_nudge(effort.HIGH, effort.HIGH) is False
    # ultracode ranks equal to xhigh, so being at xhigh already satisfies it
    assert effort.should_nudge(effort.XHIGH, effort.ULTRACODE) is False


def test_max_silences_the_feature():
    # The user has deliberately gone above anything we know how to recommend.
    assert effort.should_nudge(effort.MAX, effort.LOW) is False
    assert effort.should_nudge(effort.MAX, effort.ULTRACODE) is False


def test_missing_observation_suppresses_nudge():
    # First prompt of a session: sensor has not run yet. Never guess.
    assert effort.should_nudge(None, effort.XHIGH) is False
    assert effort.should_nudge(effort.LOW, None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_effort.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skill_advisor.effort'`

- [ ] **Step 3: Write minimal implementation**

Create `src/skill_advisor/effort.py`:

```python
"""Effort-level vocabulary, ordering, and the classifier.

The observed and recommended level sets are deliberately NOT the same:

    observed    (from Claude Code's statusLine payload)  low medium high xhigh max
    recommended (from this classifier)                   low medium high xhigh ultracode

`max` is never recommended — the classifier has no basis for distinguishing it
from `xhigh`. `ultracode` is never observed — the harness surfaces it as `xhigh`.
"""
from __future__ import annotations

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
XHIGH = "xhigh"
MAX = "max"
ULTRACODE = "ultracode"

RECOMMENDABLE: tuple[str, ...] = (LOW, MEDIUM, HIGH, XHIGH, ULTRACODE)
OBSERVABLE: tuple[str, ...] = (LOW, MEDIUM, HIGH, XHIGH, MAX)

# ultracode resolves to xhigh effort plus a dynamic-workflow flag, so it shares
# xhigh's rank. max sits above both.
_ORDER: dict[str, int] = {LOW: 0, MEDIUM: 1, HIGH: 2, XHIGH: 3, ULTRACODE: 3, MAX: 4}


def rank(level: str) -> int | None:
    """Comparable ordering, or None for an unrecognised level."""
    return _ORDER.get(level)


def to_persistable(level: str) -> str | None:
    """The value safe to write into settings, or None if it must never be written.

    ultracode is session-only by Claude Code's own design, so it degrades to the
    xhigh it resolves to. max is never recommended, so it is never written.
    """
    if level == ULTRACODE:
        return XHIGH
    if level in (LOW, MEDIUM, HIGH, XHIGH):
        return level
    return None


def should_nudge(observed: str | None, recommended: str | None) -> bool:
    """True when the user should be told their effort level disagrees with the task."""
    if not observed or not recommended:
        return False
    if observed == MAX:
        return False
    obs, rec = rank(observed), rank(recommended)
    if obs is None or rec is None:
        return False
    return obs != rec
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_effort.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/effort.py tests/test_effort.py
git commit -m "feat(effort): level vocabulary with observed/recommended asymmetry"
```

---

### Task 3: Effort state files

**Files:**
- Modify: `src/skill_advisor/paths.py:96-120`
- Modify: `src/skill_advisor/effort.py`
- Test: `tests/test_effort.py`, `tests/test_paths.py`

**Interfaces:**
- Consumes: Task 2's constants.
- Produces:
  - `paths.effort_file() -> Path`, `paths.observed_effort_file() -> Path`, `paths.baseline_file() -> Path`, `paths.statusline_script() -> Path`.
  - `effort.EffortRecommendation` frozen dataclass: `level: str`, `reason: str`, `source: str`.
  - `effort.write_recommendation(rec: EffortRecommendation, *, session_id: str | None) -> None`
  - `effort.read_observed() -> tuple[str | None, str | None]` returning `(session_id, level)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_effort.py`:

```python
import json

from skill_advisor import paths


def test_write_recommendation_roundtrip():
    rec = effort.EffortRecommendation(level=effort.XHIGH, reason="5 tasks", source="parallelization")
    effort.write_recommendation(rec, session_id="s1")
    data = json.loads(paths.effort_file().read_text(encoding="utf-8"))
    assert data["level"] == "xhigh"
    assert data["reason"] == "5 tasks"
    assert data["source"] == "parallelization"
    assert data["session_id"] == "s1"


def test_write_recommendation_is_atomic_no_tmp_left():
    rec = effort.EffortRecommendation(level=effort.LOW, reason="r", source="heuristic")
    effort.write_recommendation(rec, session_id="s1")
    leftovers = list(paths.cache_dir().glob("effort.json.*"))
    assert leftovers == []


def test_read_observed_missing_file_returns_none():
    assert effort.read_observed() == (None, None)


def test_read_observed_parses_sensor_file():
    paths.ensure_dirs()
    paths.observed_effort_file().write_text(
        json.dumps({"session_id": "s9", "level": "medium", "ts": 1}), encoding="utf-8"
    )
    assert effort.read_observed() == ("s9", "medium")


def test_read_observed_rejects_corrupt_file():
    paths.ensure_dirs()
    paths.observed_effort_file().write_text("not json{", encoding="utf-8")
    assert effort.read_observed() == (None, None)


def test_read_observed_rejects_unknown_level():
    paths.ensure_dirs()
    paths.observed_effort_file().write_text(
        json.dumps({"session_id": "s9", "level": "turbo"}), encoding="utf-8"
    )
    assert effort.read_observed() == ("s9", None)


def test_write_recommendation_swallows_ensure_dirs_oserror(monkeypatch):
    """Silent-on-error: even directory creation failing must not raise."""
    rec = effort.EffortRecommendation(level=effort.LOW, reason="r", source="heuristic")
    monkeypatch.setattr(
        "skill_advisor.paths.ensure_dirs",
        lambda: (_ for _ in ()).throw(OSError("disk full")),
    )
    effort.write_recommendation(rec, session_id="s1")  # must return normally
```

Append to `tests/test_paths.py`:

```python
def test_effort_paths_live_in_cache_dir():
    assert paths.effort_file().parent == paths.cache_dir()
    assert paths.observed_effort_file().parent == paths.cache_dir()
    assert paths.baseline_file().parent == paths.cache_dir()


def test_statusline_script_lives_in_config_dir():
    assert paths.statusline_script().parent == paths.config_dir()
    assert paths.statusline_script().name == "statusline.sh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_effort.py tests/test_paths.py -v`
Expected: FAIL with `AttributeError: module 'skill_advisor.paths' has no attribute 'effort_file'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/skill_advisor/paths.py` after `telemetry_salt_file()` (`paths.py:101`):

```python
def effort_file() -> Path:
    """Latest effort recommendation, written by the UserPromptSubmit hook."""
    return cache_dir() / "effort.json"


def observed_effort_file() -> Path:
    """Live effort level as last seen by the status line (the sensor)."""
    return cache_dir() / "observed-effort.json"


def baseline_file() -> Path:
    """Rolling window + write-back provenance."""
    return cache_dir() / "baseline.json"


def statusline_script() -> Path:
    """Generated POSIX-sh status line, registered in claudeskill-settings.json."""
    return config_dir() / "statusline.sh"
```

Add to `src/skill_advisor/effort.py`:

```python
import json
import logging
import os
import time
from dataclasses import dataclass

from . import paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffortRecommendation:
    level: str
    reason: str
    source: str  # "parallelization" | "judge" | "phase" | "heuristic"


def write_recommendation(rec: EffortRecommendation, *, session_id: str | None) -> None:
    """Persist the current recommendation for the status line to read.

    Atomic (temp + rename) so a half-written file is never observed by the
    status line, which reads this on every render.
    """
    target = paths.effort_file()
    tmp = target.with_suffix(f".json.tmp.{os.getpid()}")
    payload = {
        "schema": 1,
        "level": rec.level,
        "reason": rec.reason,
        "source": rec.source,
        "session_id": session_id,
        "ts": int(time.time()),
    }
    try:
        # Inside the guard: mkdir can raise OSError too, and the Global
        # Constraints forbid any exception escaping to Claude Code.
        paths.ensure_dirs()
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        log.debug("effort write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def read_observed() -> tuple[str | None, str | None]:
    """(session_id, level) as last recorded by the status line sensor.

    Returns (None, None) when the file is missing or unparseable — the caller
    must treat that as "no observation", never as a guess.
    """
    target = paths.observed_effort_file()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (None, None)
    if not isinstance(data, dict):
        return (None, None)
    session_id = data.get("session_id")
    level = data.get("level")
    return (
        str(session_id) if isinstance(session_id, str) else None,
        level if level in OBSERVABLE else None,
    )
```

Note `tmp.with_suffix` yields `effort.json.tmp.<pid>`; the atomicity test globs `effort.json.*` and must find nothing after a successful write.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_effort.py tests/test_paths.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/paths.py src/skill_advisor/effort.py tests/test_effort.py tests/test_paths.py
git commit -m "feat(effort): atomic recommendation + sensor state files"
```

---

### Task 4: Judge schema extension

**Files:**
- Modify: `src/skill_advisor/judge.py:27-43` (template), `src/skill_advisor/judge.py:57-133` (parse + return)
- Modify: `src/skill_advisor/matcher.py:22-76,118-119` (`StatelessResult`, `pick_stateless`, `_default_picks`)
- Modify: `src/skill_advisor/cli.py:552-560` (`_cmd_match`)
- Test: `tests/test_judge.py`, `tests/test_matcher.py`, `tests/test_match_cli.py`

**Interfaces:**
- Consumes: Task 2's `RECOMMENDABLE`.
- Produces:
  - `judge.JudgeResult` frozen dataclass with `picks: list[Pick]` and `effort: str | None`. `judge.rank()` returns `JudgeResult | None` instead of `list[Pick] | None`.
  - `matcher.StatelessResult` frozen dataclass with `picks: list[ResolvedPick]` and `judge_effort: str | None`. `matcher.pick_stateless()` returns it instead of a bare list.

**Why two new return types:** `matcher.pick_stateless()` at `matcher.py:60` does `raw = judge.rank(...)` then `for p in raw[:k_picks]`, so changing the judge's return type breaks that call site. And Task 8 needs the judge's effort verdict at `matcher.pick()`, two frames up. Both are solved the same way — by returning the value explicitly rather than stashing it in module state. `_cmd_match` and `_default_picks` are the only other consumers.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_judge.py`:

```python
import json

from skill_advisor import effort, judge
from skill_advisor.catalog import CatalogEntry


def _entry(name):
    return CatalogEntry(kind="skill", name=name, namespace="user", description="d", path="/x")


def _envelope(inner: dict) -> str:
    return json.dumps({"result": json.dumps(inner)})


def test_judge_parses_effort_field():
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "skip": False, "effort": "xhigh"}),
        cands,
    )
    assert out.effort == effort.XHIGH
    assert [p.name for p in out.picks] == ["alpha"]


def test_judge_without_effort_field_still_parses():
    """Backward compatibility: a reply omitting `effort` must behave as today."""
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "skip": False}), cands
    )
    assert out.effort is None
    assert [p.name for p in out.picks] == ["alpha"]


def test_judge_drops_out_of_enum_effort():
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "effort": "turbo"}), cands
    )
    assert out.effort is None


def test_judge_drops_max_effort():
    """`max` is observable but never recommendable — the judge must not return it."""
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "effort": "max"}), cands
    )
    assert out.effort is None


def test_judge_skip_true_returns_empty_picks_with_effort():
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [], "skip": True, "effort": "low"}), cands
    )
    assert out.picks == []
    assert out.effort == effort.LOW
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_judge.py -v`
Expected: FAIL with `AttributeError: 'list' object has no attribute 'effort'`

- [ ] **Step 3: Write minimal implementation**

In `src/skill_advisor/judge.py`, add the import and result type after `Pick` (`judge.py:25`):

```python
from . import effort as effort_mod


@dataclass(frozen=True)
class JudgeResult:
    picks: list[Pick]
    effort: str | None = None
```

Replace the schema line in `_JUDGE_TEMPLATE` (`judge.py:29`) and add a rule:

```python
{{"picks": [{{"name": "<exact catalog name>", "reason": "<<=12 words>"}}], "skip": <bool>, "effort": "<low|medium|high|xhigh|ultracode>"}}
```

Add to the Rules block (after the existing third bullet at `judge.py:34`):

```
- `effort` is how much reasoning depth this task warrants: `low` for trivial edits and lookups, `medium` for routine changes, `high` for multi-file work, `xhigh` for design or debugging that needs sustained reasoning, `ultracode` only when the task decomposes into several independent sub-tasks that could run in parallel.
```

Change `rank()`'s signature and final line (`judge.py:57`, `judge.py:90`):

```python
def rank(
    prompt: str, candidates: list[CatalogEntry], config: Config, timeout: float | None = None
) -> JudgeResult | None:
```

Every existing `return None` in `rank()` stays as-is. The final line becomes:

```python
    return _parse_judge_reply(completed.stdout, candidates)
```

(unchanged — `_parse_judge_reply` now returns `JudgeResult | None`).

Rewrite `_parse_judge_reply`'s body from `judge.py:113` onward:

```python
    picks_raw = inner.get("picks")
    if not isinstance(picks_raw, list):
        return None

    raw_effort = inner.get("effort")
    parsed_effort = raw_effort if raw_effort in effort_mod.RECOMMENDABLE else None
    if raw_effort is not None and parsed_effort is None:
        log.info("rejected out-of-enum effort: %r", raw_effort)

    skip = bool(inner.get("skip", False))
    if skip and not picks_raw:
        return JudgeResult(picks=[], effort=parsed_effort)

    valid_names = {e.name for e in candidates}
    picks: list[Pick] = []
    for item in picks_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        reason = item.get("reason", "")
        if not isinstance(name, str) or not isinstance(reason, str):
            continue
        if name not in valid_names:
            log.info("rejected hallucinated pick: %r", name)
            continue
        picks.append(Pick(name=name.strip(), reason=reason.strip()))
    return JudgeResult(picks=picks, effort=parsed_effort)
```

The judge's verdict has to reach `matcher.pick()` in Task 8. Return it
**explicitly** — never via module-level mutable state. Add the result type beside
`PickResult` in `src/skill_advisor/matcher.py:22`:

```python
@dataclass(frozen=True)
class StatelessResult:
    picks: list[ResolvedPick]
    judge_effort: str | None = None
```

Change `pick_stateless()` (`matcher.py:28-37`) to `-> StatelessResult` and wrap
each of its three returns:

```python
    idx = index if index is not None else _load_index()
    if idx is None:
        return StatelessResult(picks=[])
```

```python
    if use_judge:
        cand_entries = [e for e, _ in ranked]
        if not cand_entries:
            return StatelessResult(picks=[])
        raw = judge.rank(prompt, cand_entries, cfg)
        if raw is None:
            return StatelessResult(picks=[])
        by_name = {e.name: e for e in cand_entries}
        out: list[ResolvedPick] = []
        for p in raw.picks[:k_picks]:
            entry = by_name.get(p.name)
            if entry is not None:
                out.append(ResolvedPick(entry=entry, reason=p.reason))
        return StatelessResult(picks=out, judge_effort=raw.effort)
```

```python
    out: list[ResolvedPick] = []
    for entry, score in ranked[:k_picks]:
        if score < min_score:
            break
        out.append(ResolvedPick(entry=entry, reason=f"embedding match ({score:.2f})"))
    return StatelessResult(picks=out)
```

Note the judge branch keeps `judge_effort` even when it produced no picks — a
`skip: true` reply still carries a valid effort assessment.

Update the two consumers. `_default_picks` (`matcher.py:118-119`) keeps its list
contract for now; Task 8 widens it:

```python
def _default_picks(prompt: str, cfg: Config, idx: index_mod.Index) -> list[ResolvedPick]:
    return pick_stateless(prompt, cfg, index=idx).picks
```

And `_cmd_match` in `src/skill_advisor/cli.py:552-560` — append `.picks` to the
existing call:

```python
        picks = matcher.pick_stateless(
            prompt,
            cfg,
            force_judge=args.judge,
            threshold=args.threshold,
            top_k=args.top_k,
            candidates=args.candidates,
            index=idx,
        ).picks
```

Add a guard test to `tests/test_matcher.py` so a module global cannot creep in
later:

```python
def test_judge_verdict_returned_explicitly_not_via_module_state():
    assert not hasattr(matcher, "_LAST_JUDGE_EFFORT")
    assert "judge_effort" in matcher.StatelessResult.__dataclass_fields__
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_judge.py tests/test_matcher.py tests/test_match_cli.py tests/test_lifecycle_config.py -v`
Expected: PASS. All four files exercise the changed return types — `test_match_cli.py`
covers `_cmd_match`, and `test_lifecycle_config.py:138,170` call `matcher.pick(...)`
end-to-end.

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/judge.py src/skill_advisor/matcher.py src/skill_advisor/cli.py \
        tests/test_judge.py tests/test_matcher.py
git commit -m "feat(judge): return effort explicitly via JudgeResult and StatelessResult"
```

---

### Task 5: Classifier ladder

**Files:**
- Modify: `src/skill_advisor/effort.py`
- Test: `tests/test_effort.py`

**Interfaces:**
- Consumes: Task 2 constants, Task 3 `EffortRecommendation`, Task 4 `JudgeResult`, `lifecycle` phase constants.
- Produces: `effort.classify(*, phase, judge_effort, parallel, cfg, prompt) -> EffortRecommendation | None`.

Rungs, first hit wins: `parallelization` → `judge` → `phase` → `heuristic` → `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_effort.py`:

```python
from skill_advisor import config as config_mod
from skill_advisor import lifecycle


def _cfg(**kw):
    base = dict(enabled=True, ultracode_nudge=True)
    base.update(kw)
    return config_mod.Config(effort=config_mod.EffortConfig(**base))


def test_rung1_parallel_wins_and_yields_ultracode():
    rec = effort.classify(
        phase=lifecycle.PLANNING, judge_effort=effort.LOW, parallel=True,
        cfg=_cfg(), prompt="build the thing",
    )
    assert rec.level == effort.ULTRACODE
    assert rec.source == "parallelization"


def test_rung1_suppressed_when_ultracode_nudge_off():
    rec = effort.classify(
        phase=lifecycle.PLANNING, judge_effort=effort.LOW, parallel=True,
        cfg=_cfg(ultracode_nudge=False), prompt="build the thing",
    )
    assert rec.level == effort.LOW
    assert rec.source == "judge"


def test_rung2_judge_used_when_not_parallel():
    rec = effort.classify(
        phase=None, judge_effort=effort.XHIGH, parallel=False, cfg=_cfg(), prompt="x"
    )
    assert rec.level == effort.XHIGH
    assert rec.source == "judge"


def test_rung3_phase_used_when_judge_silent():
    rec = effort.classify(
        phase=lifecycle.COMPLETE, judge_effort=None, parallel=False, cfg=_cfg(), prompt="x"
    )
    assert rec.level == effort.LOW
    assert rec.source == "phase"


def test_rung4_heuristic_long_technical_prompt():
    prompt = "refactor the auth middleware and migrate the session schema to postgres"
    rec = effort.classify(
        phase=None, judge_effort=None, parallel=False, cfg=_cfg(), prompt=prompt
    )
    assert rec.source == "heuristic"
    assert rec.level in (effort.HIGH, effort.XHIGH)


def test_rung4_heuristic_short_prompt_is_low():
    rec = effort.classify(
        phase=None, judge_effort=None, parallel=False, cfg=_cfg(), prompt="rename this var"
    )
    assert rec.source == "heuristic"
    assert rec.level == effort.LOW


def test_disabled_config_returns_none():
    rec = effort.classify(
        phase=lifecycle.PLANNING, judge_effort=effort.XHIGH, parallel=True,
        cfg=_cfg(enabled=False), prompt="x",
    )
    assert rec is None


def test_classify_never_returns_max():
    for phase in (lifecycle.PLANNING, lifecycle.REVIEW, lifecycle.COMPLETE, None):
        rec = effort.classify(
            phase=phase, judge_effort=None, parallel=False, cfg=_cfg(), prompt="a b c d e f g"
        )
        assert rec is None or rec.level != effort.MAX
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_effort.py -k classify -v`
Expected: FAIL with `AttributeError: module 'skill_advisor.effort' has no attribute 'classify'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/skill_advisor/effort.py`:

```python
import re

# Phase → effort. Planning and review are reasoning-heavy; wrapping up is not.
_PHASE_EFFORT: dict[str, str] = {
    "planning": HIGH,
    "parallelization_check": XHIGH,
    "implementation": HIGH,
    "review": HIGH,
    "correction": MEDIUM,
    "complete": LOW,
}

_TECHNICAL = re.compile(
    r"\b(refactor|migrat\w*|debug|architect\w*|design|schema|auth|concurren\w*|"
    r"race|deadlock|performance|optimi[sz]\w*|security|audit|integrat\w*)\b",
    re.IGNORECASE,
)


def _heuristic(prompt: str) -> str:
    """Cheapest rung: prompt shape only. Never returns MAX or ULTRACODE."""
    words = len(prompt.split())
    hits = len(_TECHNICAL.findall(prompt))
    if words < 6 and hits == 0:
        return LOW
    if hits >= 2 or words >= 40:
        return XHIGH
    if hits >= 1 or words >= 15:
        return HIGH
    return MEDIUM


def classify(
    *,
    phase: str | None,
    judge_effort: str | None,
    parallel: bool,
    cfg,
    prompt: str,
) -> EffortRecommendation | None:
    """Resolve a recommendation, or None to stay silent.

    Adds no subprocess: `judge_effort` and `parallel` are results the caller
    already obtained from round-trips it was making anyway.
    """
    if not cfg.effort.enabled:
        return None

    # Rung 1 — the parallelization detector already answered "does this
    # decompose into independent sub-tasks?", which is the ultracode question.
    if parallel and cfg.effort.ultracode_nudge:
        return EffortRecommendation(
            level=ULTRACODE,
            reason="tasks decompose into parallel sub-agents",
            source="parallelization",
        )

    # Rung 2 — the judge, when it ran and returned a valid level.
    if judge_effort in RECOMMENDABLE:
        return EffortRecommendation(
            level=judge_effort, reason="judge assessment", source="judge"
        )

    # Rung 3 — lifecycle phase.
    if phase and phase in _PHASE_EFFORT:
        return EffortRecommendation(
            level=_PHASE_EFFORT[phase], reason=f"{phase} phase", source="phase"
        )

    # Rung 4 — prompt shape.
    if prompt.strip():
        return EffortRecommendation(
            level=_heuristic(prompt), reason="prompt shape", source="heuristic"
        )

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_effort.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/effort.py tests/test_effort.py
git commit -m "feat(effort): classifier ladder over existing round-trips"
```

---

### Task 6: Status line script — renderer and sensor

**Files:**
- Create: `src/skill_advisor/statusline.py`
- Create: `tests/test_statusline.py`

**Interfaces:**
- Consumes: `paths.statusline_script()`, `paths.effort_file()`, `paths.observed_effort_file()`.
- Produces: `statusline.script_text() -> str` (pure), `statusline.write_script() -> Path` (writes mode `0o755`).

The script is POSIX `sh` + `jq` — never Python. It renders `observed → recommended` and writes the sensor file.

- [ ] **Step 1: Write the failing test**

Create `tests/test_statusline.py`:

```python
import json
import os
import shutil
import subprocess

import pytest

from skill_advisor import paths, statusline

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _run(payload: dict) -> str:
    script = statusline.write_script()
    env = dict(os.environ)
    env["SKILL_ADVISOR_CACHE_HOME"] = str(paths.cache_dir())
    out = subprocess.run(
        ["sh", str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _payload(level="high", **kw):
    base = {
        "session_id": "sess-1",
        "effort": {"level": level},
        "model": {"display_name": "Opus"},
        "context_window": {"used_percentage": 34.2},
    }
    base.update(kw)
    return base


def test_renders_observed_level_and_model():
    out = _run(_payload())
    assert "high" in out
    assert "Opus" in out


def test_renders_arrow_on_disagreement():
    paths.ensure_dirs()
    paths.effort_file().write_text(json.dumps({"level": "xhigh"}), encoding="utf-8")
    out = _run(_payload(level="medium"))
    assert "medium" in out and "xhigh" in out
    assert "→" in out


def test_no_arrow_on_agreement():
    paths.ensure_dirs()
    paths.effort_file().write_text(json.dumps({"level": "high"}), encoding="utf-8")
    out = _run(_payload(level="high"))
    assert "→" not in out


def test_writes_sensor_file():
    _run(_payload(level="xhigh"))
    data = json.loads(paths.observed_effort_file().read_text(encoding="utf-8"))
    assert data["level"] == "xhigh"
    assert data["session_id"] == "sess-1"


def test_absent_effort_key_is_survivable():
    """Models without reasoning-effort support omit the key entirely."""
    payload = _payload()
    del payload["effort"]
    out = _run(payload)
    assert "Opus" in out
    assert "→" not in out


def test_malformed_stdin_exits_zero_silently():
    script = statusline.write_script()
    env = dict(os.environ)
    env["SKILL_ADVISOR_CACHE_HOME"] = str(paths.cache_dir())
    out = subprocess.run(
        ["sh", str(script)], input="not json{", capture_output=True, text=True, env=env
    )
    assert out.returncode == 0


def test_script_is_executable():
    script = statusline.write_script()
    assert os.access(script, os.X_OK)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_statusline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skill_advisor.statusline'`

- [ ] **Step 3: Write minimal implementation**

Create `src/skill_advisor/statusline.py`:

```python
"""Generates the POSIX-sh status line script.

Deliberately NOT Python: Claude Code re-renders the status line continuously,
and a ~400 ms Python interpreter start in that loop is unacceptable. The script
needs only `jq`; when `jq` is absent it prints nothing and exits 0.

The script has two jobs:
  1. render  `observed → recommended · model · ctx%`
  2. sense   record the live effort level, which is visible on NO other surface
"""
from __future__ import annotations

import stat
from pathlib import Path

from . import paths

_SCRIPT = r"""#!/usr/bin/env sh
# Generated by `skill-advisor install`. Do not edit — regenerate instead.
set -u

CACHE="${SKILL_ADVISOR_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/skill-advisor}"

command -v jq >/dev/null 2>&1 || exit 0
input=$(cat 2>/dev/null) || exit 0
[ -n "$input" ] || exit 0

observed=$(printf '%s' "$input" | jq -r '.effort.level // empty' 2>/dev/null) || exit 0
session=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
model=$(printf '%s' "$input" | jq -r '.model.display_name // empty' 2>/dev/null)
ctx=$(printf '%s' "$input" | jq -r '.context_window.used_percentage // empty' 2>/dev/null)

# ctx must be numerically well-formed, not just the right character set:
# digits with at most one decimal point, and at least one digit (reject "",
# non-[0-9.] chars, "a.b.c"-style multi-dot, and a bare "." with no digits).
case "$ctx" in
  ''|*[!0-9.]*|*.*.*) ctx="" ;;
esac
case "$ctx" in
  *[0-9]*) : ;;
  *) ctx="" ;;
esac

# --- sensor: the only place live effort is visible ---
if [ -n "$observed" ]; then
  if mkdir -p "$CACHE" 2>/dev/null; then
    tmp="$CACHE/observed-effort.json.tmp.$$"
    if jq -n --arg session "$session" --arg level "$observed" --argjson ts "$(date +%s)" \
         '{session_id:$session, level:$level, ts:$ts}' > "$tmp" 2>/dev/null; then
      mv "$tmp" "$CACHE/observed-effort.json" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    fi
  fi
fi

recommended=""
if [ -f "$CACHE/effort.json" ]; then
  recommended=$(jq -r '.level // empty' < "$CACHE/effort.json" 2>/dev/null)
fi

colour() {
  case "$1" in
    low)       printf '[34m' ;;
    medium)    printf '[36m' ;;
    high)      printf '[32m' ;;
    xhigh|max) printf '[33m' ;;
    ultracode) printf '[35m' ;;
    *)         printf '' ;;
  esac
}
RESET='[0m'

out=""
if [ -n "$observed" ]; then
  out="$(colour "$observed")${observed}${RESET}"
  # ultracode ranks equal to xhigh, so xhigh-observed never disagrees with it
  if [ -n "$recommended" ] && [ "$recommended" != "$observed" ] \
     && ! { [ "$recommended" = "ultracode" ] && [ "$observed" = "xhigh" ]; } \
     && [ "$observed" != "max" ]; then
    out="$out [2m→[0m $(colour "$recommended")${recommended}${RESET}"
  fi
fi

[ -n "$model" ] && { [ -n "$out" ] && out="$out [2m·[0m $model" || out="$model"; }
[ -n "$ctx" ] && out="$out [2m·[0m ctx $(printf '%.0f' "$ctx")%"

[ -n "$out" ] && printf '%b
' "$out"
exit 0
"""


def script_text() -> str:
    """The full script body. Pure — safe to snapshot in tests."""
    return _SCRIPT


def write_script() -> Path:
    """Write the script to `paths.statusline_script()` and chmod it executable."""
    paths.ensure_dirs()
    target = paths.statusline_script()
    target.write_text(script_text(), encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_statusline.py -v`
Expected: PASS (7 tests; whole file skips if `jq` is absent)

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/statusline.py tests/test_statusline.py
git commit -m "feat(statusline): sh+jq renderer that doubles as the effort sensor"
```

---

### Task 7: Register the status line and extend doctor

**Files:**
- Modify: `src/skill_advisor/install.py:117-148` (`render_settings`)
- Modify: `src/skill_advisor/cli.py` (`_cmd_doctor`)
- Test: `tests/test_install.py`, `tests/test_doctor_cli.py`

**Interfaces:**
- Consumes: Task 6's `statusline.write_script()`, Task 1's `cfg.effort.statusline`.
- Produces: `claudeskill-settings.json` gains `"statusLine": {"type": "command", "command": "<abs path to statusline.sh>"}` when enabled.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install.py`:

```python
import json

from skill_advisor import install, paths


def _settings() -> dict:
    return json.loads(paths.settings_file().read_text(encoding="utf-8"))


def test_statusline_registered_when_enabled(monkeypatch):
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: __import__("skill_advisor.config", fromlist=["x"]).Config(
            effort=__import__("skill_advisor.config", fromlist=["x"]).EffortConfig(
                enabled=True, statusline=True
            )
        ),
    )
    install.render_settings()
    data = _settings()
    assert data["statusLine"]["type"] == "command"
    assert data["statusLine"]["command"].endswith("statusline.sh")
    assert paths.statusline_script().is_file()


def test_statusline_absent_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: __import__("skill_advisor.config", fromlist=["x"]).Config(),
    )
    install.render_settings()
    assert "statusLine" not in _settings()


def test_render_settings_preserves_effortlevel_written_by_baseline(monkeypatch):
    """A baseline write must survive a later `skill-advisor install`."""
    paths.ensure_dirs()
    paths.settings_file().write_text(json.dumps({"effortLevel": "high"}), encoding="utf-8")
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: __import__("skill_advisor.config", fromlist=["x"]).Config(),
    )
    install.render_settings()
    assert _settings()["effortLevel"] == "high"


def test_render_settings_preserves_foreign_statusline(monkeypatch):
    """Never clobber a status line the user configured themselves."""
    paths.ensure_dirs()
    paths.settings_file().write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/usr/local/bin/mine.sh"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: __import__("skill_advisor.config", fromlist=["x"]).Config(
            effort=__import__("skill_advisor.config", fromlist=["x"]).EffortConfig(
                enabled=True, statusline=True
            )
        ),
    )
    install.render_settings()
    assert _settings()["statusLine"]["command"] == "/usr/local/bin/mine.sh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_install.py -k statusline -v`
Expected: FAIL with `KeyError: 'statusLine'`

- [ ] **Step 3: Write minimal implementation**

In `src/skill_advisor/install.py`, add imports at the top:

```python
from . import statusline
from .config import load as load_config
```

Then, in `render_settings()` immediately before the final `target.write_text(...)` (`install.py:147`):

```python
    try:
        cfg = load_config()
    except Exception:  # pragma: no cover - defensive; installer must not crash
        cfg = None

    if cfg is not None and cfg.effort.enabled and cfg.effort.statusline:
        existing = data.get("statusLine")
        expected = str(paths.statusline_script())
        existing_cmd = existing.get("command") if isinstance(existing, dict) else None
        # Exact path, never a suffix test: endswith("statusline.sh") would also
        # match /opt/other/user-statusline.sh and clobber a script the user owns.
        is_foreign = isinstance(existing_cmd, str) and existing_cmd != expected
        if not is_foreign:
            data["statusLine"] = {"type": "command", "command": str(statusline.write_script())}
```

The foreign-status-line guard mirrors `_merge_hook_entry`'s sentinel approach: we only own entries whose command ends in our own filename.

For `_cmd_doctor` in `src/skill_advisor/cli.py`, add after the existing catalog checks:

```python
    cfg_effort = cfg.effort
    if cfg_effort.enabled:
        jq = shutil.which("jq")
        print(f"jq             : {jq or 'MISSING (status line will render nothing)'}")
        script = paths.statusline_script()
        print(f"statusline     : {script if script.is_file() else 'not written (run install)'}")
        rec = paths.effort_file()
        print(f"effort state   : {'present' if rec.is_file() else 'none yet'}")
```

Ensure `import shutil` is present in `cli.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_install.py tests/test_doctor_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/install.py src/skill_advisor/cli.py tests/test_install.py tests/test_doctor_cli.py
git commit -m "feat(install): register the status line and report it in doctor"
```

---

### Task 8: Hook integration — write state and nudge

**Files:**
- Create: `src/skill_advisor/baseline.py` (store + nudge ledger; Task 9 extends it)
- Modify: `src/skill_advisor/matcher.py:22-26` (`PickResult`), `src/skill_advisor/matcher.py:132-231` (`pick`)
- Modify: `src/skill_advisor/hook.py:47-55` (`_emit`), `src/skill_advisor/hook.py:74-136` (`run`)
- Test: `tests/test_hook.py`, `tests/test_baseline.py`

**Interfaces:**
- Consumes: Task 5's `effort.classify()`, Task 3's `write_recommendation`/`read_observed`, Task 2's `should_nudge`.
- Produces:
  - `matcher.PickResult` gains `effort: effort.EffortRecommendation | None = None`.
  - `hook._emit(text: str, system_message: str | None = None) -> None`.
  - `baseline.mark_nudged(session_id, observed, recommended) -> bool` (added here, extended in Task 9's module).

**Do NOT store nudge state on `LifecycleState`.** Two reasons, both load-bearing:

1. `lifecycle.save()` (`lifecycle.py:272-277`) unconditionally sets
   `state.updated_at = time.time()`. `hook.run_stop()` (`hook.py:259`) skips
   auto-advance when `time.time() - state.updated_at < 1.0`. So writing nudge
   state through `save()` would refresh that timestamp on every prompt and
   **silently suppress lifecycle auto-advance** whenever a Stop event lands within
   a second of a nudge. A cross-feature regression with no visible symptom.
2. `LifecycleState.from_json()` (`lifecycle.py:249-258`) filters against an explicit
   `known` set, so a new field is silently dropped on every load unless that set is
   also updated — the rate limit would appear to work and never actually persist.

Nudge bookkeeping therefore lives in `baseline.json`, which is the effort feature's
own mutable store and touches nothing the lifecycle depends on.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hook.py`:

```python
import json

from skill_advisor import effort, hook, paths


def _emit_capture(capsys):
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


def test_emit_includes_system_message(capsys):
    hook._emit("ctx", system_message="hello")
    payload = _emit_capture(capsys)
    assert payload["hookSpecificOutput"]["additionalContext"] == "ctx"
    assert payload["systemMessage"] == "hello"


def test_emit_omits_system_message_when_none(capsys):
    hook._emit("ctx")
    payload = _emit_capture(capsys)
    assert "systemMessage" not in payload


def test_nudge_suppressed_without_observation():
    """First prompt of a session — the sensor has not run yet."""
    msg = hook._nudge_message(observed=None, rec=effort.EffortRecommendation("xhigh", "r", "judge"))
    assert msg is None


def test_nudge_message_names_both_levels():
    msg = hook._nudge_message(
        observed="medium", rec=effort.EffortRecommendation("xhigh", "5 tasks", "judge")
    )
    assert "xhigh" in msg and "medium" in msg
    assert "/effort xhigh" in msg


def test_ultracode_nudge_suggests_the_keyword_not_a_slash_command():
    msg = hook._nudge_message(
        observed="medium",
        rec=effort.EffortRecommendation("ultracode", "parallel tasks", "parallelization"),
    )
    assert "ultracode" in msg
    assert "/effort" not in msg


def test_nudge_suppressed_at_max():
    msg = hook._nudge_message(
        observed="max", rec=effort.EffortRecommendation("low", "r", "heuristic")
    )
    assert msg is None


def test_nudge_rate_limited_per_level_pair():
    """A long session must not nag on every prompt for the same disagreement."""
    from skill_advisor import baseline

    assert baseline.mark_nudged("s1", "medium", "xhigh") is True
    assert baseline.mark_nudged("s1", "medium", "xhigh") is False
    # a different pair is a genuinely new piece of information
    assert baseline.mark_nudged("s1", "medium", "low") is True


def test_nudge_bookkeeping_does_not_touch_lifecycle_state():
    """Regression guard: writing nudge state must not refresh `updated_at`.

    `hook.run_stop()` skips auto-advance when the lifecycle state was updated
    less than a second ago. If nudge bookkeeping went through `lifecycle.save()`
    it would bump that timestamp on every prompt and silently disable
    auto-advance.
    """
    from skill_advisor import baseline, lifecycle

    state = lifecycle.start("s1", "build a thing")
    before = lifecycle.load("s1").updated_at
    baseline.mark_nudged("s1", "medium", "xhigh")
    assert lifecycle.load("s1").updated_at == before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hook.py -k "emit or nudge" -v`
Expected: FAIL with `TypeError: _emit() got an unexpected keyword argument 'system_message'`

- [ ] **Step 3: Write minimal implementation**

Add `from . import effort as effort_mod` to the imports at `matcher.py:8-11`.
`PickResult` is extended below, together with the `StatelessResult` change — do
both in one edit.

In `matcher.pick()`, compute the recommendation once before each `return PickResult(...)`. The cleanest change is to wrap the existing returns: rename the current `pick` body to `_pick_inner` and add a thin wrapper at the end of the module:

```python
def pick(
    prompt: str,
    config: Config | None = None,
    session_id: str | None = None,
) -> PickResult | None:
    """Return picks for a prompt, with an effort recommendation attached."""
    cfg = config or load_config()
    result = _pick_inner(prompt, cfg, session_id)
    if result is None or not cfg.effort.enabled:
        return result

    phase = result.state.phase if result.state else None
    parallel = any(p.reason.startswith("parallelization:") for p in result.picks)
    rec = effort_mod.classify(
        phase=phase,
        judge_effort=result.judge_effort,
        parallel=parallel,
        cfg=cfg,
        prompt=prompt,
    )
    return dataclasses.replace(result, effort=rec)
```

Rename the existing `def pick(` at `matcher.py:132` to:

```python
def _pick_inner(
    prompt: str,
    cfg: Config,
    session_id: str | None,
) -> PickResult | None:
```

and delete its first line `cfg = config or load_config()` (the wrapper now owns it).

Task 4 already introduced `StatelessResult` and made `pick_stateless()` return the
judge's verdict explicitly. This task carries that verdict the rest of the way, on
`PickResult` itself — the judge's
effort assessment genuinely *is* part of a match result, so this is a real field,
not a smuggling channel. `PickResult` (`matcher.py:22-26`) becomes:

```python
@dataclass(frozen=True)
class PickResult:
    picks: list[ResolvedPick]
    state: lifecycle.LifecycleState | None  # None when not in an active lifecycle
    judge_effort: str | None = None          # raw verdict from the judge, if it ran
    effort: "effort_mod.EffortRecommendation | None" = None  # resolved recommendation
```

Widen `_default_picks` (narrowed to `.picks` in Task 4) so callers reach both
halves:

```python
def _default_picks(prompt: str, cfg: Config, idx: index_mod.Index) -> "StatelessResult":
    return pick_stateless(prompt, cfg, index=idx)
```

Each of the four `_default_picks` call sites inside `_pick_inner`
(`matcher.py:184`, `191`, `203`, `210`, `226`, `230`) uses `.picks` where it
previously used the list, and the `PickResult(...)` it builds passes the verdict
through. For example `matcher.py:229-231` becomes:

```python
    sr = _default_picks(text, cfg, idx)
    return PickResult(picks=sr.picks, state=None, judge_effort=sr.judge_effort) if sr.picks else None
```

Apply the same shape at every other `_default_picks` site: bind the result to
`sr`, use `sr.picks`, and pass `judge_effort=sr.judge_effort` into the
`PickResult`. Sites that build a `PickResult` purely from `_phase_picks` (which
never runs the judge) leave `judge_effort` at its `None` default.

The `pick()` wrapper then reads it directly — no second judge call, no global:

```python
    rec = effort_mod.classify(
        phase=phase,
        judge_effort=result.judge_effort,
        parallel=parallel,
        cfg=cfg,
        prompt=prompt,
    )
```

Extend Task 4's guard test to cover `PickResult` too:

```python
def test_judge_verdict_returned_explicitly_not_via_module_state():
    assert not hasattr(matcher, "_LAST_JUDGE_EFFORT")
    assert "judge_effort" in matcher.StatelessResult.__dataclass_fields__
    assert "judge_effort" in matcher.PickResult.__dataclass_fields__
```

In `src/skill_advisor/hook.py`, replace `_emit` (`hook.py:47-55`):

```python
def _emit(text: str, system_message: str | None = None) -> None:
    envelope: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    if system_message:
        envelope["systemMessage"] = system_message
    sys.stdout.write(json.dumps(envelope))
```

Add the nudge builder to `hook.py`:

```python
def _nudge_message(observed: str | None, rec) -> str | None:
    """One-line systemMessage, or None when we must stay quiet."""
    if rec is None or not effort.should_nudge(observed, rec.level):
        return None
    if rec.level == effort.ULTRACODE:
        # ultracode is a keyword, not a /effort argument — typing it trips the
        # harness's own built-in badge.
        return (
            f"skill-advisor: this decomposes into parallel work ({rec.reason}) — "
            f"consider the `ultracode` keyword. You're at {observed}."
        )
    return (
        f"skill-advisor: this looks like {rec.level} work ({rec.reason}) — "
        f"you're at {observed}.  /effort {rec.level}"
    )
```

Add `from . import effort` to `hook.py:15`.

In `hook.run()`, after `picks = result.picks if result else []` (`hook.py:103`):

```python
    rec = result.effort if result else None
    nudge = None
    if cfg.effort.enabled and rec is not None:
        try:
            effort.write_recommendation(rec, session_id=session_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("effort write failed: %s", exc, exc_info=True)
        if cfg.effort.nudge:
            obs_session, observed = effort.read_observed()
            if obs_session == session_id:
                candidate = _nudge_message(observed, rec)
                # Read-only check here; the slot is CONSUMED only after a
                # successful emit, inside _emit_nudged(). Consuming it here
                # would burn the slot on any path that never reaches _emit
                # (empty picks, or _emit raising), silently suppressing the
                # nudge for the rest of the session without ever showing it.
                if candidate and not baseline.was_nudged(session_id, observed, rec.level):
                    nudge = candidate
```

and add the emission helper, which is the single place consumption happens:

```python
def _emit_nudged(
    text: str,
    *,
    nudge: str | None,
    session_id: str | None,
    observed: str | None,
    level: str | None,
) -> None:
    """Emit the envelope, then consume the nudge slot only once the emit happened.

    Consumption is tied to this one emission point rather than to any caller's
    branch, so any future emission site that carries a nudge stays correct by
    calling this helper instead of re-deriving "did this reach the user".
    """
    _emit(text, system_message=nudge)
    if nudge:
        try:
            baseline.mark_nudged(session_id, observed, level)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("nudge mark failed: %s", exc, exc_info=True)
```

`baseline.was_nudged(session_id, observed, recommended) -> bool` is the read-only
sibling of `mark_nudged`; it calls `_load()` and never `_save()`.

and change the emit call (`hook.py:109`) to:

```python
            _emit(inject.format(result), system_message=nudge)
```

Create `src/skill_advisor/baseline.py` with just the JSON store and the nudge
ledger. Task 9 extends this same module with the rolling window — do not create a
second file.

```python
"""Mutable state for the effort feature: nudge ledger and (from Task 9) the
rolling window of per-session modal recommendations.

Deliberately separate from `LifecycleState`: `lifecycle.save()` refreshes
`updated_at`, which `hook.run_stop()` uses as its double-advance guard. Writing
effort bookkeeping through it would silently suppress lifecycle auto-advance.
"""
from __future__ import annotations

import json
import logging
import os

from . import paths

log = logging.getLogger(__name__)


def _load() -> dict:
    try:
        data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    paths.ensure_dirs()
    target = paths.baseline_file()
    tmp = target.with_suffix(f".json.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        log.debug("baseline save failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def mark_nudged(session_id: str | None, observed: str | None, recommended: str) -> bool:
    """True the first time this (observed, recommended) pair is nudged this session.

    Rate limits the systemMessage so a long session doesn't nag on every prompt.
    """
    if not session_id:
        return False
    key = f"{observed}>{recommended}"
    data = _load()
    ledger = data.setdefault("nudged", {})
    if not isinstance(ledger, dict):
        ledger = {}
        data["nudged"] = ledger
    seen = list(ledger.get(session_id, []))
    if key in seen:
        return False
    seen.append(key)
    ledger[session_id] = seen
    _save(data)
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_hook.py tests/test_matcher.py tests/test_lifecycle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/hook.py src/skill_advisor/matcher.py src/skill_advisor/baseline.py \
        tests/test_hook.py tests/test_baseline.py
git commit -m "feat(hook): write effort state and emit a rate-limited nudge"
```

---

### Task 9: Baseline window

**Files:**
- Modify: `src/skill_advisor/baseline.py` (created in Task 8 — **extend it**, do not recreate)
- Modify: `tests/test_baseline.py`

**Interfaces:**
- Consumes: Task 2 constants and `to_persistable`, Task 8's `_load`/`_save`.
- Produces:
  - `baseline.record(session_id: str, level: str) -> None` — append one recommendation to the session's tally.
  - `baseline.finalise_session(session_id: str) -> str | None` — compute the modal level, append to the rolling window, clear the tally. Returns the modal level, or `None` when the session had fewer than `_MIN_RECOMMENDATIONS` (3).
  - `baseline.window() -> list[str]` — the rolling window of session modals, oldest first.

- [ ] **Step 1: Write the failing test**

Create `tests/test_baseline.py`:

```python
from skill_advisor import baseline, effort


def test_modal_of_a_session():
    for lvl in ("high", "high", "low"):
        baseline.record("s1", lvl)
    assert baseline.finalise_session("s1") == effort.HIGH


def test_thin_session_is_not_yet_in_the_window_but_tally_survives():
    """Stop fires per TURN, so a 2-recommendation session is simply not ready yet.

    The tally must NOT be cleared — clearing it is what made the window
    permanently empty, since each turn contributes only one recommendation.
    """
    baseline.record("s1", "high")
    baseline.record("s1", "high")
    assert baseline.finalise_session("s1") is None
    assert baseline.window() == []
    baseline.record("s1", "high")            # third turn arrives
    assert baseline.finalise_session("s1") == effort.HIGH
    assert baseline.window() == [effort.HIGH]


def test_long_session_contributes_exactly_one_window_entry():
    """A session finalised on every turn must not flood the window."""
    for _ in range(9):
        baseline.record("s1", "high")
        baseline.finalise_session("s1")
    assert baseline.window() == [effort.HIGH]


def test_distinct_sessions_each_get_an_entry():
    for sid, lvl in (("a", "high"), ("b", "low")):
        for _ in range(3):
            baseline.record(sid, lvl)
            baseline.finalise_session(sid)
    assert baseline.window() == [effort.HIGH, effort.LOW]


def test_finalise_appends_to_window_and_clears_tally():
    for lvl in ("low", "low", "low"):
        baseline.record("s1", lvl)
    baseline.finalise_session("s1")
    assert baseline.window() == [effort.LOW]
    # tally cleared — re-finalising the same session must not double-count
    assert baseline.finalise_session("s1") is None
    assert baseline.window() == [effort.LOW]


def test_ultracode_contributes_xhigh_to_the_window():
    for _ in range(3):
        baseline.record("s1", effort.ULTRACODE)
    assert baseline.finalise_session("s1") == effort.XHIGH


def test_window_is_bounded():
    for i in range(40):
        for _ in range(3):
            baseline.record(f"s{i}", "high")
        baseline.finalise_session(f"s{i}")
    assert len(baseline.window()) <= baseline._WINDOW_CAP


def test_corrupt_baseline_file_resets_cleanly():
    from skill_advisor import paths
    paths.ensure_dirs()
    paths.baseline_file().write_text("{{{", encoding="utf-8")
    assert baseline.window() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_baseline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skill_advisor.baseline'`

- [ ] **Step 3: Write minimal implementation**

Extend `src/skill_advisor/baseline.py` (Task 8 created it with `_load`, `_save`,
and `mark_nudged`). Add the import and constants to the existing header:

```python
from collections import Counter

from . import effort

_MIN_RECOMMENDATIONS = 3
_WINDOW_CAP = 30
```

Then append:

```python
def record(session_id: str, level: str) -> None:
    """Append one recommendation to this session's tally."""
    if not session_id or level not in effort.RECOMMENDABLE:
        return
    data = _load()
    tallies = data.setdefault("tallies", {})
    # Same guard the nudge ledger uses: baseline.json can be structurally
    # corrupt (valid JSON, wrong shape) and this must never raise.
    if not isinstance(tallies, dict):
        tallies = {}
        data["tallies"] = tallies
    tallies.setdefault(session_id, []).append(level)
    _save(data)


def finalise_session(session_id: str) -> str | None:
    """Upsert this session's modal level into the rolling window.

    CRITICAL: the Stop hook fires once per TURN, not once per session, so this
    runs repeatedly for the same session as it grows. Two consequences:

      * The tally is NOT consumed. Clearing it would mean a single-recommendation
        turn is discarded for being under _MIN_RECOMMENDATIONS and the tally can
        never accumulate — which made the whole write-back unreachable.
      * The window entry is UPDATED IN PLACE, keyed by session id, so a long
        session contributes exactly one entry no matter how many turns it runs.
        `write_back_after_sessions` therefore genuinely means sessions.

    Returns the persistable modal level once the session has at least
    _MIN_RECOMMENDATIONS recommendations, else None.
    """
    data = _load()
    tallies = data.get("tallies", {})
    if not isinstance(tallies, dict):
        return None
    levels = tallies.get(session_id)
    if not isinstance(levels, list) or len(levels) < _MIN_RECOMMENDATIONS:
        return None

    modal = Counter(levels).most_common(1)[0][0]
    persistable = effort.to_persistable(modal)
    if persistable is None:
        return None

    entries = _window_entries(data)
    for entry in entries:
        if entry.get("session") == session_id:
            entry["level"] = persistable
            break
    else:
        entries.append({"session": session_id, "level": persistable})
    entries = entries[-_WINDOW_CAP:]
    data["window"] = entries

    # Bound growth: keep tallies only for sessions still represented in the
    # window. Without this, `tallies` grows forever now that it is never popped.
    live = {e["session"] for e in entries}
    data["tallies"] = {k: v for k, v in tallies.items() if k in live}
    _save(data)
    return persistable


def _window_entries(data: dict) -> list[dict]:
    """Window as a list of {"session", "level"} dicts, tolerating corruption."""
    raw = data.get("window", [])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for e in raw:
        if isinstance(e, dict) and isinstance(e.get("session"), str) and e.get("level") in effort.RECOMMENDABLE:
            out.append({"session": e["session"], "level": e["level"]})
    return out


def window() -> list[str]:
    """Rolling window of per-session modal levels, oldest session first.

    Projects the levels out of the {"session","level"} entries so `maybe_write`
    keeps its existing `list[str]` contract.
    """
    return [e["level"] for e in _window_entries(_load())]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_baseline.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/baseline.py tests/test_baseline.py
git commit -m "feat(baseline): rolling window of per-session modal recommendations"
```

---

### Task 10: Write-back with atomic merge

**Files:**
- Modify: `src/skill_advisor/baseline.py`
- Test: `tests/test_baseline.py`

**Interfaces:**
- Consumes: Task 9's `window()`, `paths.settings_file()`.
- Produces:
  - `baseline.current_written_level() -> str | None` — reads `effortLevel` from `claudeskill-settings.json`.
  - `baseline.maybe_write(cfg, *, launch_level: str | None) -> str | None` — returns the newly-written level, or `None` when nothing was written.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_baseline.py`:

```python
import json

import pytest

from skill_advisor import config as config_mod
from skill_advisor import paths


def _cfg(**kw):
    base = dict(enabled=True, write_back=True, write_back_after_sessions=3)
    base.update(kw)
    return config_mod.Config(effort=config_mod.EffortConfig(**base))


def _fill(level, n):
    for i in range(n):
        for _ in range(3):
            baseline.record(f"sess{i}", level)
        baseline.finalise_session(f"sess{i}")


def test_no_write_below_threshold():
    _fill("high", 2)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None
    assert baseline.current_written_level() is None


def test_writes_at_threshold():
    _fill("high", 3)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") == "high"
    assert baseline.current_written_level() == "high"


def test_no_write_when_window_agrees_with_launch():
    _fill("xhigh", 5)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None


def test_write_preserves_existing_settings_keys():
    paths.ensure_dirs()
    paths.settings_file().write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [{"matcher": ""}]}}), encoding="utf-8"
    )
    _fill("low", 3)
    baseline.maybe_write(_cfg(), launch_level="xhigh")
    data = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert data["effortLevel"] == "low"
    assert "hooks" in data  # merged, not clobbered


def test_write_aborts_on_unparseable_settings():
    paths.ensure_dirs()
    paths.settings_file().write_text("not json{", encoding="utf-8")
    _fill("low", 3)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None
    # the original file is left exactly as-is
    assert paths.settings_file().read_text(encoding="utf-8") == "not json{"


def test_write_records_provenance():
    _fill("medium", 3)
    baseline.maybe_write(_cfg(), launch_level="xhigh")
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    entry = data["history"][-1]
    assert entry["from"] == "xhigh"
    assert entry["to"] == "medium"
    assert entry["sessions"] == 3


def test_write_disabled_by_config():
    _fill("low", 5)
    assert baseline.maybe_write(_cfg(write_back=False), launch_level="xhigh") is None


def test_ultracode_is_never_written():
    """Guard the invariant even if a bad level reaches the window."""
    paths.ensure_dirs()
    paths.baseline_file().write_text(
        json.dumps({"window": ["ultracode"] * 5}), encoding="utf-8"
    )
    assert baseline.maybe_write(_cfg(), launch_level="high") is None


def test_no_write_without_a_launch_observation():
    """Model without reasoning-effort support: the whole feature stays silent.

    `launch_level` is None when the sensor never saw an effort level, which is
    exactly the case for a model that doesn't support the dial. Writing a
    baseline there would be acting on no evidence.
    """
    _fill("low", 5)
    assert baseline.maybe_write(_cfg(), launch_level=None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_baseline.py -k write -v`
Expected: FAIL with `AttributeError: module 'skill_advisor.baseline' has no attribute 'maybe_write'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/skill_advisor/baseline.py`:

```python
import time


def current_written_level() -> str | None:
    """`effortLevel` currently in claudeskill-settings.json, if any."""
    try:
        data = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    level = data.get("effortLevel")
    return level if level in effort.RECOMMENDABLE else None


def _write_settings_effort(level: str) -> bool:
    """Merge `effortLevel` into the settings file atomically. False on any failure."""
    target = paths.settings_file()
    data: dict = {}
    if target.is_file():
        try:
            parsed = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("settings unparseable; write-back aborted")
            return False
        if not isinstance(parsed, dict):
            log.warning("settings not a JSON object; write-back aborted")
            return False
        data = parsed

    data["effortLevel"] = level
    tmp = target.with_suffix(f".json.tmp.{os.getpid()}")
    try:
        # Inside the guard, same as effort.write_recommendation: mkdir raises
        # OSError too, and nothing here may escape.
        paths.ensure_dirs()
        serialised = json.dumps(data, indent=2) + "\n"
        json.loads(serialised)  # round-trip validation before it touches the real path
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(target)
        return True
    except (OSError, ValueError) as exc:
        log.warning("settings write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def maybe_write(cfg, *, launch_level: str | None) -> str | None:
    """Write a new baseline if the window has disagreed for long enough.

    `launch_level` is the effective value this session started with — the
    advisor's own written value if present, else the sensor's first observation.
    Returns the level written, or None.
    """
    if not cfg.effort.enabled or not cfg.effort.write_back:
        return None

    data = _load()
    if int(data.get("veto_cooldown_remaining", 0)) > 0:
        return None

    if launch_level is None:
        # No observation ever landed — e.g. a model without reasoning-effort
        # support. Never write a baseline on no evidence.
        return None

    need = max(int(cfg.effort.write_back_after_sessions), 1)
    win = window()
    if len(win) < need:
        return None

    recent = win[-need:]
    if len(set(recent)) != 1:
        return None  # not a sustained signal

    target_level = recent[0]
    if effort.to_persistable(target_level) != target_level:
        return None  # never write ultracode or max
    if target_level == launch_level:
        return None  # already there

    if not _write_settings_effort(target_level):
        return None

    data = _load()
    history = list(data.get("history", []))
    history.append({
        "from": launch_level,
        "to": target_level,
        "sessions": need,
        "ts": int(time.time()),
    })
    data["history"] = history[-50:]
    data["announce"] = {"from": launch_level, "to": target_level, "sessions": need}
    data["window"] = []  # consumed; start a fresh observation window
    _save(data)
    return target_level
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_baseline.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/baseline.py tests/test_baseline.py
git commit -m "feat(baseline): atomic, provenance-tracked effortLevel write-back"
```

---

### Task 11: Veto and cooldown

**Files:**
- Modify: `src/skill_advisor/baseline.py`
- Modify: `src/skill_advisor/hook.py` (`run`)
- Test: `tests/test_baseline.py`

**Interfaces:**
- Consumes: Task 10's `_load`/`_save`, Task 3's `read_observed`.
- Produces:
  - `baseline.note_observation(session_id: str, level: str, cfg) -> bool` — records the session's first observation; returns `True` if this call detected a veto.
  - `baseline.first_observation(session_id: str) -> str | None`.

A veto is: a later observation in a session differs from that session's **first** observation. That definition holds whether or not the advisor has ever written anything.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_baseline.py`:

```python
def test_first_observation_is_remembered():
    baseline.note_observation("s1", "xhigh", _cfg())
    assert baseline.first_observation("s1") == "xhigh"
    baseline.note_observation("s1", "medium", _cfg())
    assert baseline.first_observation("s1") == "xhigh"  # first, not latest


def test_change_after_launch_is_a_veto():
    assert baseline.note_observation("s1", "xhigh", _cfg()) is False
    assert baseline.note_observation("s1", "medium", _cfg()) is True


def test_repeated_same_observation_is_not_a_veto():
    baseline.note_observation("s1", "high", _cfg())
    assert baseline.note_observation("s1", "high", _cfg()) is False


def test_veto_blocks_write_back_for_the_cooldown():
    cfg = _cfg(veto_cooldown_sessions=2)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # veto
    _fill("low", 3)
    assert baseline.maybe_write(cfg, launch_level="xhigh") is None


def test_cooldown_decrements_and_expires():
    cfg = _cfg(veto_cooldown_sessions=1)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # veto → cooldown 1
    baseline.decrement_cooldown()
    _fill("low", 3)
    assert baseline.maybe_write(cfg, launch_level="xhigh") == "low"


def test_veto_resets_the_window():
    _fill("low", 2)
    cfg = _cfg()
    baseline.note_observation("s9", "xhigh", cfg)
    baseline.note_observation("s9", "high", cfg)  # veto
    assert baseline.window() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_baseline.py -k "veto or observation or cooldown" -v`
Expected: FAIL with `AttributeError: module 'skill_advisor.baseline' has no attribute 'note_observation'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/skill_advisor/baseline.py`:

```python
def first_observation(session_id: str) -> str | None:
    data = _load()
    firsts = data.get("first_observations", {})
    value = firsts.get(session_id) if isinstance(firsts, dict) else None
    return value if isinstance(value, str) else None


def note_observation(session_id: str, level: str, cfg) -> bool:
    """Record an observation. Returns True when this call detected a user veto.

    A veto is any change from the session's FIRST observation — the user reached
    for /effort mid-session. That signal is meaningful whether or not the advisor
    has written anything, which is why it is defined relative to the session's own
    launch value rather than to our settings file.
    """
    if not session_id or level not in effort.OBSERVABLE:
        return False

    data = _load()
    firsts = data.setdefault("first_observations", {})
    if not isinstance(firsts, dict):
        firsts = {}
        data["first_observations"] = firsts

    previous = firsts.get(session_id)
    # isinstance, not `is None`: a corrupted non-string value would otherwise
    # compare unequal by type and register as a spurious veto, setting the
    # cooldown and wiping the accumulated window. Same guard shape as
    # record() and mark_nudged() use for their own malformed sub-dicts.
    if not isinstance(previous, str):
        firsts[session_id] = level
        _save(data)
        return False

    if previous == level:
        return False

    # Veto: reset the window so post-override evidence starts fresh, and hold
    # off write-back for the cooldown.
    data["veto_cooldown_remaining"] = max(int(cfg.effort.veto_cooldown_sessions), 0)
    data["window"] = []
    _save(data)
    log.info("effort veto: session %s moved %s → %s", session_id, previous, level)
    return True


def decrement_cooldown() -> None:
    """Tick the veto cooldown down by one session."""
    data = _load()
    remaining = int(data.get("veto_cooldown_remaining", 0))
    if remaining <= 0:
        return
    data["veto_cooldown_remaining"] = remaining - 1
    _save(data)
```

In `src/skill_advisor/hook.py`'s `run()`, inside the existing `if cfg.effort.enabled and rec is not None:` block, after `obs_session, observed = effort.read_observed()`:

```python
            if observed and obs_session == session_id:
                try:
                    baseline.note_observation(session_id, observed, cfg)
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug("baseline observation failed: %s", exc, exc_info=True)
        try:
            baseline.record(session_id or "", rec.level)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("baseline record failed: %s", exc, exc_info=True)
```

Add `from . import baseline` to `hook.py:15`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_baseline.py tests/test_hook.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/baseline.py src/skill_advisor/hook.py tests/test_baseline.py
git commit -m "feat(baseline): user veto detection and write-back cooldown"
```

---

### Task 12: Session finalisation and the announcement

**Files:**
- Modify: `src/skill_advisor/hook.py` (`run_stop`, `run`)
- Modify: `src/skill_advisor/baseline.py`
- Test: `tests/test_baseline.py`, `tests/test_hook.py`

**Interfaces:**
- Consumes: Tasks 9–11.
- Produces:
  - `baseline.take_announcement() -> str | None` — returns a formatted one-line message once, then clears it.
  - `run_stop()` calls `finalise_session`, `decrement_cooldown`, `maybe_write`.
  - `run()` emits the announcement on the first prompt after a write.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_baseline.py`:

```python
def test_announcement_is_returned_once_then_cleared():
    _fill("low", 3)
    baseline.maybe_write(_cfg(), launch_level="xhigh")
    msg = baseline.take_announcement()
    assert "xhigh" in msg and "low" in msg
    assert "/effort xhigh" in msg
    assert baseline.take_announcement() is None


def test_no_announcement_without_a_write():
    assert baseline.take_announcement() is None
```

Append to `tests/test_hook.py`:

```python
def test_stop_finalises_session_and_may_write(monkeypatch, tmp_path):
    from skill_advisor import baseline

    calls = []
    monkeypatch.setattr(baseline, "finalise_session", lambda s: calls.append(("final", s)))
    monkeypatch.setattr(baseline, "decrement_cooldown", lambda: calls.append(("dec",)))
    monkeypatch.setattr(baseline, "maybe_write", lambda cfg, launch_level: calls.append(("write",)))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"session_id":"s1"}'))
    hook.run_stop()
    assert ("final", "s1") in calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_baseline.py -k announce -v`
Expected: FAIL with `AttributeError: module 'skill_advisor.baseline' has no attribute 'take_announcement'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/skill_advisor/baseline.py`:

```python
def take_announcement() -> str | None:
    """Formatted announcement for the most recent write, consumed once."""
    data = _load()
    ann = data.pop("announce", None)
    if not isinstance(ann, dict):
        return None
    _save(data)
    frm = ann.get("from") or "your previous default"
    to = ann.get("to")
    sessions = ann.get("sessions")
    if not to:
        return None
    return (
        f"skill-advisor moved your effort baseline {frm} → {to} "
        f"({sessions} sessions of consistent work). Run /effort {frm} to keep it there."
    )
```

In `src/skill_advisor/hook.py`'s `run_stop()`, after the telemetry block and before the lifecycle-enabled check (`hook.py:245`):

```python
    if cfg.effort.enabled:
        try:
            baseline.finalise_session(session_id)
            baseline.decrement_cooldown()
            baseline.maybe_write(cfg, launch_level=baseline.first_observation(session_id))
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("baseline finalise failed: %s", exc, exc_info=True)
```

In `run()`, prepend the announcement to any nudge so a single `systemMessage` carries both:

```python
    if cfg.effort.enabled:
        try:
            announcement = baseline.take_announcement()
        except Exception:  # pragma: no cover - defensive
            announcement = None
        if announcement:
            nudge = f"{announcement}\n{nudge}" if nudge else announcement
```

Place this immediately before the `if result is None or not result.picks:` branch, and make the no-picks branch still emit when an announcement exists.

**This second emission site MUST route through `_emit_nudged()`, never raw `_emit()`.**
Task 8 moved nudge-slot consumption inside `_emit_nudged` precisely so a second
emission point stays correct for free. Calling `_emit` directly here would either
re-nudge the same pair forever (slot never consumed) or, if you re-added a
`mark_nudged` call by hand, reintroduce the consume-without-emitting bug Task 8
fixed. Nothing structurally enforces this — it is a convention, so honour it.

```python
    if result is None or not result.picks:
        if nudge:
            try:
                _emit_nudged(
                    "",
                    nudge=nudge,
                    session_id=session_id,
                    observed=observed,
                    level=rec.level if rec else None,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("emit failed: %s", exc, exc_info=True)
        log.debug("no picks for prompt (%.2fs)", duration)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -v`
Expected: PASS — full suite, 261 pre-existing plus the new tests.

- [ ] **Step 5: Commit**

```bash
git add src/skill_advisor/baseline.py src/skill_advisor/hook.py tests/test_baseline.py tests/test_hook.py
git commit -m "feat(baseline): finalise sessions on Stop and announce baseline moves"
```

---

### Task 13: README

**Files:**
- Modify: `README.md`

The README is the project's primary documentation (1498 lines) and is structured around a numbered table of contents. Every section below must be updated or the docs will contradict the code.

- [ ] **Step 1: Add the feature to Highlights and the ToC**

In the `## Highlights` bullet list (`README.md:30-36`), add after the *"Lifecycle aware"* bullet:

```markdown
- **Effort aware** — recommends a reasoning-effort level per prompt, shows it in a status line,
  nudges when it disagrees with your live setting, and slowly tunes your launch default behind
  a user veto. Adds no new subprocess.
```

In the `## Table of contents` (`README.md:42-60`), insert a new entry after `11. Lifecycle mode` and renumber the following entries:

```markdown
12. [Effort signalling](#effort-signalling)
```

- [ ] **Step 2: Add the Effort signalling section**

Insert a full `## Effort signalling` section after `## Lifecycle mode` ends and before `## Telemetry and usage reports`. It must cover:

- What it does, in three sentences, and that it is **off by default** (`[effort] enabled = false`).
- The precedence chain from the spec, verbatim, so users understand why `--settings` wins.
- **The observed/recommended enum asymmetry table** — copy the table from the spec's Finding 7. Users will otherwise report "why does it never recommend `max`" as a bug.
- The status line: what it renders, that it needs `jq`, and that it writes the sensor file.
- The nudge: what a `systemMessage` looks like, and that it is rate-limited per (observed, recommended) pair.
- Write-back: the N-consecutive-sessions rule, the announcement, and — prominently — **the veto**, including why `/effort` alone is not enough to undo a write.
- That **ultracode is never persisted**, and why (Claude Code treats it as session-only).
- A troubleshooting entry: "the status line shows nothing" → check `jq`, check `skill-advisor doctor`, check `effort.enabled`.

- [ ] **Step 3: Update the Configuration reference**

In `## Configuration reference` (`README.md:554-626`), add the `[effort]` block to the representative `config.toml`, with the same comments as `install.py::_default_config_text()`. **The two must not drift** — a reader who copies from the README must get a file that parses identically.

- [ ] **Step 4: Update Architecture, File layout, and CLI reference**

- `### Data flow` (`README.md:704-720`): add steps for writing `effort.json` and emitting `systemMessage`.
- `### Catalog storage` (`README.md:768-774`) and `## File layout` (`README.md:1442-1461`): add `effort.json`, `observed-effort.json`, `baseline.json` under `~/.cache/skill-advisor/`, and `statusline.sh` under `~/.config/skill-advisor/`.
- `## File layout`'s module list (`README.md:1401-1422`): add `effort.py`, `baseline.py`, `statusline.py` with one-line descriptions matching their docstrings.
- `### skill-advisor doctor` (`README.md:491-505`): update the sample output to include the `jq`, `statusline`, and `effort state` lines added in Task 7.
- `## Uninstalling` (`README.md:1465-1476`): note that `uninstall` also removes `effort.json`, `observed-effort.json`, `baseline.json`, and `statusline.sh`.

- [ ] **Step 5: Verify and commit**

```bash
# The config block in the README must match the installer's default byte-for-byte
# in its [effort] section. Extract both and diff them.
uv run python -c "
from skill_advisor.install import _default_config_text
t = _default_config_text()
assert '[effort]' in t, 'installer default config is missing the [effort] section'
print('installer config OK')
"
grep -q '^\[effort\]' README.md && echo "README documents [effort]"
git add README.md
git commit -m "docs: document effort signalling in the README"
```

---

### Task 14: INSTALL.md and examples

**Files:**
- Modify: `INSTALL.md`, `examples/config.toml`, `examples/claudeskill-settings.json`
- Modify: `src/skill_advisor/install.py:151-260` (`_default_config_text`)
- Modify: `src/skill_advisor/cli.py` (`_cmd_uninstall`)

- [ ] **Step 1: Add `[effort]` to the installer's default config**

Append to `_default_config_text()` in `src/skill_advisor/install.py`, before the closing `"""`:

```toml
[effort]
# Recommend a reasoning-effort level per prompt, show it in the status line, and
# nudge when it disagrees with your live setting. Off by default.
#
# Requires `jq` for the status line. The advisor CANNOT set effort directly —
# Claude Code keeps it in AppState, which hooks cannot write. It recommends,
# displays, and (optionally) tunes your launch-time default.
enabled = false

# Register a `statusLine` command in claudeskill-settings.json.
statusline = true

# Emit a one-line systemMessage when the recommendation disagrees with reality.
nudge = true

# Occasionally rewrite `effortLevel` in claudeskill-settings.json so your launch
# default converges on how you actually work.
write_back = true

# Consecutive sessions of consistent disagreement before a write happens.
write_back_after_sessions = 5

# Sessions to suppress write-back for after you manually run /effort.
# This is what makes the announcement's "undo" real: --settings outranks your
# own settings.json, so without a veto the advisor would silently win again.
veto_cooldown_sessions = 10

# Recommend `ultracode` when the parallelization detector says the tasks split.
# Never persisted — Claude Code treats ultracode as session-only by design.
ultracode_nudge = true
```

- [ ] **Step 2: Sync `examples/config.toml`**

Copy the same `[effort]` block into `examples/config.toml` so the committed example matches what `install` writes.

- [ ] **Step 3: Update `examples/claudeskill-settings.json`**

Add the `statusLine` key alongside the existing hook entries, using a placeholder path with a comment in the surrounding README prose that the real path is absolute and written by `install`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/home/you/.config/skill-advisor/statusline.sh"
  }
}
```

- [ ] **Step 4: Update INSTALL.md**

Add a short "Optional: effort signalling" section to `INSTALL.md` covering: install `jq`, set `enabled = true` under `[effort]`, re-run `skill-advisor install` so the `statusLine` key lands in the settings file, restart `claudeskill`, verify with `skill-advisor doctor`. Note explicitly that a **re-run of `install` is required** — flipping the toggle alone does not register the status line.

- [ ] **Step 5: Extend uninstall and commit**

In `_cmd_uninstall` in `src/skill_advisor/cli.py`, add the new artifacts to the removal list alongside the existing cache files:

```python
        paths.effort_file(),
        paths.observed_effort_file(),
        paths.baseline_file(),
        paths.statusline_script(),
```

```bash
uv run pytest tests/test_install.py -v
git add INSTALL.md examples/config.toml examples/claudeskill-settings.json \
        src/skill_advisor/install.py src/skill_advisor/cli.py
git commit -m "docs: sync installer defaults, examples, and INSTALL.md for [effort]"
```

---

### Task 15: Version bump and final verification

**Files:**
- Modify: `pyproject.toml`, `src/skill_advisor/__init__.py`

- [ ] **Step 1: Bump the version**

Set `version = "0.3.0"` in `pyproject.toml` and match `__version__` in `src/skill_advisor/__init__.py`. Minor bump: additive feature, no breaking change to existing config.

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS, zero failures, zero errors.

- [ ] **Step 3: Verify the off-by-default invariant**

This is the single most important regression check — an existing install must be byte-identically unaffected until the user opts in.

```bash
uv run python - <<'PY'
import json, tempfile, os, pathlib
tmp = tempfile.mkdtemp()
os.environ["SKILL_ADVISOR_CONFIG_HOME"] = tmp + "/config"
os.environ["SKILL_ADVISOR_CACHE_HOME"] = tmp + "/cache"
from skill_advisor import config, install, paths
cfg = config.load(pathlib.Path(tmp) / "missing.toml")
assert cfg.effort.enabled is False, "effort must default to disabled"
install.render_settings()
data = json.loads(paths.settings_file().read_text())
assert "statusLine" not in data, "status line must not register when disabled"
assert not paths.effort_file().exists(), "no effort state when disabled"
print("off-by-default invariant holds")
PY
```

- [ ] **Step 4: Manual smoke test**

```bash
uv tool install --reinstall .
skill-advisor doctor
echo '{"prompt":"refactor the auth middleware and migrate the session schema","session_id":"smoke"}' \
  | skill-advisor hook | jq .
# With [effort] enabled, expect hookSpecificOutput plus (once the status line has
# run at least once in a real session) a systemMessage on disagreement.
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/skill_advisor/__init__.py
git commit -m "chore: bump to 0.3.0 for effort signalling"
```

---

## Deferred (conscious choices, not oversights)

- **No `skill-advisor effort` CLI verb.** The spec does not call for one; `doctor` plus the cache files cover debugging. Add it if the feature proves hard to inspect in practice.
- **No `min_level` / `max_level` band.** Rejected during design as redundant with sticky write-back plus veto.
- **Latency is untouched.** The p50 10.2 s / p95 24.8 s floor comes from `use_judge = true` and the parallelization detector; this plan adds no round-trips but does not remove any either. Worth its own investigation.
