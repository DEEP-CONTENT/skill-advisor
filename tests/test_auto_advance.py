"""Tests for Stop/PostToolUse lifecycle auto-advance.

Covers:
- PostToolUse recording (tool name, subagent_type from Task tool).
- Stop handler applying phase-specific auto-advance rules.
- Time-window guard against double-advance with user prompts.
- Safe behavior under terminal phases, missing session, disabled toggle.
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from skill_advisor import hook as hook_mod
from skill_advisor import lifecycle, paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_handler(handler, payload: dict) -> int:
    """Pipe JSON into a handler's stdin. Returns the handler's exit code."""
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
         patch("sys.stdout", io.StringIO()):
        return handler()


def _enable_auto_advance(isolated_paths) -> None:
    cfg = isolated_paths["config_home"] / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "[lifecycle]\nenabled = true\n"
        "[lifecycle.auto_advance]\nenabled = true\n"
        "on_plan_subagent_done = true\non_edit_stop = true\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# TurnState + record_tool
# ---------------------------------------------------------------------------


def test_posttooluse_records_single_tool(isolated_paths):
    rc = _run_handler(hook_mod.run_posttooluse, {"session_id": "s1", "tool_name": "Edit"})
    assert rc == 0
    turn = lifecycle.load_turn("s1")
    assert turn is not None
    assert turn.tool_names == ["Edit"]
    assert turn.subagents_invoked == []


def test_posttooluse_accumulates_across_calls(isolated_paths):
    for tool in ("Read", "Edit", "Write"):
        _run_handler(hook_mod.run_posttooluse, {"session_id": "s1", "tool_name": tool})
    turn = lifecycle.load_turn("s1")
    assert turn.tool_names == ["Read", "Edit", "Write"]


def test_posttooluse_records_subagent_from_task_tool(isolated_paths):
    _run_handler(
        hook_mod.run_posttooluse,
        {"session_id": "s1", "tool_name": "Task", "tool_input": {"subagent_type": "Plan"}},
    )
    turn = lifecycle.load_turn("s1")
    assert turn.tool_names == ["Task"]
    assert turn.subagents_invoked == ["Plan"]


def test_posttooluse_ignores_subagent_on_non_task_tools(isolated_paths):
    # subagent_type on an Edit call (weird but plausible) should not leak into subagents_invoked.
    _run_handler(
        hook_mod.run_posttooluse,
        {"session_id": "s1", "tool_name": "Edit", "tool_input": {"subagent_type": "Plan"}},
    )
    turn = lifecycle.load_turn("s1")
    assert turn.subagents_invoked == []


def test_posttooluse_missing_session_id_noop(isolated_paths):
    _run_handler(hook_mod.run_posttooluse, {"tool_name": "Edit"})
    # No file written — session id missing.
    assert list(paths.sessions_dir().glob("*.turn.json")) == []


def test_posttooluse_missing_tool_name_noop(isolated_paths):
    _run_handler(hook_mod.run_posttooluse, {"session_id": "s1"})
    assert lifecycle.load_turn("s1") is None


# ---------------------------------------------------------------------------
# Stop handler: phase transitions
# ---------------------------------------------------------------------------


def _make_state_at_phase(session_id: str, phase: str, *, age_seconds: float = 10.0) -> lifecycle.LifecycleState:
    state = lifecycle.start(session_id, "build a rate limiter")
    # Advance to target phase by stepping through the machine.
    # start() lands at PLANNING.
    while state.phase != phase and state.phase in lifecycle.ACTIVE_PHASES:
        state = lifecycle.advance(state)
        if state.phase == phase:
            break
    # Backdate updated_at so the double-advance guard doesn't block tests.
    state.updated_at = time.time() - age_seconds
    lifecycle.save(state)
    # save() overwrites updated_at — restore the backdated value so guard doesn't trip.
    # Rewrite file directly with the old timestamp.
    data = json.loads(paths.session_file(session_id).read_text())
    data["updated_at"] = time.time() - age_seconds
    paths.session_file(session_id).write_text(json.dumps(data), encoding="utf-8")
    return lifecycle.load(session_id)


def test_stop_advances_planning_on_plan_subagent(isolated_paths):
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s1", lifecycle.PLANNING)

    # Model invoked Plan subagent this turn.
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Task"], subagents_invoked=["Plan"]))

    rc = _run_handler(hook_mod.run_stop, {"session_id": "s1"})
    assert rc == 0

    state = lifecycle.load("s1")
    assert state.phase == lifecycle.IMPLEMENTATION
    assert state.history[-1]["source"] == "auto"
    # Turn file was cleared.
    assert lifecycle.load_turn("s1") is None


def test_stop_advances_implementation_on_mutating_tools(isolated_paths):
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s1", lifecycle.IMPLEMENTATION)
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Read", "Edit", "Bash"]))

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    state = lifecycle.load("s1")
    assert state.phase == lifecycle.REVIEW
    assert state.history[-1]["source"] == "auto"


def test_stop_no_advance_on_readonly_tools_only(isolated_paths):
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s1", lifecycle.IMPLEMENTATION)
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Read", "Grep", "LS"]))

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    state = lifecycle.load("s1")
    assert state.phase == lifecycle.IMPLEMENTATION  # unchanged
    # Turn file cleared even when no advance happened.
    assert lifecycle.load_turn("s1") is None


def test_stop_no_advance_with_empty_tool_list(isolated_paths):
    """Esc-cancelled turns land here: Stop fires with no completed tools."""
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s1", lifecycle.IMPLEMENTATION)
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=[]))

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    assert lifecycle.load("s1").phase == lifecycle.IMPLEMENTATION


def test_stop_toggle_off_never_advances(isolated_paths):
    # Do NOT call _enable_auto_advance. With no config.toml, auto_advance defaults off.
    _make_state_at_phase("s1", lifecycle.IMPLEMENTATION)
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Edit"]))

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    assert lifecycle.load("s1").phase == lifecycle.IMPLEMENTATION
    # Turn file still cleared — clean-up always happens.
    assert lifecycle.load_turn("s1") is None


def test_stop_review_phase_never_auto_advances(isolated_paths):
    """Design decision 1: REVIEW → CORRECTION is user-driven."""
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s1", lifecycle.REVIEW)
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Edit"]))

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    assert lifecycle.load("s1").phase == lifecycle.REVIEW


def test_stop_terminal_phase_noop(isolated_paths):
    _enable_auto_advance(isolated_paths)
    state = lifecycle.start("s1", "build a rate limiter")
    lifecycle.force_complete(state, note="user said LGTM")
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Edit"]))

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    assert lifecycle.load("s1").phase == lifecycle.COMPLETE


def test_stop_time_window_guard(isolated_paths):
    """Guard: if state was updated < 1s ago, Stop defers (user just advanced it)."""
    _enable_auto_advance(isolated_paths)
    # Create state at IMPLEMENTATION with a fresh updated_at (< 1s ago).
    state = lifecycle.start("s1", "build a rate limiter")
    state = lifecycle.advance(state)  # IMPLEMENTATION; updated_at = now
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Edit"]))

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    # Guard dropped the advance.
    assert lifecycle.load("s1").phase == lifecycle.IMPLEMENTATION


def test_stop_missing_session_id_noop(isolated_paths):
    # Creating state for an unrelated session; the handler should not touch it.
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s_other", lifecycle.IMPLEMENTATION)

    rc = _run_handler(hook_mod.run_stop, {})
    assert rc == 0
    assert lifecycle.load("s_other").phase == lifecycle.IMPLEMENTATION


def test_stop_no_turn_file_is_noop(isolated_paths):
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s1", lifecycle.IMPLEMENTATION)
    # No turn file.
    assert lifecycle.load_turn("s1") is None

    _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    assert lifecycle.load("s1").phase == lifecycle.IMPLEMENTATION


def test_stop_silent_when_advance_raises(isolated_paths):
    """Stop handler swallows exceptions from advance() so Claude Code never sees a failure."""
    _enable_auto_advance(isolated_paths)
    _make_state_at_phase("s1", lifecycle.IMPLEMENTATION)
    lifecycle.save_turn(lifecycle.TurnState(session_id="s1", tool_names=["Edit"]))

    with patch("skill_advisor.hook.lifecycle.advance", side_effect=RuntimeError("boom")):
        rc = _run_handler(hook_mod.run_stop, {"session_id": "s1"})

    assert rc == 0
    # Turn file was cleared before the exception was raised.
    assert lifecycle.load_turn("s1") is None


def _backdate_session(session_id: str, age_seconds: float = 5.0) -> None:
    """Rewrite the session file's updated_at so the double-advance guard doesn't fire."""
    data = json.loads(paths.session_file(session_id).read_text())
    data["updated_at"] = time.time() - age_seconds
    paths.session_file(session_id).write_text(json.dumps(data), encoding="utf-8")


def test_stop_transitions_into_parallelization_check(monkeypatch):
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[lifecycle]
enabled = true
[lifecycle.auto_advance]
enabled = true
[parallelization]
enabled = true
min_tasks = 3
""",
        encoding="utf-8",
    )

    state = lifecycle.start("sess-s1", "build thing")
    assert state.phase == lifecycle.PLANNING
    # Simulate the turn that issued TodoWrite. Backdate updated_at so the
    # double-advance guard doesn't skip us.
    lifecycle.save(state)
    _backdate_session("sess-s1")
    lifecycle.record_tool("sess-s1", "TodoWrite")
    lifecycle.record_todo_write("sess-s1", ["A", "B", "C"])

    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"session_id": "sess-s1"})))
    assert hook.run_stop() == 0

    reloaded = lifecycle.load("sess-s1")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.PARALLELIZATION_CHECK
    assert reloaded.pending_todos == ["A", "B", "C"]


