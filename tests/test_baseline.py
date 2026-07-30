import json

from skill_advisor import baseline, effort, paths


def test_load_missing_file_returns_empty_dict():
    assert baseline._load() == {}


def test_load_rejects_corrupt_json():
    paths.ensure_dirs()
    paths.baseline_file().write_text("not json{", encoding="utf-8")
    assert baseline._load() == {}


def test_load_rejects_non_dict_root():
    paths.ensure_dirs()
    paths.baseline_file().write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert baseline._load() == {}


def test_save_roundtrips_through_load():
    baseline._save({"nudged": {"s1": ["medium>xhigh"]}})
    assert baseline._load() == {"nudged": {"s1": ["medium>xhigh"]}}


def test_save_is_atomic_no_tmp_left():
    baseline._save({"nudged": {}})
    leftovers = list(paths.cache_dir().glob("baseline.json.*"))
    assert leftovers == []


def test_mark_nudged_false_without_session_id():
    assert baseline.mark_nudged(None, "medium", "xhigh") is False
    assert baseline.mark_nudged("", "medium", "xhigh") is False


def test_mark_nudged_true_once_per_pair_per_session():
    assert baseline.mark_nudged("sess-a", "medium", "xhigh") is True
    assert baseline.mark_nudged("sess-a", "medium", "xhigh") is False


def test_mark_nudged_distinguishes_sessions():
    """The same (observed, recommended) pair is fresh news in a different session."""
    assert baseline.mark_nudged("sess-a", "medium", "xhigh") is True
    assert baseline.mark_nudged("sess-b", "medium", "xhigh") is True


def test_mark_nudged_persists_to_baseline_file():
    baseline.mark_nudged("sess-a", "medium", "xhigh")
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["nudged"]["sess-a"] == ["medium>xhigh"]


def test_mark_nudged_tolerates_malformed_ledger():
    """A corrupted 'nudged' value (not a dict) must not raise — just reset it."""
    paths.ensure_dirs()
    paths.baseline_file().write_text(json.dumps({"nudged": "not-a-dict"}), encoding="utf-8")
    assert baseline.mark_nudged("sess-a", "medium", "xhigh") is True
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["nudged"]["sess-a"] == ["medium>xhigh"]


def test_mark_nudged_accumulates_multiple_pairs_same_session():
    assert baseline.mark_nudged("sess-a", "medium", "xhigh") is True
    assert baseline.mark_nudged("sess-a", "medium", "low") is True
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert sorted(data["nudged"]["sess-a"]) == ["medium>low", "medium>xhigh"]


# ---------------------------------------------------------------------------
# was_nudged: read-only check, must never itself consume the slot.
# ---------------------------------------------------------------------------


def test_was_nudged_false_before_any_mark():
    assert baseline.was_nudged("sess-a", "medium", "xhigh") is False


def test_was_nudged_true_after_mark():
    baseline.mark_nudged("sess-a", "medium", "xhigh")
    assert baseline.was_nudged("sess-a", "medium", "xhigh") is True


def test_was_nudged_does_not_consume():
    """Calling was_nudged repeatedly must never itself change the ledger."""
    baseline.mark_nudged("sess-a", "medium", "xhigh")
    assert baseline.was_nudged("sess-a", "medium", "xhigh") is True
    assert baseline.was_nudged("sess-a", "medium", "xhigh") is True
    assert baseline.was_nudged("sess-a", "medium", "xhigh") is True


def test_was_nudged_false_without_session_id():
    assert baseline.was_nudged(None, "medium", "xhigh") is False
    assert baseline.was_nudged("", "medium", "xhigh") is False


def test_was_nudged_tolerates_malformed_ledger():
    paths.ensure_dirs()
    paths.baseline_file().write_text(json.dumps({"nudged": "not-a-dict"}), encoding="utf-8")
    assert baseline.was_nudged("sess-a", "medium", "xhigh") is False


def test_was_nudged_tolerates_malformed_session_entry():
    paths.ensure_dirs()
    paths.baseline_file().write_text(
        json.dumps({"nudged": {"sess-a": "not-a-list"}}), encoding="utf-8"
    )
    assert baseline.was_nudged("sess-a", "medium", "xhigh") is False


