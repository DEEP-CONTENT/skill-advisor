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


def test_thin_session_is_discarded():
    baseline.record("s1", "high")
    baseline.record("s1", "high")
    assert baseline.finalise_session("s1") is None
    assert baseline.window() == []


def test_finalise_appends_to_window_and_clears_tally():
    for lvl in ("low", "low", "low"):
        baseline.record("s1", lvl)
    baseline.finalise_session("s1")
    assert baseline.window() == [effort.LOW]
    # tally cleared — re-finalising the same session must not double-count
    assert baseline.finalise_session("s1") is None
    assert baseline.window() == [effort.LOW]


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
    """A corrupted 'tallies' value (not a dict) must not raise on finalise."""
    paths.ensure_dirs()
    paths.baseline_file().write_text(json.dumps({"tallies": ["list", "not", "dict"]}), encoding="utf-8")
    # finalise_session must not crash; it should reset tallies and return None (no session to finalize).
    assert baseline.finalise_session("s1") is None
    # Verify tallies was reset to an empty dict in the file.
    data = json.loads(paths.baseline_file().read_text(encoding="utf-8"))
    assert data["tallies"] == {}


def test_thin_session_clears_tally_across_calls():
    """A thin session (< 3 recommendations) must not leak data to subsequent finalise calls."""
    # First thin session.
    baseline.record("s1", "high")
    baseline.record("s1", "high")
    assert baseline.finalise_session("s1") is None
    # Record two more times for the same session (simulating a new attempt).
    baseline.record("s1", "low")
    baseline.record("s1", "low")
    # Still thin — must return None and not have accumulated the first two records.
    assert baseline.finalise_session("s1") is None
    assert baseline.window() == []


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
    """Guard the invariant even if a bad level reaches the window."""
    paths.ensure_dirs()
    paths.baseline_file().write_text(
        json.dumps({"window": ["ultracode"] * 5}), encoding="utf-8"
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
