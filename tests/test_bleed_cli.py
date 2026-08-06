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


def _recent(minutes_ago: int) -> str:
    """A timestamp comfortably inside a `--since 7d` window."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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


def test_bleed_survives_a_structurally_damaged_row(isolated_paths, capsys):
    """End-to-end: one bad `tool_spans` used to raise straight out of the
    command. The healthy turn must still be reported and the damage disclosed."""
    rows = _pair(
        "2026-08-06T10:00:00Z",
        "2026-08-06T10:05:00Z",
        ["healthy"],
        [("Read", 10)],
        session="ok",
    )
    broken = _pair(
        "2026-08-06T11:00:00Z",
        "2026-08-06T11:05:00Z",
        ["hurt"],
        [("Read", 10)],
        session="bad",
    )
    broken[1]["tool_spans"] = [["Read"]]  # a 1-element pair: valid JSON, unusable
    _write_events(rows + broken)

    assert cli._cmd_bleed(_ns(min_n=1)) == 0
    out = capsys.readouterr().out
    assert "healthy" in out
    assert "malformed rows: 1" in out


def test_bleed_with_no_events_exits_zero_with_an_explanation(isolated_paths, capsys):
    assert cli._cmd_bleed(_ns()) == 0
    assert "no telemetry events" in capsys.readouterr().out


def test_bleed_discloses_skipped_rows_even_when_nothing_survives(
    isolated_paths, capsys
):
    """Fourth recurrence of the silent-drop class, same function: the
    empty-events early return fired BEFORE any disclosure, so three damaged
    rows reported as a clean empty log."""
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        fh.write("{not json\n[]\nnot json at all\n")

    assert cli._cmd_bleed(_ns()) == 0
    out = capsys.readouterr().out
    assert "skipped 3 unparseable rows" in out


def test_bleed_says_the_window_is_empty_not_that_the_log_is(isolated_paths, capsys):
    """`no telemetry events recorded.` under `--since` is a false statement when
    events ARE recorded and merely fall outside the window."""
    paths.ensure_dirs()
    with open(paths.events_file(), "w", encoding="utf-8") as fh:
        for row in _pair(
            "2020-01-01T10:00:00Z", "2020-01-01T10:05:00Z", ["s"], [("Read", 10)]
        ):
            fh.write(json.dumps(row) + "\n")
        fh.write("{not json\n")

    assert cli._cmd_bleed(_ns(since="7d")) == 0
    out = capsys.readouterr().out
    assert "no telemetry events recorded." not in out
    assert "7d" in out
    assert "2 row(s)" in out  # the out-of-window rows are counted, not hidden
    assert "skipped 1 unparseable rows" in out


def test_bleed_names_the_window_and_what_it_excluded(isolated_paths, capsys):
    """A non-empty windowed report must still say how much it left out — the
    difference between 'I use this rarely' and 'my window is too tight'."""
    _write_events(
        _pair(
            "2020-01-01T10:00:00Z",
            "2020-01-01T10:05:00Z",
            ["ancient-skill"],
            [("Read", 10)],
            session="a",
        )
        + _pair(_recent(60), _recent(30), ["fresh-skill"], [("Read", 10)], session="b")
    )
    assert cli._cmd_bleed(_ns(min_n=1, since="7d")) == 0
    out = capsys.readouterr().out
    assert "window: 7d" in out
    assert "outside the 7d window: 2 row(s)" in out
    assert "fresh-skill" in out and "ancient-skill" not in out


def test_bleed_counts_a_malformed_timestamp_under_a_since_window(
    isolated_paths, capsys
):
    """R5: a row whose ts will not parse must not vanish under --since. It is
    NOT out-of-window — it has no window position at all — so it falls through
    to pair_turns and is counted as malformed."""
    _write_events(
        _pair("2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z", ["s"], [("Read", 10)])
        + [{"kind": "prompt", "session_sha256": "z", "ts": "not-a-date"}]
    )
    cli._cmd_bleed(_ns(min_n=1, since="3650d"))
    assert "malformed rows: 1" in capsys.readouterr().out


def test_bleed_ranks_by_turn_time_when_no_turn_has_spans(isolated_paths, capsys):
    """R6: with every attributed_ms at 0, ranking by it degenerates to the
    alphabetical tie-break. Rank by the measurement that exists instead."""
    _write_events(
        _pair("2026-08-06T10:00:00Z", "2026-08-06T10:01:00Z", ["aaa-fast"], session="a")
        + _pair(
            "2026-08-06T11:00:00Z", "2026-08-06T11:30:00Z", ["zzz-slow"], session="b"
        )
    )
    cli._cmd_bleed(_ns(min_n=1))
    out = capsys.readouterr().out

    assert "no span data yet" in out
    # zzz-slow took 30 min vs aaa-fast's 1 min, so it must rank FIRST despite
    # sorting last alphabetically — this fails if the fallback rank is dropped.
    assert out.index("zzz-slow") < out.index("aaa-fast")
    # The span-derived columns are omitted, not printed as a row of zeros.
    assert "attributed" not in out


def test_bleed_says_the_tool_table_is_empty_for_want_of_spans(isolated_paths, capsys):
    _write_events(_pair("2026-08-06T10:00:00Z", "2026-08-06T10:05:00Z", ["s"]))
    cli._cmd_bleed(_ns(min_n=1))
    assert "no span data yet. Spans accrue from install" in capsys.readouterr().out


def test_bleed_is_registered_as_a_subcommand(isolated_paths):
    """The parser wiring is a separate failure mode from the handler."""
    args = cli._build_parser().parse_args(["bleed", "--min-n", "3"])
    assert args.func is cli._cmd_bleed
    assert args.min_n == 3
    assert args.idle_threshold == 120.0
    assert args.by == "both"