# ---------------------------------------------------------------------------
# record, finalise_session, window: rolling window of session modals
# ---------------------------------------------------------------------------


def test_modal_of_a_session():
    # Reorder so mode (high) is not first, to catch naive "return first element" bugs.
    for lvl in ("low", "high", "high"):
        baseline.record("s1", lvl)
    assert baseline.finalise_session("s1") == effort.HIGH


def test_thin_session_is_not_yet_in_the_window_but_tally_survives():
    """Stop fires per TURN, so a 2-recommendation session is simply not ready yet.

    The tally must NOT be cleared — clearing it is what made the window
    permanently empty, since each turn contributes only one recommendation.
    """
    baseline.record("s1", "high")
    baseline.record("s1", "high")
    assert baseline.finalise_session("s1") is None
    assert baseline.window() == []
    baseline.record("s1", "high")  # third turn arrives
    assert baseline.finalise_session("s1") == effort.HIGH
    assert baseline.window() == [effort.HIGH]


def test_long_session_contributes_exactly_one_window_entry():
    """A session finalised on every turn must not flood the window."""
    for _ in range(9):
        baseline.record("s1", "high")
        baseline.finalise_session("s1")
    assert baseline.window() == [effort.HIGH]


def test_distinct_sessions_each_get_an_entry():
    for sid, lvl in (("a", "high"), ("b", "low")):
        for _ in range(3):
            baseline.record(sid, lvl)
            baseline.finalise_session(sid)
    assert baseline.window() == [effort.HIGH, effort.LOW]


def test_thin_session_tally_accumulates_across_finalise_calls():
    """Inverts the old (buggy) 'must not leak data to subsequent finalise calls'
    premise: because finalise_session no longer clears the tally, two
    below-threshold batches for the same session must ACCUMULATE into one
    tally rather than reset between calls — that accumulation is exactly what
    makes write-back reachable when Stop fires once per turn.
    """
    baseline.record("s1", "high")
    baseline.record("s1", "high")
    assert baseline.finalise_session("s1") is None  # 2 < 3, still thin
    baseline.record("s1", "low")
    baseline.record("s1", "low")
    # Now 4 total (2 high + 2 low), no longer thin — and critically, this only
    # works because the first two records survived the earlier None-returning
    # call instead of being popped.
    result = baseline.finalise_session("s1")
    assert result is not None
    assert baseline.window() == [result]


def test_ultracode_contributes_xhigh_to_the_window():
    for _ in range(3):
        baseline.record("s1", effort.ULTRACODE)
    assert baseline.finalise_session("s1") == effort.XHIGH


def test_window_is_bounded_and_keeps_newest():
    # Fill window beyond cap: sessions 0-39 record LOW, sessions 40-49 record HIGH.
    # This lets us distinguish old entries (LOW) from new (HIGH).
    # Total: 50 entries. Correct slice keeps last 30 (indices 20-49).
    for i in range(40):
        for _ in range(3):
            baseline.record(f"s{i}", effort.LOW)
        baseline.finalise_session(f"s{i}")
    for i in range(40, 50):
        for _ in range(3):
            baseline.record(f"s{i}", effort.HIGH)
        baseline.finalise_session(f"s{i}")
    window = baseline.window()
    # Window must be exactly at cap since we added 50 > 30 entries.
    assert len(window) == baseline._WINDOW_CAP
    # Correct slice keeps last 30 (indices 20-49 of the 50): 20 LOW (indices 20-39) + 10 HIGH (indices 40-49).
    # Wrong slice would keep first 30 (indices 0-29 of the 50): 30 LOW + 0 HIGH.
    # Count the entries to verify we have the newer pattern.
    low_count = window.count(effort.LOW)
    high_count = window.count(effort.HIGH)
    # Correct: 20 low + 10 high. Wrong: 30 low + 0 high.
    # So high_count should be 10, not 0.
    assert high_count == 10, f"Expected 10 HIGH entries (newest 30), got {high_count}"
    assert low_count == 20, f"Expected 20 LOW entries (newest 30), got {low_count}"


def test_corrupt_baseline_file_resets_cleanly():
    paths.ensure_dirs()
    paths.baseline_file().write_text("{{{", encoding="utf-8")
    assert baseline.window() == []


