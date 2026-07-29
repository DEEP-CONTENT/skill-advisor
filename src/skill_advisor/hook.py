"""UserPromptSubmit hook entry point.

Never fails the user's prompt submission. On any error or budget overrun,
exits 0 silently and logs the reason to ~/.cache/skill-advisor/advisor.log.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
import time
from typing import Any

from . import baseline, effort, inject, lifecycle, matcher, paths, telemetry, triage
from .config import load as load_config


class _BudgetExceeded(Exception):
    pass


def _alarm_handler(signum, frame):  # pragma: no cover - signal path
    raise _BudgetExceeded()


def _setup_logging() -> None:
    paths.ensure_dirs()
    logging.basicConfig(
        filename=str(paths.log_file()),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


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


def _emit_nudged(
    text: str,
    *,
    nudge: str | None,
    session_id: str | None,
    observed: str | None,
    level: str | None,
) -> None:
    """Emit the envelope, then consume the nudge rate-limit slot only once the
    emit actually happened.

    Consumption is tied to this single emission point rather than to any one
    caller's branch, so a future emission site that also carries a pending
    nudge (e.g. a no-picks-but-announcement path) stays correct by calling
    this same helper instead of re-deriving "did this reach the user" logic.
    If `_emit` raises, the caller's own exception handling takes over and the
    slot is never touched — the message never reached the user.
    """
    _emit(text, system_message=nudge)
    if nudge:
        try:
            baseline.mark_nudged(session_id, observed, level)
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger("skill_advisor.hook").debug(
                "nudge mark failed: %s", exc, exc_info=True
            )


def _extract_todo_titles(tool_input: dict) -> list[str]:
    """Pull the `content` string from each todo in a TodoWrite tool_input.

    Returns [] when the payload is malformed; caller decides what that means.
    """
    todos = tool_input.get("todos")
    if not isinstance(todos, list):
        return []
    out: list[str] = []
    for item in todos:
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                out.append(content.strip())
    return out


def run() -> int:
    _setup_logging()
    log = logging.getLogger("skill_advisor.hook")

    started = time.monotonic()
    event = _read_input()
    prompt = str(event.get("prompt") or "").strip()
    session_id = str(event.get("session_id") or "").strip() or None
    if not prompt:
        return 0

    cfg = load_config()
    budget = max(int(cfg.matcher.budget_seconds + 0.5), 1)
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(budget)

    try:
        result = matcher.pick(prompt, cfg, session_id=session_id)
    except _BudgetExceeded:
        log.info("budget exceeded after %.2fs; falling back silent", time.monotonic() - started)
        return 0
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("hook exception: %s", exc, exc_info=True)
        return 0
    finally:
        signal.alarm(0)

    duration = time.monotonic() - started
    phase = result.state.phase if (result and result.state) else "none"
    picks = result.picks if result else []

    rec = result.effort if result else None
    nudge = None
    observed = None
    if cfg.effort.enabled and rec is not None:
        try:
            effort.write_recommendation(rec, session_id=session_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("effort write failed: %s", exc, exc_info=True)
        try:
            obs_session, observed = effort.read_observed()
            if cfg.effort.nudge and obs_session == session_id:
                candidate = _nudge_message(observed, rec)
                # Check only — do NOT consume the slot here. It must not
                # be spent until we know the message actually reached the
                # user (see `_emit_nudged`); otherwise a turn that never
                # emits (empty picks) or fails mid-emit silently burns the
                # one shot this session gets for this (observed, level)
                # pair, and the user is never told.
                if candidate and not baseline.was_nudged(session_id, observed, rec.level):
                    nudge = candidate
            if observed and obs_session == session_id:
                try:
                    baseline.note_observation(session_id, observed, cfg)
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug("baseline observation failed: %s", exc, exc_info=True)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("nudge computation failed: %s", exc, exc_info=True)
            nudge = None
            observed = None
        try:
            baseline.record(session_id or "", rec.level)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("baseline record failed: %s", exc, exc_info=True)

    # A baseline write on a previous session's Stop leaves a one-shot
    # announcement pending. Consume it here, on the first prompt after the
    # write, and prepend it to any pending nudge so a single systemMessage
    # carries both. `announcement` (not the merged `nudge`) is what gates the
    # no-picks branch below — an ordinary effort nudge with no picks must
    # stay silent (there's nothing to attach it to), but an announcement is
    # its own message and must get through regardless of picks.
    announcement = None
    if cfg.effort.enabled:
        try:
            announcement = baseline.take_announcement()
        except Exception:  # pragma: no cover - defensive
            announcement = None
        if announcement:
            nudge = f"{announcement}\n{nudge}" if nudge else announcement

    if result is None or not result.picks:
        if announcement:
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
    else:
        try:
            _emit_nudged(
                inject.format(result),
                nudge=nudge,
                session_id=session_id,
                observed=observed,
                level=rec.level if rec else None,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("emit failed: %s", exc, exc_info=True)
            return 0
        log.info(
            "picks=%s phase=%s duration=%.2fs",
            [p.entry.name for p in result.picks],
            phase,
            duration,
        )

    # Telemetry: opt-in structured log. Never fails the hook.
    if cfg.telemetry.events_enabled:
        try:
            telemetry.record(
                prompt=prompt,
                session_id=session_id,
                picks=picks,
                phase=phase,
                phase_source="user",
                judge_used=cfg.matcher.use_judge,
                triage_skipped=triage.should_skip(prompt, cfg),
                duration_ms=int(duration * 1000),
                config=cfg.telemetry,
            )
        except Exception as exc:
            log.debug("telemetry record failed: %s", exc, exc_info=True)
    return 0


def main() -> None:
    sys.exit(run())


# ---------------------------------------------------------------------------
# PostToolUse + Stop handlers — drive lifecycle auto-advance
# ---------------------------------------------------------------------------


# Double-advance guard window. If the lifecycle state was updated within this
# many seconds, the Stop handler defers — the user probably just sent a
# continuation prompt that already advanced the phase.
_STOP_ADVANCE_MIN_GAP_SECONDS = 1.0


def run_posttooluse() -> int:
    """Record tool usage into per-turn state. Never fails."""
    _setup_logging()
    log = logging.getLogger("skill_advisor.posttooluse")

    event = _read_input()
    session_id = str(event.get("session_id") or "").strip()
    tool_name = str(event.get("tool_name") or "").strip()
    if not session_id or not tool_name:
        return 0

    subagent_type = None
    tool_input = event.get("tool_input") or {}
    if isinstance(tool_input, dict):
        raw_subagent = tool_input.get("subagent_type")
        if raw_subagent is not None:
            subagent_type = str(raw_subagent).strip() or None

    try:
        lifecycle.record_tool(session_id, tool_name, subagent_type=subagent_type)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("posttooluse record failed: %s", exc, exc_info=True)

    # The Claude Code task tool family has evolved: pre-2026 builds emitted a
    # single batched `TodoWrite` (tool_input.todos = [{content,...}, ...]);
    # current builds emit per-call `TaskCreate` (tool_input.subject = "..."),
    # one call per task. Watch both — TodoWrite stays last-write-wins, while
    # TaskCreate appends incrementally into the same turn-state slot.
    if tool_name in ("TodoWrite", "TaskCreate") and isinstance(tool_input, dict):
        try:
            cfg = load_config()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("posttooluse config load failed: %s", exc, exc_info=True)
            return 0
        if not cfg.parallelization.enabled:
            return 0
        try:
            if tool_name == "TodoWrite":
                titles = _extract_todo_titles(tool_input)
                if titles:
                    lifecycle.record_todo_write(session_id, titles)
            else:  # TaskCreate
                subject = tool_input.get("subject")
                if isinstance(subject, str) and subject.strip():
                    lifecycle.append_todo_title(session_id, subject)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("posttooluse todo capture failed: %s", exc, exc_info=True)
    return 0


def run_stop() -> int:
    """Apply auto-advance rules based on the current turn's tool usage."""
    _setup_logging()
    log = logging.getLogger("skill_advisor.stop")

    event = _read_input()
    session_id = str(event.get("session_id") or "").strip()
    if not session_id:
        return 0

    # Whatever happens below, the turn file is per-turn — always clear it.
    try:
        turn = lifecycle.load_turn(session_id)
        lifecycle.delete_turn(session_id)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("stop turn cleanup failed: %s", exc, exc_info=True)
        return 0

    try:
        cfg = load_config()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("stop config load failed: %s", exc, exc_info=True)
        return 0

    # Effort baseline finalisation: tallies/window/write-back are session-level
    # bookkeeping, independent of lifecycle turn-tracking. A turn that used no
    # tools at all (turn is None — e.g. a plain conversational reply) still
    # deserves this pass, so it runs before the turn-is-None short-circuit
    # below. Wrapped defensively: run_stop must always return 0.
    if cfg.effort.enabled:
        try:
            baseline.finalise_session(session_id)
            baseline.decrement_cooldown(session_id)
            baseline.maybe_write(cfg, launch_level=baseline.first_observation(session_id))
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("baseline finalise failed: %s", exc, exc_info=True)

    if turn is None:
        return 0

    # Stop-event telemetry runs regardless of lifecycle config — it's how the
    # report correlates picks with downstream Skill invocations to compute
    # ingestion rate. Lifecycle auto-advance is a separate concern below.
    if cfg.telemetry.events_enabled:
        try:
            telemetry.record_stop(
                session_id=session_id,
                tools=turn.tool_names,
                subagents=turn.subagents_invoked,
                config=cfg.telemetry,
            )
        except Exception as exc:
            log.debug("stop telemetry record failed: %s", exc, exc_info=True)

    if not cfg.lifecycle.enabled or not cfg.lifecycle.auto_advance.enabled:
        return 0

    try:
        state = lifecycle.load(session_id)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("stop state load failed: %s", exc, exc_info=True)
        return 0

    if state is None or not state.is_active():
        return 0

    # Double-advance guard (decision 2). User's prompt may have just advanced
    # the state; ignore the Stop event if it arrives in the same breath.
    if time.time() - state.updated_at < _STOP_ADVANCE_MIN_GAP_SECONDS:
        log.info(
            "stop: auto-advance skipped (updated %.2fs ago < %.2fs)",
            time.time() - state.updated_at,
            _STOP_ADVANCE_MIN_GAP_SECONDS,
        )
        return 0

    advance_cfg = cfg.lifecycle.auto_advance
    phase = state.phase
    advanced = False

    try:
        if (
            phase == lifecycle.PLANNING
            and cfg.parallelization.enabled
            and turn.todo_write is not None
            and int(turn.todo_write.get("count") or 0) >= cfg.parallelization.min_tasks
        ):
            titles = list(turn.todo_write.get("titles") or [])
            lifecycle.enter_parallelization_check(
                state,
                titles,
                note=f"auto: TodoWrite with {len(titles)} tasks",
                source="auto",
            )
            advanced = True
        elif phase == lifecycle.PLANNING and advance_cfg.on_plan_subagent_done:
            if any(name in {"Plan", "writing-plans"} for name in turn.subagents_invoked):
                lifecycle.advance(
                    state,
                    note="auto: Plan subagent completed",
                    source="auto",
                    config=cfg.lifecycle,
                )
                advanced = True
        elif phase == lifecycle.IMPLEMENTATION and advance_cfg.on_edit_stop:
            # Decision 3: skip advance when no mutating tool ran this turn.
            if turn.has_mutating_tool():
                lifecycle.advance(
                    state,
                    note=f"auto: Stop after mutating tools ({','.join(sorted(set(turn.tool_names)))})",
                    source="auto",
                    config=cfg.lifecycle,
                )
                advanced = True
        # REVIEW/CORRECTION/PARALLELIZATION_CHECK/terminal phases: handled elsewhere.
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("stop: advance failed: %s", exc, exc_info=True)
        return 0

    log.info(
        "stop: phase=%s advanced=%s tools=%s subagents=%s",
        phase,
        advanced,
        turn.tool_names,
        turn.subagents_invoked,
    )
    return 0


if __name__ == "__main__":
    main()
