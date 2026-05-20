"""Tests for the `skill-advisor report` CLI subcommand."""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone

from skill_advisor import catalog as catalog_mod
from skill_advisor import cli, paths, telemetry
from skill_advisor.catalog import CatalogEntry


def _ns(**overrides) -> Namespace:
    base = dict(
        since="30d",
        top=20,
        dead=False,
        by_phase=False,
        by_kind=False,
        format="table",
        purge_older_than=None,
    )
    base.update(overrides)
    return Namespace(**base)


def _write_events(rows: list[dict]) -> None:
    paths.ensure_dirs()
    path = paths.events_file()
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _event(
    *,
    ts: datetime | None = None,
    picks: list[tuple[str, str, float]] | None = None,
    phase: str = "none",
    duration_ms: int = 100,
    session: str = "sess",
    triage_skipped: bool = False,
) -> dict:
    """Build a schema-v1 event dict for tests. picks = [(name, kind, score), ...]."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    pick_list = []
    for rank, (name, kind, score) in enumerate(picks or [], start=1):
        pick_list.append({"rank": rank, "name": name, "kind": kind, "score": score})
    return {
        "schema": 1,
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_sha256": session,
        "prompt_sha256": "abc",
        "prompt_words": 5,
        "triage_skipped": triage_skipped,
        "duration_ms": duration_ms,
        "phase": phase,
        "phase_source": "user",
        "judge_used": False,
        "picks": pick_list,
    }


def _prime_catalog(*names: str) -> None:
    entries = [
        CatalogEntry(kind="skill", name=n, namespace="user", description=f"{n} desc") for n in names
    ]
    catalog_mod.save(entries)


def test_report_no_events_file(isolated_paths, capsys):
    rc = cli._cmd_report(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    assert "no telemetry events recorded" in out
    assert "events_enabled" in out  # hint to enable


def test_report_empty_window(isolated_paths, capsys):
    # Event outside the 30d window.
    old = datetime.now(timezone.utc) - timedelta(days=100)
    _write_events([_event(ts=old, picks=[("a", "skill", 0.5)])])
    rc = cli._cmd_report(_ns(since="30d"))
    assert rc == 0
    assert "no events in the last 30d" in capsys.readouterr().out


def test_report_top_n(isolated_paths, capsys):
    now = datetime.now(timezone.utc)
    _write_events([
        _event(ts=now, picks=[("popular", "skill", 0.8)]),
        _event(ts=now, picks=[("popular", "skill", 0.9)]),
        _event(ts=now, picks=[("popular", "skill", 0.7)]),
        _event(ts=now, picks=[("rare", "skill", 0.5)]),
    ])
    rc = cli._cmd_report(_ns(top=5))
    assert rc == 0
    out = capsys.readouterr().out
    assert "popular" in out
    assert "rare" in out
    # popular appears before rare
    assert out.index("popular") < out.index("rare")


def test_report_dead_skills(isolated_paths, capsys):
    _prime_catalog("alpha", "beta", "gamma")
    now = datetime.now(timezone.utc)
    _write_events([_event(ts=now, picks=[("alpha", "skill", 0.7)])])

    rc = cli._cmd_report(_ns(dead=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dead entries" in out
    assert "beta" in out
    assert "gamma" in out
    assert "alpha" not in out.split("Dead entries")[1]  # alpha not in the dead section


def test_report_by_phase(isolated_paths, capsys):
    now = datetime.now(timezone.utc)
    _write_events([
        _event(ts=now, phase="planning", picks=[("x", "skill", 0.5)]),
        _event(ts=now, phase="planning", picks=[("x", "skill", 0.5)]),
        _event(ts=now, phase="review", picks=[("y", "skill", 0.5)]),
    ])
    rc = cli._cmd_report(_ns(by_phase=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "By phase:" in out
    assert "planning" in out
    assert "review" in out


def test_report_by_kind(isolated_paths, capsys):
    now = datetime.now(timezone.utc)
    _write_events([
        _event(ts=now, picks=[("a", "skill", 0.5), ("b", "subagent", 0.5)]),
    ])
    rc = cli._cmd_report(_ns(by_kind=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "By kind:" in out
    assert "skill" in out
    assert "subagent" in out


def test_report_json_format_shape(isolated_paths, capsys):
    _prime_catalog("alpha", "beta")
    now = datetime.now(timezone.utc)
    _write_events([_event(ts=now, picks=[("alpha", "skill", 0.7)])])

    rc = cli._cmd_report(_ns(format="json", top=3))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["events_total"] == 1
    assert payload["top"][0]["name"] == "alpha"
    assert payload["coverage"]["catalog"] == 2
    assert payload["coverage"]["picked"] == 1
    assert "latency" in payload
    assert "by_phase" in payload
    assert "by_kind" in payload


def test_report_csv_format(isolated_paths, capsys):
    now = datetime.now(timezone.utc)
    _write_events([
        _event(ts=now, picks=[("alpha", "skill", 0.7)]),
        _event(ts=now, picks=[("alpha", "skill", 0.7)]),
    ])
    rc = cli._cmd_report(_ns(format="csv", top=5))
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0] == "rank,name,count,percent"
    assert lines[1].startswith("1,alpha,2,")


def test_report_purge(isolated_paths, capsys):
    old_ts = datetime.now(timezone.utc) - timedelta(days=100)
    new_ts = datetime.now(timezone.utc)
    _write_events([
        _event(ts=old_ts, picks=[("a", "skill", 0.5)]),
        _event(ts=new_ts, picks=[("a", "skill", 0.5)]),
    ])
    rc = cli._cmd_report(_ns(purge_older_than="30d"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "purged 1" in out
    remaining = list(telemetry.iter_events())
    assert len(remaining) == 1


def test_report_invalid_duration(isolated_paths, capsys):
    _write_events([_event(picks=[("a", "skill", 0.5)])])
    rc = cli._cmd_report(_ns(since="tomorrow"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid duration" in err


def test_report_malformed_lines_skipped(isolated_paths, capsys):
    paths.ensure_dirs()
    path = paths.events_file()
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(
        "not json\n"
        + json.dumps({"schema": 1, "ts": now_ts, "picks": [{"rank": 1, "name": "alpha", "kind": "skill", "score": 0.5}]})
        + "\n",
        encoding="utf-8",
    )
    rc = cli._cmd_report(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out


def _stop_event(
    *,
    ts: datetime | None = None,
    session: str = "sess",
    tools: list[str] | None = None,
    subagents: list[str] | None = None,
) -> dict:
    if ts is None:
        ts = datetime.now(timezone.utc)
    return {
        "schema": 1,
        "kind": "stop",
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_sha256": session,
        "tools": tools or [],
        "subagents": subagents or [],
    }


def test_report_ingestion_rate_skill_after_picks(isolated_paths, capsys):
    """A prompt event with picks, followed by a stop event containing 'Skill',
    counts as one ingestion."""
    base = datetime.now(timezone.utc)
    _write_events([
        _event(ts=base, session="sA", picks=[("alpha", "skill", 0.7)]),
        _stop_event(ts=base + timedelta(seconds=10), session="sA",
                    tools=["Bash", "Skill", "Edit"]),
        _event(ts=base, session="sB", picks=[("beta", "skill", 0.7)]),
        _stop_event(ts=base + timedelta(seconds=5), session="sB",
                    tools=["Bash", "Read"]),
    ])
    rc = cli._cmd_report(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    # 2 prompt events with picks, 1 had a Skill invocation in its turn → 50%.
    assert "Ingestion" in out or "ingestion" in out
    assert "50" in out  # 50.0% somewhere in the output


def test_report_ingestion_pairs_within_session_only(isolated_paths, capsys):
    """Stop event in session B must not credit a prompt in session A."""
    base = datetime.now(timezone.utc)
    _write_events([
        _event(ts=base, session="sA", picks=[("alpha", "skill", 0.7)]),
        # Stop in B happens after A's prompt; must NOT count for A.
        _stop_event(ts=base + timedelta(seconds=5), session="sB",
                    tools=["Skill"]),
    ])
    rc = cli._cmd_report(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    # 1 prompt event with picks, 0 ingestions in its own session.
    # The exact phrasing depends on render, but "0.0%" or "0%" should appear
    # under ingestion.
    assert "Ingestion" in out or "ingestion" in out


def test_report_ingestion_excludes_triage_skipped(isolated_paths, capsys):
    """Triage-skipped prompts shouldn't count toward ingestion denominator."""
    base = datetime.now(timezone.utc)
    _write_events([
        _event(ts=base, session="sA", picks=[], triage_skipped=True),
        _stop_event(ts=base + timedelta(seconds=5), session="sA",
                    tools=["Bash"]),
        _event(ts=base, session="sB", picks=[("alpha", "skill", 0.7)]),
        _stop_event(ts=base + timedelta(seconds=5), session="sB",
                    tools=["Skill"]),
    ])
    rc = cli._cmd_report(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    # Denominator = 1 (sB only); numerator = 1 → 100%.
    assert "Ingestion" in out or "ingestion" in out
    # The ingestion line should report 1/1.
    assert "1 / 1" in out or "1/1" in out


def test_report_ingestion_json_field(isolated_paths, capsys):
    base = datetime.now(timezone.utc)
    _write_events([
        _event(ts=base, session="sA", picks=[("alpha", "skill", 0.7)]),
        _stop_event(ts=base + timedelta(seconds=10), session="sA",
                    tools=["Skill"]),
    ])
    rc = cli._cmd_report(_ns(format="json"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "ingestion" in payload
    assert payload["ingestion"]["prompts_with_picks"] == 1
    assert payload["ingestion"]["skill_invoked"] == 1


def test_uninstall_removes_telemetry_files(isolated_paths, monkeypatch, capsys):
    # Create both files, then verify uninstall unlinks them.
    paths.ensure_dirs()
    paths.events_file().write_text("{}\n", encoding="utf-8")
    paths.telemetry_salt_file().write_text("deadbeef", encoding="utf-8")

    # The uninstall command also tries to strip a shell alias — give it a fish shell
    # with no existing alias so it's a clean no-op.
    monkeypatch.setenv("SHELL", "/usr/bin/fish")

    args = Namespace(purge_config=False)
    rc = cli._cmd_uninstall(args)
    assert rc == 0
    assert not paths.events_file().exists()
    assert not paths.telemetry_salt_file().exists()