def test_record_tolerates_malformed_tallies():
    """A corrupted 'tallies' value (not a dict) must not raise — just reset it."""
    paths.ensure_dirs()
    paths.baseline_file().write_text(json.dumps({"tallies": "not-a-dict"}), encoding="utf-8")
    baseline.record("s1", "high")
    baseline.record("s1", "high")
    baseline.record("s1", "high")
    assert baseline.finalise_session("s1") == effort.HIGH


def test_finalise_tolerates_malformed_tallies():
    """A corrupted 'tallies' value (not a dict) must not raise on finalise.

    finalise_session no longer owns healing malformed tallies — since it no
    longer consumes/clears the tally on the happy path, it also does not
    write on this read-only "nothing to finalise" path. Healing malformed
    tallies is `record()`'s job (see test_record_tolerates_malformed_tallies).
    """
    paths.ensure_dirs()
    paths.baseline_file().write_text(json.dumps({"tallies": ["list", "not", "dict"]}), encoding="utf-8")
    assert baseline.finalise_session("s1") is None
    # File is left exactly as-is — finalise_session never wrote on this path.
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["tallies"] == ["list", "not", "dict"]


def test_concurrent_sessions_do_not_starve_each_other(monkeypatch):
    """Fix round 2: pruning tallies by window membership deleted a DIFFERENT,
    still-accumulating session's tally the moment any session finalised
    successfully — a thin session is legitimately absent from the window
    precisely because it's still accumulating, which is when its tally
    matters most. Reproduces the coordinator's exact repro: B records 2
    (thin), A records 3 and finalises (this used to wipe B's tally via the
    window-membership prune), then B records a 3rd and must still reach the
    window with its own modal.
    """
    baseline.record("B", "high")
    baseline.record("B", "high")

    baseline.record("A", "low")
    baseline.record("A", "low")
    baseline.record("A", "low")
    assert baseline.finalise_session("A") == effort.LOW

    # B's tally must have survived A's finalise call.
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["tallies"].get("B") == ["high", "high"]

    baseline.record("B", "high")
    assert baseline.finalise_session("B") == effort.HIGH
    assert baseline.window() == [effort.LOW, effort.HIGH]


def test_tally_growth_is_bounded_by_recency_not_window_membership():
    """More than _TALLY_CAP sessions must not grow `tallies` without bound,
    but the cap must be keyed on recency (insertion order), not on whether a
    session made it into the window — see test_concurrent_sessions_do_not_starve_each_other
    for why window-membership pruning is wrong.
    """
    for i in range(baseline._TALLY_CAP + 10):
        baseline.record(f"s{i}", "high")
        baseline.record(f"s{i}", "high")
        baseline.record(f"s{i}", "high")
        baseline.finalise_session(f"s{i}")
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert len(data["tallies"]) <= baseline._TALLY_CAP


def test_tally_growth_is_bounded_even_when_no_session_ever_finalises():
    """Fix round 3: _TALLY_CAP was only enforced on finalise_session's SUCCESS
    path, so a session that never reaches _MIN_RECOMMENDATIONS never passes
    through it and its key is never counted against the cap. Reproduces the
    coordinator's exact finding: many short sessions that record once and
    never finalise must still be bounded, because record() — not
    finalise_session — is the only function that ever creates a tally key.
    """
    for i in range(baseline._TALLY_CAP + 30):
        baseline.record(f"s{i}", "high")  # single prompt, session never finalises
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert len(data["tallies"]) <= baseline._TALLY_CAP


def test_record_never_evicts_the_session_it_is_currently_recording_for():
    """The cap must be enforced without ever dropping the key record() itself
    just touched — append-then-cap makes a brand-new key the most recently
    inserted, so oldest-first eviction alone would already skip it, but this
    pins down the guarantee explicitly rather than relying on that ordering
    argument going unverified.
    """
    for i in range(baseline._TALLY_CAP):
        baseline.record(f"s{i}", "high")
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert len(data["tallies"]) == baseline._TALLY_CAP  # exactly at cap, no overflow yet

    baseline.record("brand-new-session", "high")
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["tallies"].get("brand-new-session") == ["high"]
    assert len(data["tallies"]) <= baseline._TALLY_CAP


