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
