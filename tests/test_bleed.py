from skill_advisor import bleed


def _prompt(session="s", ts="2026-08-06T10:00:00Z", **kw):
    return {"kind": "prompt", "session_sha256": session, "ts": ts, **kw}


def _stop(session="s", ts="2026-08-06T10:05:00Z", **kw):
    return {"kind": "stop", "session_sha256": session, "ts": ts,
            "tools": [], "subagents": [], "skills": [], **kw}


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
    turns, unpaired = bleed.pair_turns([_prompt(), _stop()])
    assert unpaired == 0
    assert len(turns) == 1
    assert turns[0].duration_s == 300.0


def test_a_legacy_row_without_a_kind_counts_as_a_prompt():
    """838 rows predate the `kind` key. Dropping them loses the oldest history."""
    legacy = {"session_sha256": "s", "ts": "2026-08-06T10:00:00Z"}
    turns, _ = bleed.pair_turns([legacy, _stop()])
    assert len(turns) == 1


def test_a_prompt_with_no_stop_is_counted_unpaired_not_dropped_silently():
    """run_stop returns early when the turn used no tools, so no stop event is
    written at all. Expected, but it must be visible in the count."""
    turns, unpaired = bleed.pair_turns([_prompt()])
    assert turns == []
    assert unpaired == 1


def test_turns_do_not_pair_across_sessions():
    turns, unpaired = bleed.pair_turns([_prompt(session="a"), _stop(session="b")])
    assert turns == []
    assert unpaired == 1


def test_a_second_prompt_before_a_stop_orphans_the_first():
    turns, unpaired = bleed.pair_turns([
        _prompt(ts="2026-08-06T10:00:00Z"),
        _prompt(ts="2026-08-06T10:01:00Z"),
        _stop(ts="2026-08-06T10:02:00Z"),
    ])
    assert unpaired == 1
    assert len(turns) == 1
    assert turns[0].duration_s == 60.0


def test_spans_are_carried_onto_the_turn_and_absent_ones_are_none():
    with_spans, _ = bleed.pair_turns([_prompt(), _stop(tool_spans=[["Read", 412]])])
    without, _ = bleed.pair_turns([_prompt(), _stop()])
    assert with_spans[0].spans == [("Read", 412)]
    assert without[0].spans is None


def test_an_unparseable_timestamp_does_not_crash_the_pairing():
    turns, unpaired = bleed.pair_turns([_prompt(ts="not-a-date"), _stop()])
    assert turns == []