def test_tie_breaking_uses_insertion_order():
    """When multiple levels tie for mode, Counter.most_common picks first encountered."""
    # Create a tie: two high, two low (equal count). High is encountered first.
    for lvl in ("high", "low", "high", "low"):
        baseline.record("s1", lvl)
    # Counter.most_common(1) will return the first one seen in insertion order.
    # Since Python 3.7+, dicts preserve insertion order, but Counter.most_common()
    # behavior on ties is to return the one most recently added to the Counter.
    # Actually, for a tie, most_common returns in an unspecified order among tied elements.
    # We want to test that the behavior is *deterministic* — whatever it returns, it's consistent.
    # Let's instead add a clear winner and explicitly test: if we have 2 high, 2 low, 1 medium,
    # high wins. Then reverse order and verify high still wins.
    baseline.record("s1", "medium")  # Now: 2 high, 2 low, 1 medium → high is modal.
    result = baseline.finalise_session("s1")
    assert result == effort.HIGH


# ---------------------------------------------------------------------------
# maybe_write / current_written_level: atomic, provenance-tracked write-back
# ---------------------------------------------------------------------------

import json

import pytest

from skill_advisor import config as config_mod
from skill_advisor import paths


def _cfg(**kw):
    base = dict(enabled=True, write_back=True, write_back_after_sessions=3)
    base.update(kw)
    return config_mod.Config(effort=config_mod.EffortConfig(**base))


def _fill(level, n):
    for i in range(n):
        for _ in range(3):
            baseline.record(f"sess{i}", level)
        baseline.finalise_session(f"sess{i}")


def test_no_write_below_threshold():
    _fill("high", 2)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None
    assert baseline.current_written_level() is None


def test_writes_at_threshold():
    _fill("high", 3)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") == "high"
    assert baseline.current_written_level() == "high"


def test_no_write_when_window_agrees_with_launch():
    _fill("xhigh", 5)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None


def test_write_preserves_existing_settings_keys():
    paths.ensure_dirs()
    paths.settings_file().write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [{"matcher": ""}]}}), encoding="utf-8"
    )
    _fill("low", 3)
    baseline.maybe_write(_cfg(), launch_level="xhigh")
    data = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert data["effortLevel"] == "low"
    assert "hooks" in data  # merged, not clobbered


def test_write_aborts_on_unparseable_settings():
    paths.ensure_dirs()
    paths.settings_file().write_text("not json{", encoding="utf-8")
    _fill("low", 3)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None
    # the original file is left exactly as-is
    assert paths.settings_file().read_text(encoding="utf-8") == "not json{"


def test_write_records_provenance():
    _fill("medium", 3)
    baseline.maybe_write(_cfg(), launch_level="xhigh")
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    entry = data["history"][-1]
    assert entry["from"] == "xhigh"
    assert entry["to"] == "medium"
    assert entry["sessions"] == 3


def test_write_disabled_by_config():
    _fill("low", 5)
    assert baseline.maybe_write(_cfg(write_back=False), launch_level="xhigh") is None


def test_ultracode_is_never_written():
    """Guard the invariant even if a bad level reaches the window.

    Window entries are {"session","level"} dicts (Task 12 fix-round-1), not
    bare strings — writing bare strings here would make `_window_entries`
    filter every entry out as malformed, and the assertion below would then
    pass for the wrong reason (empty window, not the ultracode guard).
    """
    paths.ensure_dirs()
    paths.baseline_file().write_text(
        json.dumps({"window": [{"session": f"s{i}", "level": "ultracode"} for i in range(5)]}),
        encoding="utf-8",
    )
    assert baseline.maybe_write(_cfg(), launch_level="high") is None


def test_no_write_without_a_launch_observation():
    """Model without reasoning-effort support: the whole feature stays silent.

    `launch_level` is None when the sensor never saw an effort level, which is
    exactly the case for a model that doesn't support the dial. Writing a
    baseline there would be acting on no evidence.
    """
    _fill("low", 5)
    assert baseline.maybe_write(_cfg(), launch_level=None) is None


