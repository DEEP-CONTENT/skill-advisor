"""Tests for the opt-in structured event log."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from skill_advisor import paths, telemetry
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import TelemetryConfig
from skill_advisor.matcher import ResolvedPick


def _on_config(**overrides) -> TelemetryConfig:
    base = dict(events_enabled=True, retain_days=90, prompt_hash_salt="")
    base.update(overrides)
    return TelemetryConfig(**base)


def _pick(name: str, score: float = 0.7, kind: str = "skill") -> ResolvedPick:
    return ResolvedPick(
        entry=CatalogEntry(kind=kind, name=name, namespace="user", description=f"{name} desc"),
        reason=f"embedding match ({score:.2f})",
    )


def _read_events(isolated_paths) -> list[dict]:
    path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_writes_event_when_enabled(isolated_paths):
    telemetry.record(
        prompt="refactor the auth middleware",
        session_id="s1",
        picks=[_pick("alpha", 0.9), _pick("beta", 0.7)],
        phase="none",
        duration_ms=123,
        config=_on_config(prompt_hash_salt="fixed-salt"),
    )
    events = _read_events(isolated_paths)
    assert len(events) == 1
    assert events[0]["schema"] == telemetry.SCHEMA_VERSION
    assert events[0]["duration_ms"] == 123
    assert [p["name"] for p in events[0]["picks"]] == ["alpha", "beta"]


def test_record_noop_when_disabled(isolated_paths):
    telemetry.record(
        prompt="anything",
        session_id="s",
        picks=[_pick("alpha")],
        config=TelemetryConfig(events_enabled=False),
    )
    assert _read_events(isolated_paths) == []
    assert not (isolated_paths["cache_home"] / "advisor.events.jsonl").exists()


def test_event_schema_has_expected_fields(isolated_paths):
    telemetry.record(
        prompt="p",
        session_id="s",
        picks=[_pick("a", 0.5)],
        phase="review",
        phase_source="auto",
        judge_used=True,
        triage_skipped=True,
        duration_ms=42,
        config=_on_config(prompt_hash_salt="fixed"),
    )
    [event] = _read_events(isolated_paths)
    for key in (
        "schema", "ts", "session_sha256", "prompt_sha256", "prompt_words",
        "triage_skipped", "duration_ms", "phase", "phase_source", "judge_used",
        "judge_failure", "picks",
    ):
        assert key in event, f"missing field: {key}"
    assert event["phase"] == "review"
    assert event["phase_source"] == "auto"
    assert event["judge_used"] is True
    assert event["triage_skipped"] is True
    assert event["picks"][0]["score"] == 0.5


def test_judge_failure_recorded_when_given(isolated_paths):
    telemetry.record(
        prompt="p",
        session_id="s",
        picks=[_pick("a")],
        judge_used=False,
        judge_failure="timeout",
        config=_on_config(prompt_hash_salt="fixed"),
    )
    [event] = _read_events(isolated_paths)
    assert event["judge_failure"] == "timeout"


def test_judge_failure_defaults_to_none_when_omitted(isolated_paths):
    telemetry.record(
        prompt="p",
        session_id="s",
        picks=[_pick("a")],
        config=_on_config(prompt_hash_salt="fixed"),
    )
    [event] = _read_events(isolated_paths)
    assert event["judge_failure"] is None


def test_prompt_hash_stable_same_salt(isolated_paths):
    cfg = _on_config(prompt_hash_salt="the-salt")
    telemetry.record(prompt="P", session_id="s", picks=[_pick("a")], config=cfg)
    telemetry.record(prompt="P", session_id="s", picks=[_pick("a")], config=cfg)
    events = _read_events(isolated_paths)
    assert events[0]["prompt_sha256"] == events[1]["prompt_sha256"]
    assert events[0]["session_sha256"] == events[1]["session_sha256"]


def test_prompt_hash_changes_with_salt(isolated_paths):
    telemetry.record(
        prompt="P", session_id="s", picks=[_pick("a")],
        config=_on_config(prompt_hash_salt="salt1"),
    )
    telemetry.record(
        prompt="P", session_id="s", picks=[_pick("a")],
        config=_on_config(prompt_hash_salt="salt2"),
    )
    events = _read_events(isolated_paths)
    assert events[0]["prompt_sha256"] != events[1]["prompt_sha256"]
    assert events[0]["session_sha256"] != events[1]["session_sha256"]


def test_session_id_always_hashed_never_stored_raw(isolated_paths):
    telemetry.record(
        prompt="p", session_id="raw-session-id-DO-NOT-LOG",
        picks=[_pick("a")], config=_on_config(prompt_hash_salt="s"),
    )
    contents = (isolated_paths["cache_home"] / "advisor.events.jsonl").read_text()
    assert "raw-session-id-DO-NOT-LOG" not in contents


def test_session_sha256_null_when_no_session_id(isolated_paths):
    telemetry.record(
        prompt="p", session_id=None,
        picks=[_pick("a")], config=_on_config(prompt_hash_salt="s"),
    )
    [event] = _read_events(isolated_paths)
    assert event["session_sha256"] is None


def test_salt_auto_generated_when_config_empty(isolated_paths):
    telemetry.record(
        prompt="p", session_id="s", picks=[_pick("a")],
        config=_on_config(prompt_hash_salt=""),
    )
    salt_file = isolated_paths["cache_home"] / "telemetry.salt"
    assert salt_file.is_file()
    # Linux chmod() should be honored; some filesystems may not enforce, so check what we can.
    if hasattr(os, "stat"):
        mode = salt_file.stat().st_mode & 0o777
        # 0o600 on POSIX-y filesystems; be tolerant of umask interaction on others.
        assert mode in (0o600, 0o644), f"unexpected mode: {oct(mode)}"


def test_concurrent_writes_produce_complete_lines(isolated_paths):
    cfg = _on_config(prompt_hash_salt="t")

    def worker(i: int) -> None:
        for j in range(20):
            telemetry.record(
                prompt=f"prompt-{i}-{j}",
                session_id=f"sess-{i}",
                picks=[_pick(f"skill-{i}-{j}")],
                config=cfg,
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = _read_events(isolated_paths)
    assert len(events) == 8 * 20
    # Every line parsed cleanly — no interleaved writes.


def test_iter_events_skips_malformed_lines(isolated_paths):
    telemetry.record(prompt="p", session_id="s", picks=[_pick("a")], config=_on_config(prompt_hash_salt="x"))
    # Append two junk lines + one more real event.
    path = paths.events_file()
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write('{"incomplete":  \n')
    telemetry.record(prompt="p2", session_id="s", picks=[_pick("b")], config=_on_config(prompt_hash_salt="x"))

    events = list(telemetry.iter_events())
    assert len(events) == 2


def test_iter_events_skips_wrong_schema(isolated_paths, monkeypatch):
    path = paths.events_file()
    paths.ensure_dirs()
    path.write_text(
        json.dumps({"schema": 999, "ts": "2026-04-22T08:00:00Z", "picks": []}) + "\n"
        + json.dumps({
            "schema": telemetry.SCHEMA_VERSION,
            "ts": "2026-04-22T08:00:00Z",
            "picks": [],
        }) + "\n",
        encoding="utf-8",
    )
    events = list(telemetry.iter_events())
    assert len(events) == 1


def test_iter_events_cutoff_filters_older(isolated_paths):
    path = paths.events_file()
    paths.ensure_dirs()
    lines = [
        json.dumps({"schema": 1, "ts": "2026-01-01T00:00:00Z", "picks": []}),
        json.dumps({"schema": 1, "ts": "2026-04-20T00:00:00Z", "picks": []}),
        json.dumps({"schema": 1, "ts": "2026-04-22T00:00:00Z", "picks": []}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cutoff = datetime(2026, 4, 15, tzinfo=timezone.utc)
    events = list(telemetry.iter_events(cutoff=cutoff))
    assert len(events) == 2


def test_parse_duration_days_hours_weeks():
    assert telemetry.parse_duration("7d") == timedelta(days=7)
    assert telemetry.parse_duration("24h") == timedelta(hours=24)
    assert telemetry.parse_duration("2w") == timedelta(weeks=2)
    assert telemetry.parse_duration("  30D ") == timedelta(days=30)
    with pytest.raises(ValueError):
        telemetry.parse_duration("tomorrow")
    with pytest.raises(ValueError):
        telemetry.parse_duration("5y")


def test_purge_older_than_removes_stale(isolated_paths):
    path = paths.events_file()
    paths.ensure_dirs()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(
        "\n".join([
            json.dumps({"schema": 1, "ts": old_ts, "picks": []}),
            json.dumps({"schema": 1, "ts": new_ts, "picks": []}),
            json.dumps({"schema": 1, "ts": old_ts, "picks": []}),
        ]) + "\n",
        encoding="utf-8",
    )
    removed = telemetry.purge_older_than(timedelta(days=7))
    assert removed == 2
    events = list(telemetry.iter_events())
    assert len(events) == 1


def test_purge_older_than_no_file_returns_zero(isolated_paths):
    assert telemetry.purge_older_than(timedelta(days=1)) == 0


def test_record_stop_writes_event_with_kind_stop(isolated_paths):
    telemetry.record_stop(
        session_id="s1",
        tools=["Bash", "Skill", "Edit"],
        subagents=["general-purpose"],
        config=_on_config(prompt_hash_salt="fixed-salt"),
    )
    [event] = _read_events(isolated_paths)
    assert event["kind"] == "stop"
    assert event["schema"] == telemetry.SCHEMA_VERSION
    assert event["session_sha256"] is not None
    assert event["tools"] == ["Bash", "Skill", "Edit"]
    assert event["subagents"] == ["general-purpose"]
    assert "ts" in event


def test_record_stop_noop_when_disabled(isolated_paths):
    telemetry.record_stop(
        session_id="s1",
        tools=["Bash"],
        subagents=[],
        config=TelemetryConfig(events_enabled=False),
    )
    assert _read_events(isolated_paths) == []


def test_prompt_event_kind_defaults_to_prompt(isolated_paths):
    """Backward compatibility: existing prompt events get kind='prompt' implicitly."""
    telemetry.record(
        prompt="p", session_id="s", picks=[_pick("a")],
        config=_on_config(prompt_hash_salt="x"),
    )
    [event] = _read_events(isolated_paths)
    # New events explicitly stamp kind="prompt"; old events without the field
    # are treated as kind="prompt" by readers (tested elsewhere).
    assert event.get("kind", "prompt") == "prompt"


def test_record_stop_session_id_hashed_not_raw(isolated_paths):
    telemetry.record_stop(
        session_id="raw-sess-id-DO-NOT-LOG",
        tools=["Bash"],
        subagents=[],
        config=_on_config(prompt_hash_salt="s"),
    )
    contents = (isolated_paths["cache_home"] / "advisor.events.jsonl").read_text()
    assert "raw-sess-id-DO-NOT-LOG" not in contents


def test_stop_event_records_invoked_skills(isolated_paths):
    import json as _json

    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s1", tools=["Skill"], subagents=[],
        skills=["superpowers:writing-plans"],
        config=TelemetryConfig(events_enabled=True),
    )
    events = [
        _json.loads(line)
        for line in paths.events_file().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["skills"] == ["superpowers:writing-plans"]


def test_record_stop_writes_tool_spans(isolated_paths):
    import json
    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s1",
        tools=["Read", "Bash"],
        subagents=[],
        skills=["ai-code-review"],
        tool_spans=[("Read", 412), ("Bash", 138204)],
        config=TelemetryConfig(events_enabled=True),
    )

    row = json.loads(paths.events_file().read_text(encoding="utf-8").splitlines()[-1])
    assert row["tool_spans"] == [["Read", 412], ["Bash", 138204]]
    assert row["span_anchor"] == "previous_tool"


def test_record_stop_omits_spans_when_there_are_none(isolated_paths):
    """A turn from before the feature must not gain a misleading empty key."""
    import json
    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s2", tools=["Read"], subagents=[], skills=[],
        config=TelemetryConfig(events_enabled=True),
    )

    row = json.loads(paths.events_file().read_text(encoding="utf-8").splitlines()[-1])
    assert "tool_spans" not in row
    assert "span_anchor" not in row


def test_record_stop_never_writes_tool_input(isolated_paths):
    """Privacy: names only. No paths, no commands, no arguments."""
    from skill_advisor import paths, telemetry
    from skill_advisor.config import TelemetryConfig

    telemetry.record_stop(
        session_id="s3",
        tools=["Bash"], subagents=[], skills=[],
        tool_spans=[("Bash", 900)],
        config=TelemetryConfig(events_enabled=True),
    )

    raw = paths.events_file().read_text(encoding="utf-8")
    assert "tool_input" not in raw
    assert "/home/" not in raw
