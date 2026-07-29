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
    if not isinstance(previous, str):
        # None (never observed) and any malformed non-string value (corrupt
        # baseline.json) both degrade to "no first observation yet" — never
        # let a type mismatch alone read as a genuine veto.
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