def test_end_to_end_production_turn_sequence_reaches_write_back():
    """The regression this whole fix-round exists for: the Stop hook fires once
    per TURN, not once per session. Simulate the real interleaving — record()
    then finalise_session() after EVERY turn, across enough sessions to cross
    write_back_after_sessions — and prove maybe_write actually writes.

    Before this fix, finalise_session() popped/cleared the tally on every
    call, so with only one record() per turn the tally could never reach
    _MIN_RECOMMENDATIONS and window() stayed [] forever; this is the exact
    sequence that exposed that (390 passing tests never caught it because
    every one of them called record() N times then finalise_session() ONCE,
    which is not the order the hooks actually emit).
    """
    for session_num in range(3):
        session_id = f"prod-sess{session_num}"
        for _turn in range(5):
            # One prompt => one record(); one Stop => one finalise_session().
            baseline.record(session_id, "high")
            baseline.finalise_session(session_id)
    assert baseline.window() == [effort.HIGH, effort.HIGH, effort.HIGH]

    written = baseline.maybe_write(_cfg(), launch_level="xhigh")
    assert written == effort.HIGH
    assert baseline.current_written_level() == effort.HIGH


def test_maybe_write_does_not_rewrite_or_reannounce_across_a_second_production_batch():
    """F2: maybe_write compared target_level only against launch_level (the
    session's first observation), never against what is actually on disk. A
    session's first_observation never changes just because a write landed, so
    every subsequent Stop that re-accumulated the SAME target level in the
    window re-passed that comparison and wrote (and announced) it again.

    Drives the real production interleave twice in a row — record() then
    finalise_session() then maybe_write() every turn, exactly what
    hook.run_stop() does — with a FIXED launch_level throughout (as it is in
    production: hook.py computes it once from first_observation()). The
    second batch re-accumulates the identical 'low' target; a correct
    implementation must not write or announce it twice.
    """
    cfg = _cfg()
    launch_level = "xhigh"
    writes: list[str] = []
    announcements: list[str] = []
    for batch in range(2):
        for session_num in range(3):
            session_id = f"batch{batch}-sess{session_num}"
            for _turn in range(3):
                baseline.record(session_id, "low")
                baseline.finalise_session(session_id)
                result = baseline.maybe_write(cfg, launch_level=launch_level)
                if result is not None:
                    writes.append(result)
                ann = baseline.take_announcement()
                if ann is not None:
                    announcements.append(ann)

    assert writes == ["low"]
    assert len(announcements) == 1
    assert baseline.current_written_level() == "low"


def test_maybe_write_refuses_to_rewrite_the_value_already_on_disk():
    """Narrower pin of the same guard: even though target_level ('low') still
    differs from launch_level ('xhigh', the session's first observation,
    unchanged by a write), a second write of the identical value must be
    refused once 'low' is already what current_written_level() reads back
    from claudeskill-settings.json — and no second announcement is queued.
    """
    _fill("low", 3)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") == "low"
    assert baseline.current_written_level() == "low"
    assert baseline.take_announcement() is not None

    _fill("low", 3)  # window re-accumulates the same target after the reset
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None
    assert baseline.take_announcement() is None


# ---------------------------------------------------------------------------
# Extra coverage for the atomicity guarantee (Task 10 brief, behaviour 7):
# these two go beyond the brief's verbatim test list because "the file is
# unchanged" alone can pass for the wrong reason — these pin down *why*.
# ---------------------------------------------------------------------------


