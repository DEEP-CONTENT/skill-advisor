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


def test_bleed_keeps_long_tool_names_distinguishable(isolated_paths, capsys):
    """`t.name[:24]` collapsed 18 distinct MCP tools on the live log into one
    visible name (`mcp__plugin_playwright_p`), so the TOOL table rendered rows
    with identical names and different numbers — and `--limit` was consumed by
    them. The discriminating part of such a name is its TAIL, so elide the
    middle instead of truncating."""
    click = "mcp__plugin_playwright_playwright__browser_click"
    close = "mcp__plugin_playwright_playwright__browser_close"
    _write_events(
        _pair(
            "2026-08-06T10:00:00Z",
            "2026-08-06T10:05:00Z",
            ["s"],
            [(click, 1000), (close, 2000)],
        )
    )
    cli._cmd_bleed(_ns(min_n=1))
    out = capsys.readouterr().out

    tool_rows = [
        line
        for line in out.splitlines()
        if line.startswith("mcp__") or line.startswith("mcp")
    ]
    assert len(tool_rows) == 2, out
    rendered = {line.split()[0] for line in tool_rows}
    assert len(rendered) == 2, f"two distinct tools rendered identically: {rendered}"
    # The distinguishing suffix survives, which is the whole point.
    assert "click" in out and "close" in out


def test_bleed_keeps_long_skill_names_distinguishable(isolated_paths, capsys):
    """Skills are safe only by luck today (the longest observed is exactly 42
    chars). Same elision, so the next long name does not silently collide."""
    a = "superpowers:a-very-long-prefix-shared-by-both-alpha"
    b = "superpowers:a-very-long-prefix-shared-by-both-bravo"
    _write_events(
        _pair(
            "2026-08-06T10:00:00Z",
            "2026-08-06T10:05:00Z",
            [a],
            [("Read", 10)],
            session="a",
        )
        + _pair(
            "2026-08-06T11:00:00Z",
            "2026-08-06T11:20:00Z",
            [b],
            [("Read", 20)],
            session="b",
        )
    )
    cli._cmd_bleed(_ns(min_n=1))
    out = capsys.readouterr().out
    rendered = {
        line.split()[0] for line in out.splitlines() if line.startswith("superpowers:")
    }
    assert len(rendered) == 2, f"two distinct skills rendered identically: {rendered}"
    assert "alpha" in out and "bravo" in out


def test_bleed_json_says_which_key_it_ranked_by(isolated_paths, capsys):
    """A machine consumer reads array order as a cost ranking. On the first-run
    path the order comes from `p50_turn_s`, not `attributed_ms`, and nothing in
    the payload said so."""
    _write_events(
        _pair("2026-08-06T10:00:00Z", "2026-08-06T10:01:00Z", ["a"], session="a")
        + _pair("2026-08-06T11:00:00Z", "2026-08-06T11:30:00Z", ["b"], session="b")
    )
    cli._cmd_bleed(_ns(min_n=1, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["ranked_by"] == "p50_turn_s"

    _write_events(
        _pair("2026-08-06T10:00:00Z", "2026-08-06T10:01:00Z", ["a"], [("Read", 10)])
    )
    cli._cmd_bleed(_ns(min_n=1, json=True))
    assert json.loads(capsys.readouterr().out)["ranked_by"] == "attributed_ms"


def test_bleed_json_arrays_are_complete_and_say_so(isolated_paths, capsys):
    """`--limit` shapes the human tables only. The JSON arrays stay complete —
    a machine consumer that got a silently truncated array would have no way to
    know — and `limit_applied` states it rather than leaving it implied."""
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
    cli._cmd_bleed(_ns(min_n=1, limit=2, json=True))
    payload = json.loads(capsys.readouterr().out)

    assert payload["limit_applied"] is False
    assert len(payload["skills"]) == 5, "the JSON arrays must not be truncated"


def test_span_coverage_counts_a_real_collector_turn_and_not_a_one_tool_turn(
    monkeypatch, isolated_paths, capsys
):
    """Collector -> events.jsonl -> report, end to end.

    The coverage invariant BLOCKING 1 broke lives across this whole chain and
    at no single point in it: a one-tool turn used to be written with
    `[["X", 0]]` and counted as span-carrying, so `spans: N of M` claimed
    measurement the report did not have. Only the stop rows are real here —
    the prompt rows are synthesised against the session hash the hooks
    actually wrote, because pairing needs a partner and the UserPromptSubmit
    path is a different subsystem.

    Goes RED if the collector stops appending marks, if `run_stop` stops
    emitting spans, if the first tool regains one (the one-tool turn would
    then count as covered, making it 2 of 2), or if `span_coverage` stops
    keying on a non-empty list.
    """
    import io
    import time
    from skill_advisor import hook, paths

    config_path = paths.config_file()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[telemetry]\nevents_enabled = true\n", encoding="utf-8")

    def _drive(session, tools):
        for tool in tools:
            monkeypatch.setattr(
                "sys.stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "session_id": session,
                            "tool_name": tool,
                            "tool_input": {},
                        }
                    )
                ),
            )
            assert hook.run_posttooluse() == 0
            time.sleep(0.02)
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"session_id": session}))
        )
        assert hook.run_stop() == 0

    _drive("many", ["Read", "Bash", "Edit"])
    _drive("solo", ["Read"])

    stops = [
        json.loads(line)
        for line in paths.events_file().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(stops) == 2
    rows = []
    for i, stop in enumerate(stops):
        stop["skills"] = ["measured"]
        rows.append(
            {
                "kind": "prompt",
                "session_sha256": stop["session_sha256"],
                "ts": f"2026-08-06T1{i}:00:00Z",
            }
        )
        stop["ts"] = f"2026-08-06T1{i}:05:00Z"
        rows.append(stop)
    _write_events(rows)

    assert cli._cmd_bleed(_ns(min_n=1)) == 0
    out = capsys.readouterr().out
    assert "spans: 1 of 2" in out, out
    # The three-tool turn contributed two spans, to Bash and Edit — never Read.
    assert "Bash" in out and "Edit" in out
    tool_names = {
        line.split()[0]
        for line in out.split("TOOL")[1].splitlines()[1:]
        if line.strip() and not line.startswith(" ")
    }
    assert tool_names == {"Bash", "Edit"}, tool_names


def test_bleed_is_registered_as_a_subcommand(isolated_paths):
    """The parser wiring is a separate failure mode from the handler."""
    args = cli._build_parser().parse_args(["bleed", "--min-n", "3"])
    assert args.func is cli._cmd_bleed
    assert args.min_n == 3
    assert args.idle_threshold == 120.0
    assert args.by == "both"
