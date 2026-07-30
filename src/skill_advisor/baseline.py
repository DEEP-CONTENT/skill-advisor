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
_TALLY_CAP = 50


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
    """Append one recommendation to this session's tally.

    Enforces `_TALLY_CAP` HERE, not in `finalise_session` (fix round 3):
    `record()` is the only function that ever creates a tally key, so a
    session that never reaches `_MIN_RECOMMENDATIONS` never takes
    `finalise_session`'s success path and its key was never counted against
    the cap there — a user with many short 1-2 prompt sessions grew the file
    forever. Capping here bounds growth unconditionally, regardless of
    whether any session ever finalises.

    Append first, then cap, and never evict the session just recorded for:
    a brand-new key is the most recently inserted (Python 3.7+ dict order),
    so oldest-first eviction alone already skips it — but `session_id` is
    also explicitly excluded from the eviction candidates below, so this
    holds even against a pre-existing file that already has more than
    `_TALLY_CAP` keys (e.g. from before this cap existed) where `session_id`
    happens to be the oldest one.
    """
    if not session_id or level not in effort.RECOMMENDABLE:
        return
    data = _load()
    tallies = data.setdefault("tallies", {})
    if not isinstance(tallies, dict):
        tallies = {}
        data["tallies"] = tallies
    tallies.setdefault(session_id, []).append(level)

    if len(tallies) > _TALLY_CAP:
        overflow = len(tallies) - _TALLY_CAP
        stale = [k for k in tallies if k != session_id][:overflow]
        for k in stale:
            del tallies[k]
        assert session_id in tallies, "record() must never evict its own session"

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

    # `tallies` growth is bounded in `record()` now (fix round 3), not here:
    # record() is the only function that ever creates a tally key, so it's
    # the only place that can bound growth unconditionally — this function's
    # own copy of that logic (fix round 2) only ran on the success path
    # above, which a session that never reaches _MIN_RECOMMENDATIONS never
    # takes, so it never actually bounded the sessions that needed it most.
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
    if target_level == current_written_level():
        # F2: launch_level is the session's FIRST observation and never
        # changes after a write lands, so comparing against it alone lets a
        # sustained window re-trigger the identical write (and re-announce)
        # on every later turn. current_written_level() reads what is
        # actually on disk right now, which a write updates — the two
        # guards serve different purposes and both must hold.
        return None  # already written; avoid rewrite + re-announce

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
    # off write-back for the cooldown. `veto_cooldown_charged` is the set of
    # sessions that have already consumed a unit of THIS cooldown, seeded with
    # the arming session so it never charges its own cooldown (see
    # `decrement_cooldown`). Re-arming replaces the list outright: a fresh veto
    # starts a fresh cooldown, and a session that charged the previous one must
    # be able to charge this one too.
    data["veto_cooldown_remaining"] = max(int(cfg.effort.veto_cooldown_sessions), 0)
    data["veto_cooldown_charged"] = [session_id]
    data.pop("veto_cooldown_last_session", None)  # superseded; see decrement_cooldown
    data["window"] = []
    _save(data)
    log.info("effort veto: session %s moved %s → %s", session_id, previous, level)
    return True


def _charged_sessions(data: dict) -> list[str]:
    """Sessions that have already consumed a unit of the current cooldown.

    Falls back to the legacy single-id key so a cooldown armed by an older
    version keeps exempting its arming session across the upgrade. A malformed
    value degrades to "nobody has charged yet" rather than raising — this runs
    on the Stop hook path, which must never fail.

    The legacy key is dropped only when a charge is actually recorded, not on
    the no-op paths, so a freshly upgraded file whose only Stop events come
    from the arming session keeps the bare legacy key until a second session
    appears or the next veto re-arms. Harmless: this fallback still reads it,
    and the two keys are never written to disk in a conflicting state.
    """
    charged = data.get("veto_cooldown_charged")
    if isinstance(charged, list):
        return [s for s in charged if isinstance(s, str)]
    legacy = data.get("veto_cooldown_last_session")
    return [legacy] if isinstance(legacy, str) else []


def decrement_cooldown(session_id: str) -> None:
    """Tick the veto cooldown down by one DISTINCT session, not one turn.

    The Stop hook fires once per turn, so naively decrementing on every call
    drains an N-session cooldown in N turns of a single session — with
    veto_cooldown_sessions=1, the very Stop of the turn that armed the
    cooldown would drain it to zero, giving no protection at all.

    Tracking only the *last* session id fixed that but left a second hole: it
    counted session CHANGES rather than distinct sessions, so two sessions
    interleaving (`s1,s2,s1,s2,...`) charged on every single Stop, because the
    previous id always differed from the current one. Measured: 20 alternating
    turns across 2 distinct sessions drained a 10-session cooldown to zero.
    Concurrent Claude Code sessions across repos produce exactly that pattern,
    so the documented "suppressed for N sessions" guarantee did not hold.

    Hence a set: each session id charges at most once per cooldown, in any
    order, however many turns it takes. The list cannot outgrow the cooldown it
    belongs to — once `remaining` reaches 0 this returns before recording
    anything — and `note_observation` replaces it wholesale on re-arm.
    """
    if not session_id:
        return
    data = _load()
    remaining = int(data.get("veto_cooldown_remaining", 0))
    if remaining <= 0:
        return
    charged = _charged_sessions(data)
    if session_id in charged:
        return  # already charged this cooldown (or is the arming session)
    charged.append(session_id)
    data["veto_cooldown_remaining"] = remaining - 1
    data["veto_cooldown_charged"] = charged
    data.pop("veto_cooldown_last_session", None)  # migrated into the list above
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
