"""Opt-in structured event log for the advisor.

Writes one JSON object per `UserPromptSubmit` firing to
`~/.cache/skill-advisor/advisor.events.jsonl`, consumable by `skill-advisor report`.

Design invariants:
- Opt-in via `config.telemetry.events_enabled`. When False, no file is created.
- Prompts are never stored in plaintext — only `sha256(salt + prompt)[:16]`.
- Session ids are always hashed with the same salt. No raw session ids on disk.
- Single-line atomic append: one `write()` of a JSON blob + `\n`, serialised
  across threads and processes via an advisory file lock (`fcntl.flock` on
  POSIX, `msvcrt.locking` on Windows) so concurrent writers never interleave.
- Telemetry failures are the caller's problem — this module raises on broken
  state; the hook wraps it in try/except so a telemetry hiccup never fails the
  user's prompt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from . import paths
from .config import TelemetryConfig

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Atomic append
# ---------------------------------------------------------------------------
# POSIX guarantees that a single write() under O_APPEND is atomic up to
# PIPE_BUF, so concurrent appenders never interleave. Windows text-append mode
# offers no such guarantee — the CRT seeks-then-writes, so concurrent threads
# and processes lose lines. Each hook firing is its own process, so we need
# both an in-process lock and a cross-process advisory file lock.

_append_lock = threading.Lock()

if sys.platform == "win32":
    import msvcrt

    def _lock_region(fh) -> bool:
        # Lock a fixed 1-byte region at offset 0 so every writer contends on the
        # same range, independent of the file's current length.
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            return True
        except OSError:
            return False

    def _unlock_region(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_region(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            return True
        except OSError:
            return False

    def _unlock_region(fh) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _append_line(line: str) -> None:
    """Append a single line atomically across threads and processes."""
    paths.ensure_dirs()
    data = line.encode("utf-8")
    target = paths.events_file()
    with _append_lock:
        with open(target, "ab") as fh:
            locked = _lock_region(fh)
            try:
                fh.seek(0, os.SEEK_END)
                fh.write(data)
                fh.flush()
            finally:
                if locked:
                    _unlock_region(fh)


@dataclass(frozen=True)
class PickRecord:
    rank: int
    name: str
    kind: str
    score: float | None  # None when the judge produced the pick


def _load_or_create_salt(cfg: TelemetryConfig) -> str:
    """Return the salt used to hash prompts and session ids.

    Precedence: explicit config value wins; else read from disk; else generate,
    write (mode 0600), and return. Concurrent first-writes race with
    last-write-wins semantics — acceptable because the salt is not cryptographic.
    """
    if cfg.prompt_hash_salt:
        return cfg.prompt_hash_salt
    salt_path = paths.telemetry_salt_file()
    if salt_path.is_file():
        return salt_path.read_text(encoding="utf-8").strip()
    paths.ensure_dirs()
    salt = secrets.token_hex(16)
    salt_path.write_text(salt, encoding="utf-8")
    try:
        os.chmod(salt_path, 0o600)
    except OSError:
        pass
    return salt


def _hash(value: str, salt: str) -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()[:16]


def record(
    *,
    prompt: str,
    session_id: str | None,
    picks: Iterable,   # list[matcher.ResolvedPick]; loose-typed to avoid circular import
    phase: str = "none",
    phase_source: str = "user",
    judge_used: bool = False,
    judge_failure: str | None = None,
    triage_skipped: bool = False,
    duration_ms: int = 0,
    config: TelemetryConfig,
) -> None:
    """Append a single event line. No-op when telemetry is disabled."""
    if not config.events_enabled:
        return

    salt = _load_or_create_salt(config)

    pick_records: list[dict] = []
    for rank, pick in enumerate(picks, start=1):
        entry = pick.entry
        score: float | None = None
        reason = str(getattr(pick, "reason", ""))
        m = re.search(r"\(([0-9]*\.?[0-9]+)\)", reason)
        if m:
            try:
                score = float(m.group(1))
            except ValueError:
                score = None
        pick_records.append({
            "rank": rank,
            "name": entry.name,
            "kind": entry.kind,
            "score": score,
        })

    event = {
        "schema": SCHEMA_VERSION,
        "kind": "prompt",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_sha256": _hash(session_id, salt) if session_id else None,
        "prompt_sha256": _hash(prompt, salt),
        "prompt_words": len(prompt.split()),
        "triage_skipped": bool(triage_skipped),
        "duration_ms": int(duration_ms),
        "phase": phase or "none",
        "phase_source": phase_source,
        "judge_used": bool(judge_used),
        "judge_failure": str(judge_failure) if judge_failure else None,
        "picks": pick_records,
    }

    _append_line(json.dumps(event, ensure_ascii=False) + "\n")


def record_stop(
    *,
    session_id: str | None,
    tools: Iterable[str],
    subagents: Iterable[str],
    skills: Iterable[str] = (),
    tool_spans: Iterable[tuple[str, int]] = (),
    span_anchor: str = "previous_tool",
    config: TelemetryConfig,
) -> None:
    """Append a `kind=stop` event with the turn's tool sequence.

    Paired with prior `kind=prompt` events in the same session by the report
    layer to compute ingestion rate (how often the model actually invoked the
    Skill tool after a suggestion was injected).

    No-op when telemetry is disabled. Single atomic O_APPEND write.
    """
    if not config.events_enabled:
        return

    salt = _load_or_create_salt(config)
    event = {
        "schema": SCHEMA_VERSION,
        "kind": "stop",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_sha256": _hash(session_id, salt) if session_id else None,
        "tools": [str(t) for t in tools],
        "subagents": [str(s) for s in subagents],
        "skills": [str(s) for s in skills],
    }

    spans = [[str(name), int(ms)] for name, ms in tool_spans]
    if spans:
        event["tool_spans"] = spans
        # "previous_tool": each span measures the interval since the PREVIOUS
        # tool call finished, so the turn's first tool carries no span and a
        # one-tool turn writes no `tool_spans` key at all. Constant today, but
        # written anyway so a later anchor change (e.g. to prompt submission)
        # is distinguishable in historical rows instead of silently redefining
        # what a span means.
        event["span_anchor"] = span_anchor

    _append_line(json.dumps(event, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Reading / purging
# ---------------------------------------------------------------------------


def _parse_ts(text: str) -> datetime | None:
    try:
        # strptime with Z suffix; fromisoformat doesn't accept "Z" in 3.10.
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def iter_events(
    *,
    cutoff: datetime | None = None,
    path: Path | None = None,
) -> Iterator[dict]:
    """Yield parsed events from the JSONL log.

    Malformed lines and wrong-schema lines are skipped silently (logged at DEBUG).
    Events older than `cutoff` are filtered out.
    """
    target = path or paths.events_file()
    if not target.is_file():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("telemetry: skipping malformed jsonl line")
            continue
        if not isinstance(event, dict):
            continue
        if event.get("schema") != SCHEMA_VERSION:
            continue
        ts = _parse_ts(event.get("ts", ""))
        if cutoff is not None:
            if ts is None or ts < cutoff:
                continue
        yield event


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([dDhHwW])\s*$")


def parse_duration(text: str) -> timedelta:
    """Parse '7d', '24h', '2w' into a timedelta. Raises ValueError on junk."""
    m = _DURATION_RE.match(text or "")
    if not m:
        raise ValueError(f"invalid duration {text!r}; expected e.g. 7d, 24h, 2w")
    amount = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    raise ValueError(f"unsupported duration unit: {unit}")


def purge_older_than(duration: timedelta, *, path: Path | None = None) -> int:
    """Rewrite the events file, keeping only lines newer than now - duration.

    Returns the number of events removed. No-op (returns 0) if the file is missing.
    """
    target = path or paths.events_file()
    if not target.is_file():
        return 0
    cutoff = datetime.now(timezone.utc) - duration

    kept_lines: list[str] = []
    removed = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
            ts = _parse_ts(event.get("ts", ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            # Preserve malformed lines rather than silently deleting user data.
            kept_lines.append(raw)
            continue
        if ts is not None and ts >= cutoff:
            kept_lines.append(raw)
        else:
            removed += 1

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
    os.replace(tmp, target)
    return removed
