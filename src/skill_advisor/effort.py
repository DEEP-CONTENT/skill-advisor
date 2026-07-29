"""Effort-level vocabulary, ordering, and the classifier.

The observed and recommended level sets are deliberately NOT the same:

    observed    (from Claude Code's statusLine payload)  low medium high xhigh max
    recommended (from this classifier)                   low medium high xhigh ultracode

`max` is never recommended — the classifier has no basis for distinguishing it
from `xhigh`. `ultracode` is never observed — the harness surfaces it as `xhigh`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from . import paths

log = logging.getLogger(__name__)

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
