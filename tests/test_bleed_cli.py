"""Tests for the `skill-advisor bleed` CLI subcommand."""

from __future__ import annotations

import json
from argparse import Namespace

from skill_advisor import cli, paths


def _ns(**overrides) -> Namespace:
    base = dict(
        since=None,
        min_n=5,
        idle_threshold=120.0,
        by="both",
        limit=20,
        json=False,
    )
    base.update(overrides)
    return Namespace(**base)


def _write_events(rows):
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _pair(ts_a, ts_b, skills, spans=None, session="s"):
    stop = {
        "kind": "stop",
        "session_sha256": session,
        "ts": ts_b,
        "tools": [n for n, _ in (spans or [])],
        "subagents": [],
        "skills": skills,
    }
    if spans:
        stop["tool_spans"] = [[n, ms] for n, ms in spans]
    return [{"kind": "prompt", "session_sha256": session, "ts": ts_a}, stop]


def test_bleed_reports_a_skill_ranking(isolated_paths, capsys):
    _write_events(
        _pair(
            "2026-08-06T10:00:00Z",
            "2026-08-06T10:10:00Z",
            ["slow"],
            [("Bash", 300_000)],
        )
        + _pair(
            "2026-08-06T11:00:00Z",
            "2026-08-06T11:01:00Z",
            ["fast"],
            [("Read", 9_000)],
            "t",
        )
    )
    assert cli._cmd_bleed(_ns(min_n=1)) == 0
    out = capsys.readouterr().out

    assert "slow" in out and "fast" in out
    assert out.index("slow") < out.index("fast")


def test_bleed_always_prints_span_coverage(isolated_paths, capsys):
    """Turn-level and per-tool tables are different populations. Never let them
    look like one."""
    _write_events(_pair("2026-08-06T10:00:00Z", "2026-08-06T10:10:00Z", ["s"]))
    cli._cmd_bleed(_ns(min_n=1))
    assert "spans:" in capsys.readouterr().out


def test_bleed_discloses_below_min_n_skills(isolated_paths, capsys):
    _write_events(
        _pair("2026-08-06T10:00:00Z", "2026-08-06T10:01:00Z", ["rare"], [("Read", 5)])
    )
    cli._cmd_bleed(_ns(min_n=5))
    out = capsys.readouterr().out
    assert "below n=5" in out
    assert "not ranked" in out


def test_bleed_limit_prints_an_explicit_overflow_count(isolated_paths, capsys):
    rows = []
    for i in range(5):
        rows += _pair(
            f"2026-08-06T1{i}:00:00Z",
            f"2026-08-06T1{i}:05:00Z",
            [f"skill-{i}"],
            [("Read", (i + 1) * 1000)],
            session=f"s{i}",
        )
    _write_events(rows)
    cli._cmd_bleed(_ns(min_n=1, limit=2))
    assert "+3 more" in capsys.readouterr().out


def test_bleed_counts_unparseable_rows_instead_of_hiding_them(isolated_paths, capsys):
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        fh.write("{not json\n")
        for row in _pair(
            "2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z", ["s"], [("Read", 10)]
        ):
            fh.write(json.dumps(row) + "\n")
    cli._cmd_bleed(_ns(min_n=1))
    assert "skipped 1" in capsys.readouterr().out


def test_bleed_counts_unparseable_rows_under_a_since_window_too(isolated_paths, capsys):
    """Global constraint: every excluded count is printed. A windowed read must
    not report 0 skipped just because subtraction would be meaningless there."""
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        fh.write("{not json\n")
        for row in _pair(
            "2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z", ["s"], [("Read", 10)]
        ):
            fh.write(json.dumps(row) + "\n")
    cli._cmd_bleed(_ns(min_n=1, since="3650d"))
    assert "skipped 1" in capsys.readouterr().out


def test_bleed_threshold_flag_changes_the_attribution(isolated_paths, capsys):
    """The whole reason the stored form is raw: history is re-readable at a
    different threshold."""
    _write_events(
        _pair(
            "2026-08-06T10:00:00Z", "2026-08-06T10:10:00Z", ["s"], [("Bash", 200_000)]
        )
    )

    cli._cmd_bleed(_ns(min_n=1, idle_threshold=300.0, json=True))
    wide = json.loads(capsys.readouterr().out)
    cli._cmd_bleed(_ns(min_n=1, idle_threshold=60.0, json=True))
    tight = json.loads(capsys.readouterr().out)

    assert wide["skills"][0]["attributed_ms"] == 200_000
    assert tight["skills"][0]["attributed_ms"] == 60_000
    assert tight["skills"][0]["idle_ms"] == 140_000


def test_bleed_reports_malformed_rows_separately_from_unpaired(isolated_paths, capsys):
    """Data damage and the legitimate no-tools case are different things and
    must not share a counter."""
    _write_events(
        _pair("2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z", ["s"], [("Read", 10)])
        + [{"kind": "prompt", "session_sha256": "z", "ts": "not-a-date"}]
    )
    cli._cmd_bleed(_ns(min_n=1))
    out = capsys.readouterr().out
    assert "malformed rows: 1" in out


def test_bleed_with_no_events_exits_zero_with_an_explanation(isolated_paths, capsys):
    assert cli._cmd_bleed(_ns()) == 0
    assert "no telemetry events" in capsys.readouterr().out


def test_bleed_is_registered_as_a_subcommand(isolated_paths):
    """The parser wiring is a separate failure mode from the handler."""
    args = cli._build_parser().parse_args(["bleed", "--min-n", "3"])
    assert args.func is cli._cmd_bleed
    assert args.min_n == 3
    assert args.idle_threshold == 120.0
    assert args.by == "both"