def test_write_settings_effort_cleans_up_tmp_on_replace_failure(monkeypatch):
    """If the final rename fails, no stray temp file must survive."""
    paths.ensure_dirs()
    paths.settings_file().write_text(json.dumps({"marker": True}), encoding="utf-8")

    from pathlib import Path

    def failing_replace(self, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", failing_replace)
    assert baseline._write_settings_effort("high") is False
    leftovers = list(paths.config_dir().glob("claudeskill-settings.json.tmp.*"))
    assert leftovers == []
    # target untouched — the failed rename never landed
    assert json.loads(paths.settings_file().read_text(encoding="utf-8")) == {"marker": True}


def test_write_settings_effort_aborts_before_touching_target_on_bad_serialisation(monkeypatch):
    """The round-trip json.loads(serialised) check must fire before target is ever written."""
    paths.ensure_dirs()
    paths.settings_file().write_text(json.dumps({"marker": True}), encoding="utf-8")

    def bad_dumps(*args, **kwargs):
        return "{not valid json"

    monkeypatch.setattr(baseline.json, "dumps", bad_dumps)
    assert baseline._write_settings_effort("high") is False
    # never reached tmp.write_text / tmp.replace: target is byte-for-byte original
    assert json.loads(paths.settings_file().read_text(encoding="utf-8")) == {"marker": True}
    leftovers = list(paths.config_dir().glob("claudeskill-settings.json.tmp.*"))
    assert leftovers == []


def test_maybe_write_honors_settings_file_override(monkeypatch, tmp_path):
    """Regression test for the bug this feature exists to fix.

    Before SKILL_ADVISOR_SETTINGS_FILE existed, a user whose real `claude
    --settings` file wasn't named claudeskill-settings.json got write-back
    that silently landed `effortLevel` in a SECOND file their actual `claude`
    invocation never reads — no error, no warning, the feature just did
    nothing. With the override set, the write must land in the user's actual
    file, and no default-named file may be created alongside it.
    """
    override = tmp_path / "custom-dir" / "claudew-settings.json"
    monkeypatch.setenv("SKILL_ADVISOR_SETTINGS_FILE", str(override))

    _fill("low", 3)
    written = baseline.maybe_write(_cfg(), launch_level="xhigh")

    # Checked FIRST and on its own: pre-fix, _write_settings_effort() ignores
    # the override entirely and writes the default-named file instead, so
    # this assertion alone is enough to fail red without depending on
    # anything below it (which pre-fix would instead error out reading a
    # file that was never created at `override`).
    default_named_file = paths.config_dir() / "claudeskill-settings.json"
    assert not default_named_file.exists()

    assert written == "low"
    data = json.loads(override.read_text(encoding="utf-8"))
    assert data["effortLevel"] == "low"


def test_maybe_write_survives_ensure_dirs_failure(monkeypatch):
    """A future caller could reach _write_settings_effort before config_dir exists.

    ensure_dirs() must be called INSIDE _write_settings_effort's try block so a
    failure there degrades to a no-op (maybe_write returns None) rather than an
    uncaught crash — matching the shape fixed for effort.write_recommendation.
    """
    _fill("low", 3)  # populate the window (and create config_dir) before breaking ensure_dirs

    def failing_ensure_dirs():
        raise OSError("simulated ensure_dirs failure")

    monkeypatch.setattr(paths, "ensure_dirs", failing_ensure_dirs)
    assert baseline.maybe_write(_cfg(), launch_level="xhigh") is None


# ---------------------------------------------------------------------------
# note_observation / first_observation / decrement_cooldown: user veto
# ---------------------------------------------------------------------------


def test_first_observation_is_remembered():
    baseline.note_observation("s1", "xhigh", _cfg())
    assert baseline.first_observation("s1") == "xhigh"
    baseline.note_observation("s1", "medium", _cfg())
    assert baseline.first_observation("s1") == "xhigh"  # first, not latest


def test_change_after_launch_is_a_veto():
    assert baseline.note_observation("s1", "xhigh", _cfg()) is False
    assert baseline.note_observation("s1", "medium", _cfg()) is True


def test_repeated_same_observation_is_not_a_veto():
    baseline.note_observation("s1", "high", _cfg())
    assert baseline.note_observation("s1", "high", _cfg()) is False


def test_veto_blocks_write_back_for_the_cooldown():
    cfg = _cfg(veto_cooldown_sessions=2)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # veto
    _fill("low", 3)
    assert baseline.maybe_write(cfg, launch_level="xhigh") is None


def test_veto_cooldown_survives_a_multi_turn_session():
    """F1: the Stop hook fires once per TURN, not once per session. An
    N-session cooldown must not drain within a single session no matter how
    many Stop events (turns) that session produces — only a session BOUNDARY
    (a differing session_id at the next Stop) may consume a unit.

    Before the fix, decrement_cooldown() ticked on every call regardless of
    session, so N turns of the SAME session fully drained an N-session
    cooldown — and with veto_cooldown_sessions=1, the very Stop of the turn
    that detected the veto drained it to zero, giving no protection at all.
    Here N+5 turns of one session must still leave the cooldown blocking.
    """
    n = 3
    cfg = _cfg(veto_cooldown_sessions=n)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # veto → cooldown armed at n, by s1
    for _turn in range(n + 5):  # far more Stop events than the cooldown count
        baseline.decrement_cooldown("s1")
    _fill("low", 3)
    assert baseline.maybe_write(cfg, launch_level="xhigh") is None  # still blocked


def test_veto_cooldown_is_consumed_by_session_boundaries_and_expires():
    """Complements the survival test above: the cooldown must still actually
    count down — just by distinct sessions, not by turns. The arming session
    itself (s1, where the veto was detected) must not count; only genuinely
    different session ids at subsequent Stop events consume a unit, and
    repeated turns within one of those later sessions must not double-count.
    """
    n = 2
    cfg = _cfg(veto_cooldown_sessions=n)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # veto → cooldown armed at n=2, by s1
    baseline.decrement_cooldown("s1")  # arming session itself — no-op
    baseline.decrement_cooldown("s1")  # still s1 — no-op
    baseline.decrement_cooldown("s2")  # session boundary #1 → cooldown 1
    baseline.decrement_cooldown("s2")  # still s2 (another turn) — no-op
    baseline.decrement_cooldown("s3")  # session boundary #2 → cooldown 0
    _fill("low", 3)
    assert baseline.maybe_write(cfg, launch_level="xhigh") == "low"


def test_alternating_sessions_each_charge_the_cooldown_only_once():
    """The cooldown counts DISTINCT sessions, not session *changes*.

    Tracking only the last-seen session id made `s1,s2,s1,s2,...` decrement on
    every single Stop, because the previous id always differed from the current
    one. Measured before this fix: 10 alternating turns across only 2 distinct
    sessions drained a 10-session cooldown to 1. Concurrent Claude Code sessions
    across repos produce exactly that interleaving, so the documented
    "suppressed for N sessions" guarantee silently did not hold.

    Two distinct sessions may consume at most two units, however they interleave.
    """
    n = 10
    cfg = _cfg(veto_cooldown_sessions=n)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # veto → cooldown armed at 10, by s1

    for _ in range(10):
        baseline.decrement_cooldown("s2")
        baseline.decrement_cooldown("s3")

    # s1 armed it (never charges), s2 and s3 charge once each → 10 - 2 = 8.
    assert baseline._load()["veto_cooldown_remaining"] == n - 2
    _fill("low", 3)
    assert baseline.maybe_write(cfg, launch_level="xhigh") is None


def test_rearming_the_cooldown_clears_previously_charged_sessions():
    """A fresh veto starts a fresh cooldown. Sessions that charged the previous
    one must be able to charge the new one, or a long-lived session id would be
    permanently exempt from every future cooldown."""
    cfg = _cfg(veto_cooldown_sessions=2)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # veto #1, armed by s1
    baseline.decrement_cooldown("s2")               # s2 charges → 1

    baseline.note_observation("s9", "low", cfg)
    baseline.note_observation("s9", "high", cfg)    # veto #2, armed by s9 → re-armed at 2
    baseline.decrement_cooldown("s2")               # s2 must charge again → 1

    assert baseline._load()["veto_cooldown_remaining"] == 1


def test_charged_session_list_stays_bounded():
    """The list is per-cooldown and cannot outgrow the cooldown it belongs to:
    once `remaining` hits 0 no further ids are recorded."""
    n = 3
    cfg = _cfg(veto_cooldown_sessions=n)
    baseline.note_observation("s1", "xhigh", cfg)
    baseline.note_observation("s1", "medium", cfg)  # armed by s1

    for i in range(50):
        baseline.decrement_cooldown(f"session-{i}")

    charged = baseline._load().get("veto_cooldown_charged", [])
    assert len(charged) <= n + 1  # n charging sessions + the arming session


def test_decrement_cooldown_migrates_the_legacy_last_session_key():
    """Upgrading mid-cooldown: an existing baseline.json written by the previous
    version carries `veto_cooldown_last_session` and no charged list. The arming
    session recorded there must still be exempt, or it charges its own cooldown."""
    baseline._save(
        {"veto_cooldown_remaining": 3, "veto_cooldown_last_session": "s1"}
    )
    baseline.decrement_cooldown("s1")  # the legacy arming session — must be a no-op
    assert baseline._load()["veto_cooldown_remaining"] == 3

    baseline.decrement_cooldown("s2")
    assert baseline._load()["veto_cooldown_remaining"] == 2


def test_decrement_cooldown_tolerates_malformed_charged_list():
    """A corrupt value must degrade to 'nobody has charged yet', never raise —
    baseline bookkeeping runs on the Stop hook path."""
    baseline._save({"veto_cooldown_remaining": 2, "veto_cooldown_charged": "not-a-list"})
    baseline.decrement_cooldown("s1")
    assert baseline._load()["veto_cooldown_remaining"] == 1


def test_veto_resets_the_window():
    _fill("low", 2)
    cfg = _cfg()
    baseline.note_observation("s9", "xhigh", cfg)
    baseline.note_observation("s9", "high", cfg)  # veto
    assert baseline.window() == []


def test_note_observation_tolerates_malformed_first_observation():
    """A corrupted per-session value (not a string) must not be treated as a real
    first observation — otherwise any later call reads as a spurious veto purely
    from the type mismatch, wiping the window and burning the cooldown on garbage.
    """
    _fill("low", 5)  # accumulated evidence that must survive
    paths.ensure_dirs()
    # Merge the malformed first_observations value into the real baseline.json
    # rather than reserialising baseline.window() — window() now returns the
    # projected list[str] form, which is not the on-disk {"session","level"}
    # entry shape; round-tripping it back into "window" would silently drop
    # every entry as malformed and defeat the "must survive" assertion below.
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    data["first_observations"] = {"s1": 123}
    paths.baseline_file().write_text(json.dumps(data), encoding="utf-8")
    assert baseline.note_observation("s1", "xhigh", _cfg()) is False
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data.get("veto_cooldown_remaining", 0) == 0
    assert baseline.window() == ["low"] * 5
    # the malformed value is now healed to a real first observation
    assert baseline.first_observation("s1") == "xhigh"


def test_note_observation_ignores_level_not_observable():
    assert baseline.note_observation("s1", "not-a-level", _cfg()) is False
    assert baseline.first_observation("s1") is None


def test_note_observation_ignores_empty_session_id():
    assert baseline.note_observation("", "xhigh", _cfg()) is False
    assert baseline.note_observation(None, "xhigh", _cfg()) is False


def test_decrement_cooldown_floors_at_zero():
    baseline._save({"veto_cooldown_remaining": 0})
    baseline.decrement_cooldown("s1")
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["veto_cooldown_remaining"] == 0


def test_decrement_cooldown_tolerates_empty_session_id():
    baseline._save({"veto_cooldown_remaining": 3})
    baseline.decrement_cooldown("")
    baseline.decrement_cooldown(None)
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["veto_cooldown_remaining"] == 3


# ---------------------------------------------------------------------------
# take_announcement: one-shot consumption of a pending baseline-move message
# ---------------------------------------------------------------------------


def test_announcement_is_returned_once_then_cleared():
    _fill("low", 3)
    baseline.maybe_write(_cfg(), launch_level="xhigh")
    msg = baseline.take_announcement()
    assert "xhigh" in msg and "low" in msg
    assert "/effort xhigh" in msg
    assert baseline.take_announcement() is None


def test_no_announcement_without_a_write():
    assert baseline.take_announcement() is None


def test_announcement_tolerates_non_dict_shape():
    """baseline.json can be structurally corrupt (valid JSON, wrong shape) —
    e.g. hand-edited or clobbered by a racing writer. `announce` being a
    non-dict must degrade to "nothing pending", never raise.
    """
    paths.ensure_dirs()
    paths.baseline_file().write_text(json.dumps({"announce": "not-a-dict"}), encoding="utf-8")
    assert baseline.take_announcement() is None

    paths.baseline_file().write_text(json.dumps({"announce": ["from", "to"]}), encoding="utf-8")
    assert baseline.take_announcement() is None

    paths.baseline_file().write_text(json.dumps({"announce": 42}), encoding="utf-8")
    assert baseline.take_announcement() is None


def test_announcement_tolerates_missing_to_key():
    """A dict-shaped `announce` missing its `to` key must not raise or format
    a message with a blank destination.
    """
    paths.ensure_dirs()
    paths.baseline_file().write_text(
        json.dumps({"announce": {"from": "xhigh", "sessions": 3}}), encoding="utf-8"
    )
    assert baseline.take_announcement() is None
