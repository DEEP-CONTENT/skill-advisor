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
    tallies.setdefault(session_id, []).append(level)
    _save(data)


def finalise_session(session_id: str) -> str | None:
    """Collapse a session's tally to its modal level and append to the window."""
    data = _load()
    tallies = data.get("tallies", {})
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
