import io
import json
from unittest.mock import patch

from skill_advisor import hook as hook_mod
from skill_advisor.catalog import CatalogEntry
from skill_advisor.matcher import PickResult, ResolvedPick


def _run_with_stdin(payload: dict) -> str:
    buf_out = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", buf_out):
        hook_mod.run()
    return buf_out.getvalue()


def test_hook_emits_additional_context_when_picks_found():
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="vague idea needs shaping")], state=None)
    with patch("skill_advisor.hook.matcher.pick", return_value=result):
        out = _run_with_stdin({"prompt": "let's design a new feature for session replay"})
    envelope = json.loads(out)
    assert envelope["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "brainstorming" in envelope["hookSpecificOutput"]["additionalContext"]


def test_hook_silent_on_no_picks():
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        out = _run_with_stdin({"prompt": "refactor the checkout module"})
    assert out == ""


def test_hook_silent_on_empty_prompt():
    out = _run_with_stdin({"prompt": ""})
    assert out == ""


def test_hook_silent_on_malformed_stdin():
    buf_out = io.StringIO()
    with patch("sys.stdin", io.StringIO("not json at all")), patch("sys.stdout", buf_out):
        hook_mod.run()
    assert buf_out.getvalue() == ""


def test_hook_silent_on_matcher_exception():
    with patch("skill_advisor.hook.matcher.pick", side_effect=RuntimeError("boom")):
        out = _run_with_stdin({"prompt": "refactor the checkout module please"})
    assert out == ""


def test_hook_passes_session_id_to_matcher():
    with patch("skill_advisor.hook.matcher.pick", return_value=None) as mock_pick:
        _run_with_stdin({"prompt": "build a rate limiter", "session_id": "sess-123"})
    kwargs = mock_pick.call_args.kwargs
    assert kwargs.get("session_id") == "sess-123"


# ---------------------------------------------------------------------------
# Telemetry integration
# ---------------------------------------------------------------------------


def _enable_telemetry_in_config(isolated_paths):
    """Write a minimal config.toml that enables telemetry."""
    cfg = isolated_paths["config_home"] / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("[telemetry]\nevents_enabled = true\nprompt_hash_salt = \"fixed\"\n")


def _enable_telemetry_and_judge(isolated_paths):
    """Telemetry on AND use_judge on — so `judge_used` echoing the config is
    distinguishable from `judge_used` reporting what actually happened."""
    cfg = isolated_paths["config_home"] / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "[telemetry]\nevents_enabled = true\nprompt_hash_salt = \"fixed\"\n"
        "\n[matcher]\nuse_judge = true\n"
    )


def test_hook_writes_telemetry_event_when_enabled(isolated_paths):
    _enable_telemetry_in_config(isolated_paths)
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="embedding match (0.80)")], state=None)
    with patch("skill_advisor.hook.matcher.pick", return_value=result):
        _run_with_stdin({"prompt": "let's design a new feature for session replay", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    assert events_path.is_file()
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["picks"][0]["name"] == "brainstorming"
    assert lines[0]["session_sha256"] is not None
    assert "s1" not in events_path.read_text()  # raw session_id never stored


def test_hook_no_telemetry_when_disabled(isolated_paths):
    # No config.toml → events_enabled defaults to False.
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="...")], state=None)
    with patch("skill_advisor.hook.matcher.pick", return_value=result):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    assert not events_path.exists()


def test_hook_silent_when_telemetry_record_fails(isolated_paths):
    _enable_telemetry_in_config(isolated_paths)
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="...")], state=None)
    with patch("skill_advisor.hook.matcher.pick", return_value=result), \
         patch("skill_advisor.hook.telemetry.record", side_effect=RuntimeError("disk full")):
        out = _run_with_stdin({"prompt": "a substantive prompt that should match"})

    # Output still emitted; telemetry failure didn't break the hook.
    envelope = json.loads(out)
    assert "brainstorming" in envelope["hookSpecificOutput"]["additionalContext"]


