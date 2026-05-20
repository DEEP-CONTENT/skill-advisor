"""Cheap-prompt detector — skip the pipeline on acknowledgements, slash commands, etc."""
from __future__ import annotations

import re

from .config import Config, load as load_config

_ACK_PATTERN = re.compile(
    r"^\s*(yes|yeah|yep|no|nope|ok|okay|go|continue|thanks?|thx|cool|nice|done|perfect|exit|stop|quit|clear|help)[\s!.?]*$",
    re.IGNORECASE,
)
_USE_SKILL_PATTERN = re.compile(
    r"\b(use|invoke|run|call)\s+(the\s+)?(\S+\s+)?(skill|agent|subagent|command)\b",
    re.IGNORECASE,
)


def should_skip(prompt: str, config: Config | None = None) -> bool:
    cfg = config or load_config()
    text = (prompt or "").strip()

    if not text:
        return True

    # User is invoking a slash command directly — Claude Code already routes it.
    if text.startswith("/"):
        return True

    # Short acknowledgements / single-word chat turns.
    if _ACK_PATTERN.match(text):
        return True

    # Explicit "use the X skill" — don't override the user's choice.
    if _USE_SKILL_PATTERN.search(text):
        return True

    # Short prompts with no technical signal.
    word_count = len(text.split())
    if word_count < cfg.triage.skip_if_shorter_than and not _has_technical_signal(text):
        return True

    for pattern in cfg.triage.extra_skip_patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            continue

    return False


_TECH_HINTS = re.compile(
    r"\b(bug|fix|refactor|implement|review|test|build|deploy|debug|error|crash|"
    r"api|schema|database|migration|endpoint|component|auth|optimi[sz]e|security|"
    r"plan|architect|design|brainstorm|research|git|commit|pr\b|pull request)\b",
    re.IGNORECASE,
)


def _has_technical_signal(text: str) -> bool:
    return bool(_TECH_HINTS.search(text))
