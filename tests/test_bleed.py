from datetime import datetime, timedelta, timezone

import pytest

from skill_advisor import bleed


def _prompt(session="s", ts="2026-08-06T10:00:00Z", **kw):
    return {"kind": "prompt", "session_sha256": session, "ts": ts, **kw}


def _stop(session="s", ts="2026-08-06T10:05:00Z", **kw):
    return {
        "kind": "stop",
        "session_sha256": session,
        "ts": ts,
        "tools": [],
        "subagents": [],
        "skills": [],
        **kw,
    }


# --- the idle rule -------------------------------------------------------


def test_a_short_span_is_charged_in_full():
    a = bleed.attribute([("Read", 5_000)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 5_000}
    assert a.idle_ms == 0
    assert a.idle_gaps == 0


def test_a_span_exactly_at_the_threshold_is_still_charged_in_full():
    """The boundary is <=, not <. A rule nobody pinned is a rule nobody keeps."""
    a = bleed.attribute([("Read", 120_000)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 120_000}
    assert a.idle_ms == 0


def test_a_long_span_is_capped_and_the_excess_spills_to_idle():
    a = bleed.attribute([("Bash", 300_000)], threshold_ms=120_000)
    assert a.per_tool == {"Bash": 120_000}
    assert a.idle_ms == 180_000
    assert a.idle_gaps == 1


def test_a_negative_span_is_clamped_to_zero():
    """A clock adjustment mid-turn must never produce a negative total."""
    a = bleed.attribute([("Read", -4_000)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 0}
    assert a.idle_ms == 0


def test_repeated_tools_accumulate():
    a = bleed.attribute([("Read", 100), ("Read", 250)], threshold_ms=120_000)
    assert a.per_tool == {"Read": 350}


# --- turn pairing --------------------------------------------------------


def test_a_prompt_pairs_with_the_next_stop_in_its_session():
    turns, unpaired, malformed = bleed.pair_turns([_prompt(), _stop()])
    assert unpaired == 0
    assert len(turns) == 1
    assert turns[0].duration_s == 300.0


def test_a_legacy_row_without_a_kind_counts_as_a_prompt():
    """838 rows predate the `kind` key. Dropping them loses the oldest history."""
    legacy = {"session_sha256": "s", "ts": "2026-08-06T10:00:00Z"}
    turns, _, _ = bleed.pair_turns([legacy, _stop()])
    assert len(turns) == 1


def test_a_prompt_with_no_stop_is_counted_unpaired_not_dropped_silently():
    """run_stop returns early when the turn used no tools, so no stop event is
    written at all. Expected, but it must be visible in the count."""
    turns, unpaired, malformed = bleed.pair_turns([_prompt()])
    assert turns == []
    assert unpaired == 1


def test_turns_do_not_pair_across_sessions():
    turns, unpaired, _ = bleed.pair_turns([_prompt(session="a"), _stop(session="b")])
    assert turns == []
    assert unpaired == 1


def test_a_second_prompt_before_a_stop_orphans_the_first():
    turns, unpaired, _ = bleed.pair_turns(
        [
            _prompt(ts="2026-08-06T10:00:00Z"),
            _prompt(ts="2026-08-06T10:01:00Z"),
            _stop(ts="2026-08-06T10:02:00Z"),
        ]
    )
    assert unpaired == 1
    assert len(turns) == 1
    assert turns[0].duration_s == 60.0


def test_spans_are_carried_onto_the_turn_and_absent_ones_are_none():
    with_spans, _, _ = bleed.pair_turns([_prompt(), _stop(tool_spans=[["Read", 412]])])
    without, _, _ = bleed.pair_turns([_prompt(), _stop()])
    assert with_spans[0].spans == [("Read", 412)]
    assert without[0].spans is None


def test_an_unparseable_timestamp_is_counted_malformed_not_dropped():
    """A malformed row must not vanish from every output. It is counted
    separately from `unpaired`, which means the legitimate no-tools case."""
    turns, unpaired, malformed = bleed.pair_turns([_prompt(ts="not-a-date"), _stop()])
    assert turns == []
    assert unpaired == 0
    assert malformed == 1


DAMAGED_SPAN_SHAPES = [
    ("one-element pair", [["Read"]]),
    ("three-element pair", [["Read", 1, 2]]),
    ("non-numeric ms", [["Read", "soon"]]),
    ("null ms", [["Read", None]]),
    ("list of bare ints", [1, 2, 3]),
    ("list of bare strings", ["ab", "cd"]),
    ("nested list as ms", [["Read", [1]]]),
    ("infinite ms", [["Read", float("inf")]]),
]


@pytest.mark.parametrize(
    "label,shape", DAMAGED_SPAN_SHAPES, ids=[s[0] for s in DAMAGED_SPAN_SHAPES]
)
def test_a_damaged_tool_spans_value_does_not_abort_the_report(label, shape):
    """Every shape below is valid JSON in a valid dict, and every one of them
    used to raise out of `pair_turns` — killing the WHOLE report over one row.
    `_load_events_counting_failures` goes to real trouble to count one shape of
    damage; dying on another makes that pointless.

    The turn survives (its timestamps are fine, so turn-level stats keep it),
    the spans degrade to span-less, and the damage is folded into the EXISTING
    `malformed` counter, which is already returned and already printed.
    """
    turns, unpaired, malformed = bleed.pair_turns([_prompt(), _stop(tool_spans=shape)])
    assert len(turns) == 1, label
    assert turns[0].spans is None, label
    assert malformed == 1, label


def test_a_span_shape_that_is_not_a_list_stays_span_less_without_counting_damage():
    """A dict or a string already degraded correctly; keep it that way rather
    than inflating the damage count with shapes that were never a crash."""
    for shape in ({"Read": 1}, "Read", 7, None):
        turns, _, malformed = bleed.pair_turns([_prompt(), _stop(tool_spans=shape)])
        assert turns[0].spans is None, shape
        assert malformed == 0, shape


def test_a_non_hashable_session_id_does_not_abort_the_report():
    """`session_sha256` keys the pairing map, so a list there raised
    `TypeError: unhashable type` before a single turn was built."""
    turns, _, malformed = bleed.pair_turns(
        [_prompt(session=["a", "b"]), _prompt(), _stop()]
    )
    assert len(turns) == 1  # the well-formed pair still reports
    assert malformed == 1


def test_a_non_iterable_skills_or_tools_value_does_not_abort_the_report():
    """`[str(s) for s in row["skills"]]` raised `TypeError: 'int' object is not
    iterable`. Damage in one field must not cost the whole run."""
    turns, _, malformed = bleed.pair_turns([_prompt(), _stop(skills=3, tools=3)])
    assert len(turns) == 1
    assert turns[0].skills == [] and turns[0].tools == []
    assert malformed == 1  # counted ONCE per row, not once per damaged field


def test_out_of_order_rows_are_sorted_before_pairing():
    """Deleting the rows.sort() must fail a test. Every other fixture supplies
    prompt-then-stop in file order, so the sort is otherwise a no-op."""
    turns, _, _ = bleed.pair_turns([_stop(), _prompt()])
    assert len(turns) == 1
    assert turns[0].duration_s == 300.0


# --- skill and tool statistics ------------------------------------------


def _turn(seconds, skills=(), spans=None):
    """Build a Turn directly — these tests exercise the statistics, not pairing."""
    start = datetime(2026, 8, 6, 10, 0, 0, tzinfo=timezone.utc)
    return bleed.Turn(
        session="s",
        start=start,
        stop=start + timedelta(seconds=seconds),
        skills=list(skills),
        tools=[n for n, _ in (spans or [])],
        spans=spans,
    )


def test_skill_stats_rank_by_total_attributed_time():
    turns = [
        _turn(600, skills=["slow"], spans=[("Bash", 300_000)]),
        _turn(60, skills=["fast"], spans=[("Read", 10_000)]),
        _turn(60, skills=["fast"], spans=[("Read", 10_000)]),
    ]
    stats, below = skill_stats_of(turns, min_n=1)
    assert [s.name for s in stats] == ["slow", "fast"]
    assert stats[0].attributed_ms == 120_000  # capped at the threshold
    assert stats[0].idle_ms == 180_000
    assert stats[1].attributed_ms == 20_000
    assert below == 0


def test_a_skill_below_min_n_is_counted_not_dropped():
    """Task 13 of the catalog-refresh branch shipped a dry run that silently
    hid rows. Counted-but-not-ranked is the rule here."""
    turns = [
        _turn(60, skills=["rare"], spans=[("Read", 1_000)]),
        _turn(60, skills=["common"], spans=[("Read", 1_000)]),
        _turn(60, skills=["common"], spans=[("Read", 1_000)]),
    ]
    stats, below = skill_stats_of(turns, min_n=2)
    assert [s.name for s in stats] == ["common"]
    assert below == 1


def test_a_turn_invoking_two_skills_counts_toward_both():
    turns = [_turn(60, skills=["a", "b"], spans=[("Read", 4_000)])]
    stats, _ = skill_stats_of(turns, min_n=1)
    assert {s.name for s in stats} == {"a", "b"}
    assert all(s.attributed_ms == 4_000 for s in stats)


def test_turns_with_idle_counts_turns_not_gaps():
    turns = [_turn(600, skills=["s"], spans=[("Bash", 300_000), ("Bash", 300_000)])]
    stats, _ = skill_stats_of(turns, min_n=1)
    assert stats[0].turns_with_idle == 1


def test_skill_p50_turn_is_the_upper_median_of_every_turn():
    """A confirmed live mutation survivor: swapping `_p50` for `min()` in the
    skill path left the whole suite green, because every existing fixture gives
    a skill exactly ONE turn — where min, mean, max and p50 all coincide.

    This field is displayed by the CLI and is the primary sort key on the
    first-run (no-spans) path, so a wrong value silently reorders the report.

    Four turns of 60/120/300/900 s separate every plausible substitution:
      min 60 · lower median 120 · UPPER MEDIAN 300 · mean 345 · max 900
    """
    turns = [_turn(s, skills=["multi"]) for s in (900, 60, 300, 120)]
    stats, _ = skill_stats_of(turns, min_n=1)
    assert stats[0].n == 4
    assert stats[0].p50_turn_s == 300.0


def test_a_span_less_turn_still_counts_toward_n_but_adds_no_time():
    turns = [
        _turn(600, skills=["s"], spans=None),
        _turn(60, skills=["s"], spans=[("Read", 5_000)]),
    ]
    stats, _ = skill_stats_of(turns, min_n=1)
    assert stats[0].n == 2
    assert stats[0].attributed_ms == 5_000


def test_tool_stats_aggregate_calls_and_p50():
    turns = [_turn(60, spans=[("Read", 100), ("Read", 300), ("Bash", 50_000)])]
    stats = bleed.tool_stats(turns, threshold_ms=120_000)
    by_name = {s.name: s for s in stats}
    assert by_name["Read"].calls == 2
    assert by_name["Read"].p50_ms == 300  # upper median of [100, 300]
    assert by_name["Bash"].attributed_ms == 50_000
    assert [s.name for s in stats] == ["Bash", "Read"]


def test_tool_stats_calls_counts_invocations_not_spans():
    """`calls` and `spans` are different populations and must not be conflated.

    A span is "time since the previous tool finished" (see `run_stop` in
    hook.py), so a turn's FIRST tool never emits one. Here `tools` lists three
    real invocations — Skill, Bash, Skill — but `spans` holds only two: Bash's
    span ("since the leading Skill finished") and the second Skill's span
    ("since Bash finished"). The leading Skill call has no span of its own.

    A conflated implementation that derives `calls` from `len(samples[name])`
    (the old bug) reports Skill's `calls` as 1, not 2 — this assertion is
    exactly what goes RED under that code, which is why this test exists:
    `Skill` is the tool most often first in a turn, so it is the one this bug
    hurts worst (measured 72% under-count on the live log)."""
    start = datetime(2026, 8, 6, 10, 0, 0, tzinfo=timezone.utc)
    turn = bleed.Turn(
        session="s",
        start=start,
        stop=start + timedelta(seconds=60),
        skills=["multi"],
        tools=["Skill", "Bash", "Skill"],
        spans=[("Bash", 1_000), ("Skill", 2_000)],
    )
    stats = bleed.tool_stats([turn], threshold_ms=120_000)
    by_name = {s.name: s for s in stats}

    assert by_name["Skill"].calls == 2, "real invocations, not span count"
    assert by_name["Skill"].spans == 1, "measured spans, not invocation count"
    assert by_name["Bash"].calls == 1
    assert by_name["Bash"].spans == 1


def test_tool_p50_uses_capped_values_not_raw():
    """One row, one meaning. Without this the fixture spans all sit under the
    threshold and nothing discriminates capped from raw."""
    turns = [_turn(600, spans=[("Bash", 300_000), ("Bash", 300_000)])]
    stats = bleed.tool_stats(turns, threshold_ms=120_000)
    assert stats[0].p50_ms == 120_000
    assert stats[0].attributed_ms == 240_000


def test_span_coverage_reports_both_populations():
    turns = [
        _turn(60, spans=[("Read", 1)]),
        _turn(60, spans=None),
        _turn(60, spans=None),
    ]
    assert bleed.span_coverage(turns) == (1, 3)


def skill_stats_of(turns, *, min_n):
    return bleed.skill_stats(turns, threshold_ms=120_000, min_n=min_n)