def test_hook_folds_the_prompt_into_the_sketch(isolated_paths):
    import numpy as np

    from skill_advisor import centroids

    _enable_telemetry_in_config(isolated_paths)
    unit = np.zeros(centroids.DIM, dtype=np.float32)
    unit[0] = 1.0

    with patch("skill_advisor.hook.matcher.pick", return_value=None), \
         patch("skill_advisor.hook.index_mod.embed_one", return_value=unit):
        _run_with_stdin({"prompt": "why is this pod crashlooping in sydcdev", "session_id": "s1"})

    assert centroids.load().observed == 1


def test_hook_does_not_write_the_sketch_when_telemetry_is_off(isolated_paths):
    from skill_advisor import paths

    # No config.toml → events_enabled defaults to False.
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        _run_with_stdin({"prompt": "why is this pod crashlooping in sydcdev", "session_id": "s1"})

    assert not paths.centroids_file().exists()


def test_hook_skips_sketch_update_for_triage_skipped_prompts(isolated_paths):
    """F4: the sketch update was gated only on events_enabled, so it ran even
    for a prompt matcher.pick() itself never touches the index for —
    triage.should_skip() rejects trivial acknowledgements like "ok" before
    matcher.pick() does any real work (outside an active lifecycle). That
    both wastes latency on a path meant to be near-free (README promises
    ~0 ms for triage-skipped prompts) and pollutes the 8-slot centroid
    sketch with chit-chat — a trivial prompt is dissimilar to real work, so
    it claims one of only 8 slots and then accumulates count, becoming
    sticky."""
    import numpy as np

    from skill_advisor import centroids, paths

    _enable_telemetry_in_config(isolated_paths)
    unit = np.zeros(centroids.DIM, dtype=np.float32)
    unit[0] = 1.0

    with patch("skill_advisor.hook.matcher.pick", return_value=None), \
         patch(
             "skill_advisor.hook.index_mod.embed_one", return_value=unit
         ) as embed_mock:
        _run_with_stdin({"prompt": "ok", "session_id": "s1"})

    embed_mock.assert_not_called()
    assert not paths.centroids_file().exists()


def test_a_failing_sketch_update_never_breaks_the_hook(isolated_paths):
    """Hook paths are silent on error. A broken sketch must not cost a pick."""
    _enable_telemetry_in_config(isolated_paths)
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="...")], state=None)

    with patch("skill_advisor.hook.matcher.pick", return_value=result), \
         patch("skill_advisor.hook.index_mod.embed_one", side_effect=RuntimeError("boom")):
        out = _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    assert "brainstorming" in out


def test_sketch_update_is_bounded_by_the_hook_alarm(isolated_paths):
    """A hang inside embed_one() must be interrupted by the SIGALRM budget the
    hook already arms around matcher.pick(), not run unbounded past it.

    A raising mock (like the other sketch tests here) can prove the failure
    path is caught, but can never prove an alarm actually exists — only a
    real sleep racing the real signal can. This is exactly the gap that let
    an earlier version of this wiring ship with the sketch update running
    after signal.alarm(0) had already disarmed the timer: every test used
    side_effect=RuntimeError, which is indistinguishable from "the alarm
    caught it" even when there was no alarm at all.
    """
    import time as time_mod

    import numpy as np

    cfg_path = isolated_paths["config_home"] / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "[telemetry]\nevents_enabled = true\n\n[matcher]\nbudget_seconds = 2.0\n"
    )

    def _hang(text):
        time_mod.sleep(12)
        return np.zeros(384, dtype=np.float32)

    with patch("skill_advisor.hook.matcher.pick", return_value=None), \
         patch("skill_advisor.hook.index_mod.embed_one", side_effect=_hang):
        started = time_mod.monotonic()
        _run_with_stdin(
            {"prompt": "why is this pod crashlooping in sydcdev", "session_id": "s1"}
        )
        elapsed = time_mod.monotonic() - started

    assert elapsed < 6.0, (
        f"hook.run() took {elapsed:.1f}s against a 2.0s budget; "
        "the sketch update escaped the alarm"
    )