def test_stop_skips_parallelization_check_below_min_tasks(monkeypatch):
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[lifecycle]
enabled = true
[lifecycle.auto_advance]
enabled = true
[parallelization]
enabled = true
min_tasks = 5
""",
        encoding="utf-8",
    )

    state = lifecycle.start("sess-s2", "build thing")
    lifecycle.save(state)
    _backdate_session("sess-s2")
    lifecycle.record_tool("sess-s2", "TodoWrite")
    lifecycle.record_todo_write("sess-s2", ["A", "B"])

    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"session_id": "sess-s2"})))
    assert hook.run_stop() == 0

    reloaded = lifecycle.load("sess-s2")
    assert reloaded is not None
    # No transition — too few tasks.
    assert reloaded.phase == lifecycle.PLANNING
    assert reloaded.pending_todos == []


def test_stop_parallelization_check_requires_enabled_flag(monkeypatch):
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[lifecycle]
enabled = true
[lifecycle.auto_advance]
enabled = true
[parallelization]
enabled = false
""",
        encoding="utf-8",
    )

    state = lifecycle.start("sess-s3", "build thing")
    lifecycle.save(state)
    _backdate_session("sess-s3")
    lifecycle.record_tool("sess-s3", "TodoWrite")
    # Even if something else put a payload here, we must stay silent when disabled.
    lifecycle.record_todo_write("sess-s3", ["A", "B", "C"])

    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"session_id": "sess-s3"})))
    assert hook.run_stop() == 0

    reloaded = lifecycle.load("sess-s3")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.PLANNING