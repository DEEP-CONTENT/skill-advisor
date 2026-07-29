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
    for lvl in ("high", "high", "low"):
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


def test_window_is_bounded():
    for i in range(40):
        for _ in range(3):
            baseline.record(f"s{i}", "high")
        baseline.finalise_session(f"s{i}")
    assert len(baseline.window()) <= baseline._WINDOW_CAP


def test_corrupt_baseline_file_resets_cleanly():
    paths.ensure_dirs()
    paths.baseline_file().write_text("{{{", encoding="utf-8")
    assert baseline.window() == []