def test_hook_records_event_even_when_no_picks(isolated_paths):
    _enable_telemetry_in_config(isolated_paths)
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        out = _run_with_stdin({"prompt": "a substantive prompt that should match"})

    # No additional context emitted...
    assert out == ""
    # ...but the event is still logged with empty picks.
    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    assert events_path.is_file()
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert lines[0]["picks"] == []


def test_judge_used_is_false_when_the_judge_never_ran(isolated_paths):
    """Today `judge_used` records cfg.matcher.use_judge, so 830 triage-skipped
    events on the live log claim the judge ran. It must report what happened."""
    _enable_telemetry_and_judge(isolated_paths)
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert lines[-1]["judge_used"] is False


def test_judge_failure_is_recorded(isolated_paths):
    _enable_telemetry_and_judge(isolated_paths)

    def _fake_pick(prompt, cfg, session_id=None, *, trace=None):
        if trace is not None:
            trace.ran = False
            trace.failure = "timeout"
        return None

    with patch("skill_advisor.hook.matcher.pick", _fake_pick):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert lines[-1]["judge_failure"] == "timeout"
    assert lines[-1]["judge_used"] is False


def test_budget_exceeded_records_a_countable_telemetry_event(isolated_paths):
    """An alarm kill must not be invisible in events.jsonl — no row at all is
    indistinguishable from the hook never firing. It needs its own marker,
    distinct from every judge.FAILURE_* value."""
    _enable_telemetry_in_config(isolated_paths)
    with patch("skill_advisor.hook.matcher.pick", side_effect=hook_mod._BudgetExceeded):
        out = _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    assert out == ""
    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    assert events_path.is_file()
    lines = [
        json.loads(line) for line in events_path.read_text().splitlines() if line.strip()
    ]
    assert len(lines) == 1
    assert lines[-1]["judge_failure"] == "budget_exceeded"
    assert lines[-1]["judge_used"] is False
    assert lines[-1]["picks"] == []


def test_budget_exceeded_after_a_completed_judge_keeps_both_facts(isolated_paths):
    """The alarm can fire AFTER the judge answered — during the post-judge work
    in `matcher.pick()` (effort classification, lifecycle bookkeeping). The row
    then carries `judge_used: true` alongside `judge_failure: "budget_exceeded"`.

    That pair is deliberate, not a contradiction. The two fields answer different
    questions: `judge_used` is "did the subprocess run and return a verdict",
    `judge_failure` is "why did this turn emit no picks". Collapsing it — by
    forcing `judge_used` false here — would erase the only signal that the alarm
    is discarding *completed* verdicts, which is precisely the tell that
    `budget_seconds` is too close to the judge's own timeout.
    """
    _enable_telemetry_and_judge(isolated_paths)

    def _pick_then_alarm(prompt, cfg, session_id=None, *, trace=None):
        if trace is not None:
            trace.ran = True  # judge completed and returned a verdict...
        raise hook_mod._BudgetExceeded()  # ...then the alarm fired

    with patch("skill_advisor.hook.matcher.pick", _pick_then_alarm):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    lines = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    assert lines[-1]["judge_used"] is True
    assert lines[-1]["judge_failure"] == "budget_exceeded"
    assert lines[-1]["picks"] == []


def test_budget_exceeded_writes_no_event_when_telemetry_disabled(isolated_paths):
    # No config.toml → events_enabled defaults to False.
    with patch("skill_advisor.hook.matcher.pick", side_effect=hook_mod._BudgetExceeded):
        out = _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    assert out == ""
    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    assert not events_path.exists()


def test_judge_used_is_true_when_the_judge_actually_ran(isolated_paths):
    _enable_telemetry_and_judge(isolated_paths)
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="judge said so")], state=None)

    def _fake_pick(prompt, cfg, session_id=None, *, trace=None):
        if trace is not None:
            trace.ran = True
        return result

    with patch("skill_advisor.hook.matcher.pick", _fake_pick):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert lines[-1]["judge_used"] is True
    assert lines[-1]["judge_failure"] is None


