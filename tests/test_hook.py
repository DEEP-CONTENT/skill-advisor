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
