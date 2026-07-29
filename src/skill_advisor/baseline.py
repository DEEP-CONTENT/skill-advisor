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
from collections import Counter

from . import effort, paths

log = logging.getLogger(__name__)

_MIN_RECOMMENDATIONS = 3
_WINDOW_CAP = 30


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


def was_nudged(session_id: str | None, observed: str | None, recommended: str) -> bool:
    """Read-only counterpart to `mark_nudged`.

    True when this (observed, recommended) pair has already been nudged this
    session. Lets a caller decide whether a message is worth building at all,
    without consuming the slot — consumption must wait until the caller knows
    the message actually reached the user (see `mark_nudged`).
    """
    if not session_id:
        return False
    key = f"{observed}>{recommended}"
    ledger = _load().get("nudged", {})
    if not isinstance(ledger, dict):
        return False
    seen = ledger.get(session_id, [])
    if not isinstance(seen, list):
        return False
    return key in seen


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


def record(session_id: str, level: str) -> None:
    """Append one recommendation to this session's tally."""
    if not session_id or level not in effort.RECOMMENDABLE:
        return
    data = _load()
    tallies = data.setdefault("tallies", {})
    if not isinstance(tallies, dict):
        tallies = {}
        data["tallies"] = tallies
    tallies.setdefault(session_id, []).append(level)
    _save(data)


def finalise_session(session_id: str) -> str | None:
    """Collapse a session's tally to its modal level and append to the window."""
    data = _load()
    tallies = data.get("tallies", {})
    if not isinstance(tallies, dict):
        tallies = {}
    levels = tallies.pop(session_id, [])
    if len(levels) < _MIN_RECOMMENDATIONS:
        data["tallies"] = tallies
        _save(data)
        return None

    modal = Counter(levels).most_common(1)[0][0]
    persistable = effort.to_persistable(modal)
    if persistable is None:
        data["tallies"] = tallies
        _save(data)
        return None

    window = list(data.get("window", []))
    window.append(persistable)
    data["window"] = window[-_WINDOW_CAP:]
    data["tallies"] = tallies
    _save(data)
    return persistable


def window() -> list[str]:
    """Rolling window of session modals, oldest first."""
    data = _load()
    win = data.get("window", [])
    return [w for w in win if isinstance(w, str)] if isinstance(win, list) else []


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
    """Merge `effortLevel` into the settings file atomically. False on any failure.

    Race note: this is a read-modify-write on `claudeskill-settings.json`, and
    `install.render_settings()` does its own independent read-modify-write of the
    same file. If the two interleave, the later writer silently discards the
    earlier writer's key — e.g. this function can report success and reset
    baseline.json's window/history believing `effortLevel` landed, while a
    concurrent `render_settings()` clobbers it, leaving baseline.json's
    provenance permanently out of sync with the file on disk. No lock is taken:
    `render_settings()` only runs during an explicit `install`, `maybe_write`
    only fires from a live-session hook, and the overlap requires running
    install at the exact moment a Stop hook fires — judged an acceptable
    exposure for a single-user local tool.
    """
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