def test_posttooluse_records_todowrite_when_enabled(monkeypatch, tmp_path):
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    config_path = paths.config_file()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[parallelization]\nenabled = true\n",
        encoding="utf-8",
    )

    event = {
        "session_id": "sess-1",
        "tool_name": "TodoWrite",
        "tool_input": {
            "todos": [
                {"content": "Parse X", "activeForm": "Parsing X", "status": "pending"},
                {"content": "Wire Y", "activeForm": "Wiring Y", "status": "pending"},
                {"content": "Test Z", "activeForm": "Testing Z", "status": "pending"},
            ],
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-1")
    assert turn is not None
    assert turn.todo_write == {
        "count": 3,
        "titles": ["Parse X", "Wire Y", "Test Z"],
    }
    # Tool name still recorded via existing path.
    assert turn.tool_names == ["TodoWrite"]


def test_posttooluse_skips_todowrite_when_disabled(monkeypatch):
    import io
    import json as _json
    from skill_advisor import hook, lifecycle

    event = {
        "session_id": "sess-2",
        "tool_name": "TodoWrite",
        "tool_input": {"todos": [{"content": "A"}, {"content": "B"}, {"content": "C"}]},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-2")
    assert turn is not None
    assert turn.todo_write is None
    assert turn.tool_names == ["TodoWrite"]


def test_posttooluse_todowrite_malformed_payload_is_silent(monkeypatch):
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[parallelization]\nenabled = true\n", encoding="utf-8")

    # tool_input.todos is not a list — must not raise.
    event = {
        "session_id": "sess-3",
        "tool_name": "TodoWrite",
        "tool_input": {"todos": "not a list"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-3")
    assert turn is not None
    assert turn.todo_write is None


def test_posttooluse_todowrite_non_dict_tool_input_is_silent(monkeypatch):
    """Reviewer C1: a non-dict tool_input must not raise AttributeError."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[parallelization]\nenabled = true\n", encoding="utf-8")

    # tool_input is a list (truthy non-dict) — must not raise.
    event = {
        "session_id": "sess-c1",
        "tool_name": "TodoWrite",
        "tool_input": [1, 2, 3],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0  # exits 0, no traceback

    turn = lifecycle.load_turn("sess-c1")
    # tool_name still gets recorded via record_tool, but no todo_write capture.
    assert turn is not None
    assert turn.todo_write is None


def test_posttooluse_taskcreate_appends_subject(monkeypatch):
    """Each TaskCreate call adds one task; three calls reach min_tasks=3."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[parallelization]\nenabled = true\n", encoding="utf-8")

    for subject in ("Parse X", "Wire Y", "Test Z"):
        event = {
            "session_id": "sess-tc1",
            "tool_name": "TaskCreate",
            "tool_input": {
                "subject": subject,
                "description": f"do {subject}",
            },
        }
        monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
        assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-tc1")
    assert turn is not None
    assert turn.todo_write == {
        "count": 3,
        "titles": ["Parse X", "Wire Y", "Test Z"],
    }
    assert turn.tool_names == ["TaskCreate", "TaskCreate", "TaskCreate"]


def test_posttooluse_taskcreate_skipped_when_disabled(monkeypatch):
    """Without [parallelization] enabled, TaskCreate doesn't populate todo_write."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle

    event = {
        "session_id": "sess-tc2",
        "tool_name": "TaskCreate",
        "tool_input": {"subject": "A", "description": "do A"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-tc2")
    assert turn is not None
    assert turn.todo_write is None
    # Tool name still recorded so the lifecycle layer sees it.
    assert turn.tool_names == ["TaskCreate"]


def test_posttooluse_taskcreate_missing_subject_is_silent(monkeypatch):
    """A TaskCreate with no subject (or empty subject) must not blow up or write."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[parallelization]\nenabled = true\n", encoding="utf-8")

    event = {
        "session_id": "sess-tc3",
        "tool_name": "TaskCreate",
        "tool_input": {"description": "no subject here"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-tc3")
    assert turn is not None
    assert turn.todo_write is None


def test_posttooluse_captures_the_invoked_skill_name(monkeypatch):
    """`tools` records only the string "Skill" — 1,482 times across 3,971 stop
    events, never which one. invocation_rate is uncomputable without this."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle

    event = {
        "session_id": "sess-1",
        "tool_name": "Skill",
        "tool_input": {"skill": "superpowers:writing-plans"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-1")   # lifecycle.py:453, returns TurnState | None
    assert turn is not None
    assert turn.skills_invoked == ["superpowers:writing-plans"]
    assert turn.tool_names == ["Skill"]    # existing path still records the tool


def test_posttooluse_ignores_a_missing_skill_field(monkeypatch):
    """Non-Skill tools carry no `skill` key; that must not append an empty name."""
    import io
    import json as _json
    from skill_advisor import hook, lifecycle

    event = {"session_id": "sess-2", "tool_name": "Read", "tool_input": {"file_path": "/x"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(event)))
    assert hook.run_posttooluse() == 0

    turn = lifecycle.load_turn("sess-2")
    assert turn is not None
    assert turn.skills_invoked == []


# ---------------------------------------------------------------------------
# Effort: nudge builder + rate-limited state
# ---------------------------------------------------------------------------

import json

from skill_advisor import effort, hook, paths


def _emit_capture(capsys):
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


def test_emit_includes_system_message(capsys):
    hook._emit("ctx", system_message="hello")
    payload = _emit_capture(capsys)
    assert payload["hookSpecificOutput"]["additionalContext"] == "ctx"
    assert payload["systemMessage"] == "hello"


def test_emit_omits_system_message_when_none(capsys):
    hook._emit("ctx")
    payload = _emit_capture(capsys)
    assert "systemMessage" not in payload


def test_nudge_suppressed_without_observation():
    """First prompt of a session — the sensor has not run yet."""
    msg = hook._nudge_message(observed=None, rec=effort.EffortRecommendation("xhigh", "r", "judge"))
    assert msg is None


def test_nudge_message_names_both_levels():
    msg = hook._nudge_message(
        observed="medium", rec=effort.EffortRecommendation("xhigh", "5 tasks", "judge")
    )
    assert "xhigh" in msg and "medium" in msg
    assert "/effort xhigh" in msg


def test_ultracode_nudge_suggests_the_keyword_not_a_slash_command():
    msg = hook._nudge_message(
        observed="medium",
        rec=effort.EffortRecommendation("ultracode", "parallel tasks", "parallelization"),
    )
    assert "ultracode" in msg
    assert "/effort" not in msg


def test_nudge_suppressed_at_max():
    msg = hook._nudge_message(
        observed="max", rec=effort.EffortRecommendation("low", "r", "heuristic")
    )
    assert msg is None


def test_nudge_rate_limited_per_level_pair():
    """A long session must not nag on every prompt for the same disagreement."""
    from skill_advisor import baseline

    assert baseline.mark_nudged("s1", "medium", "xhigh") is True
    assert baseline.mark_nudged("s1", "medium", "xhigh") is False
    # a different pair is a genuinely new piece of information
    assert baseline.mark_nudged("s1", "medium", "low") is True


# ---------------------------------------------------------------------------
# Session finalisation (Stop) and the baseline-move announcement (first
# prompt after a write)
# ---------------------------------------------------------------------------


def test_stop_finalises_session_and_may_write(monkeypatch, isolated_paths):
    from skill_advisor import baseline

    _enable_effort_in_config(isolated_paths)

    calls = []
    monkeypatch.setattr(baseline, "finalise_session", lambda s: calls.append(("final", s)))
    monkeypatch.setattr(baseline, "decrement_cooldown", lambda s: calls.append(("dec", s)))
    monkeypatch.setattr(baseline, "maybe_write", lambda cfg, launch_level: calls.append(("write",)))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"s1"}'))
    hook.run_stop()
    assert ("final", "s1") in calls
    assert ("dec", "s1") in calls
    assert ("write",) in calls


def test_stop_finalisation_skipped_when_effort_disabled(monkeypatch):
    """No config.toml → effort.enabled defaults False. Finalisation must not run."""
    from skill_advisor import baseline

    calls = []
    monkeypatch.setattr(baseline, "finalise_session", lambda s: calls.append(("final", s)))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"s1"}'))
    rc = hook.run_stop()
    assert rc == 0
    assert calls == []


def test_stop_finalisation_swallows_exceptions(isolated_paths):
    """run_stop must always return 0, even if baseline bookkeeping blows up."""
    _enable_effort_in_config(isolated_paths)
    with patch("skill_advisor.hook.baseline.finalise_session", side_effect=RuntimeError("boom")), \
         patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"}))):
        rc = hook.run_stop()
    assert rc == 0


def test_run_no_picks_emits_pending_announcement(isolated_paths):
    """The no-picks branch is a second emission site: an announcement pending
    from a previous session's Stop must reach the user even when this turn
    matched no skills.
    """
    from skill_advisor import baseline

    _enable_effort_in_config(isolated_paths)
    baseline._save({"announce": {"from": "xhigh", "to": "low", "sessions": 3}})

    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        out = _run_with_stdin({"prompt": "anything", "session_id": "s1"})
    envelope = json.loads(out)
    assert envelope["hookSpecificOutput"]["additionalContext"] == ""
    assert "xhigh" in envelope["systemMessage"]
    assert "low" in envelope["systemMessage"]

    # Consumed once: a second no-picks prompt in the same session is fully silent.
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        out2 = _run_with_stdin({"prompt": "anything else", "session_id": "s1"})
    assert out2 == ""


def test_run_no_picks_announcement_merges_with_pending_nudge_via_emit_nudged(isolated_paths):
    """When both an ordinary effort nudge and an announcement are pending on
    a no-picks turn, the announcement must route through `_emit_nudged` (not
    a raw `_emit`) — proven here by checking that the nudge's rate-limit slot
    is actually consumed, which only `_emit_nudged` does.
    """
    from skill_advisor import baseline

    _enable_effort_in_config(isolated_paths)
    _write_observed("s1", "medium")
    baseline._save({"announce": {"from": "xhigh", "to": "low", "sessions": 3}})

    empty_result = PickResult(
        picks=[], state=None, effort=effort.EffortRecommendation("xhigh", "5 tasks", "judge")
    )
    with patch("skill_advisor.hook.matcher.pick", return_value=empty_result):
        out = _run_with_stdin({"prompt": "anything", "session_id": "s1"})
    envelope = json.loads(out)
    # Both messages landed in the single systemMessage, announcement first.
    assert "low" in envelope["systemMessage"]
    assert "you're at medium" in envelope["systemMessage"]
    assert envelope["systemMessage"].index("skill-advisor moved") < envelope["systemMessage"].index(
        "this looks like"
    )
    # The ordinary nudge rode along and was actually shown, so its rate-limit
    # slot must now be consumed — proof this went through `_emit_nudged`.
    assert baseline.was_nudged("s1", "medium", "xhigh") is True


def test_run_no_picks_without_announcement_stays_silent(isolated_paths):
    """No baseline write happened — no picks and no announcement must produce
    no output at all, matching the pre-existing no-picks-stays-silent contract.
    """
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        out = _run_with_stdin({"prompt": "anything", "session_id": "s1"})
    assert out == ""


def test_take_announcement_not_called_when_effort_disabled(isolated_paths):
    """With cfg.effort.enabled False, take_announcement must never even be
    consulted — a stray announce payload must survive untouched for whenever
    the feature is turned on.
    """
    from skill_advisor import baseline, paths as paths_mod

    paths_mod.ensure_dirs()
    baseline._save({"announce": {"from": "xhigh", "to": "low", "sessions": 3}})

    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        out = _run_with_stdin({"prompt": "anything", "session_id": "s1"})
    assert out == ""
    data = json.loads(paths_mod.baseline_file().read_text(encoding="utf-8"))
    assert data.get("announce") == {"from": "xhigh", "to": "low", "sessions": 3}


def test_nudge_bookkeeping_does_not_touch_lifecycle_state():
    """Regression guard: writing nudge state must not refresh `updated_at`.

    `hook.run_stop()` skips auto-advance when the lifecycle state was updated
    less than a second ago. If nudge bookkeeping went through `lifecycle.save()`
    it would bump that timestamp on every prompt and silently disable
    auto-advance.
    """
    from skill_advisor import baseline, lifecycle

    state = lifecycle.start("s1", "build a thing")
    before = lifecycle.load("s1").updated_at
    baseline.mark_nudged("s1", "medium", "xhigh")
    assert lifecycle.load("s1").updated_at == before


# ---------------------------------------------------------------------------
# Fix round 1: the nudge rate-limit slot must be consumed only when the
# nudge was actually shown to the user — not merely computed.
# ---------------------------------------------------------------------------


def _enable_effort_in_config(isolated_paths):
    cfg = isolated_paths["config_home"] / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("[effort]\nenabled = true\nnudge = true\n")


def _write_observed(session_id: str, level: str) -> None:
    paths.ensure_dirs()
    paths.observed_effort_file().write_text(
        json.dumps({"session_id": session_id, "level": level}), encoding="utf-8"
    )


def test_hook_no_picks_does_not_consume_nudge_slot(isolated_paths):
    """A PickResult with empty picks never reaches _emit — the slot must survive."""
    from skill_advisor import baseline

    _enable_effort_in_config(isolated_paths)
    _write_observed("s1", "medium")

    empty_result = PickResult(
        picks=[], state=None, effort=effort.EffortRecommendation("xhigh", "5 tasks", "judge")
    )
    with patch("skill_advisor.hook.matcher.pick", return_value=empty_result):
        out = _run_with_stdin({"prompt": "anything", "session_id": "s1"})
    assert out == ""
    # Nothing was shown, so the pair must still be un-nudged.
    assert baseline.was_nudged("s1", "medium", "xhigh") is False

    # A later prompt that DOES have picks must still get the nudge — proving
    # the earlier no-picks turn never burned the slot.
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result_with_picks = PickResult(
        picks=[ResolvedPick(entry=entry, reason="embedding match (0.80)")],
        state=None,
        effort=effort.EffortRecommendation("xhigh", "5 tasks", "judge"),
    )
    with patch("skill_advisor.hook.matcher.pick", return_value=result_with_picks):
        out2 = _run_with_stdin({"prompt": "anything else", "session_id": "s1"})
    envelope = json.loads(out2)
    assert envelope.get("systemMessage")
    assert "xhigh" in envelope["systemMessage"]


def test_hook_emit_failure_does_not_consume_nudge_slot(isolated_paths):
    """If _emit raises, the message never reached the user — slot must survive."""
    from skill_advisor import baseline

    _enable_effort_in_config(isolated_paths)
    _write_observed("s1", "medium")

    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(
        picks=[ResolvedPick(entry=entry, reason="embedding match (0.80)")],
        state=None,
        effort=effort.EffortRecommendation("xhigh", "5 tasks", "judge"),
    )
    with patch("skill_advisor.hook.matcher.pick", return_value=result), \
         patch("skill_advisor.hook._emit", side_effect=RuntimeError("boom")):
        out = _run_with_stdin({"prompt": "anything", "session_id": "s1"})
    assert out == ""
    assert baseline.was_nudged("s1", "medium", "xhigh") is False


def test_hook_nudge_shown_once_across_two_successful_emits(isolated_paths):
    """Existing behaviour preserved: a genuinely shown nudge is not repeated."""
    _enable_effort_in_config(isolated_paths)
    _write_observed("s1", "medium")

    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(
        picks=[ResolvedPick(entry=entry, reason="embedding match (0.80)")],
        state=None,
        effort=effort.EffortRecommendation("xhigh", "5 tasks", "judge"),
    )
    with patch("skill_advisor.hook.matcher.pick", return_value=result):
        out1 = _run_with_stdin({"prompt": "first", "session_id": "s1"})
        out2 = _run_with_stdin({"prompt": "second", "session_id": "s1"})
    env1 = json.loads(out1)
    env2 = json.loads(out2)
    assert env1.get("systemMessage")
    assert "systemMessage" not in env2
