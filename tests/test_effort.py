from skill_advisor import effort


def test_enums_are_asymmetric():
    assert effort.ULTRACODE in effort.RECOMMENDABLE
    assert effort.ULTRACODE not in effort.OBSERVABLE
    assert effort.MAX in effort.OBSERVABLE
    assert effort.MAX not in effort.RECOMMENDABLE


def test_rank_ordering():
    assert effort.rank(effort.LOW) < effort.rank(effort.MEDIUM)
    assert effort.rank(effort.MEDIUM) < effort.rank(effort.HIGH)
    assert effort.rank(effort.HIGH) < effort.rank(effort.XHIGH)
    assert effort.rank(effort.XHIGH) < effort.rank(effort.MAX)
    # ultracode resolves to xhigh effort, so it ranks equal to xhigh
    assert effort.rank(effort.ULTRACODE) == effort.rank(effort.XHIGH)


def test_rank_unknown_is_none():
    assert effort.rank("turbo") is None
    assert effort.rank("") is None


def test_to_persistable():
    assert effort.to_persistable(effort.ULTRACODE) == effort.XHIGH
    assert effort.to_persistable(effort.MAX) is None
    assert effort.to_persistable(effort.HIGH) == effort.HIGH
    assert effort.to_persistable("turbo") is None


def test_should_nudge_on_disagreement():
    assert effort.should_nudge(effort.MEDIUM, effort.XHIGH) is True
    assert effort.should_nudge(effort.XHIGH, effort.MEDIUM) is True


def test_should_not_nudge_on_agreement():
    assert effort.should_nudge(effort.HIGH, effort.HIGH) is False
    # ultracode ranks equal to xhigh, so being at xhigh already satisfies it
    assert effort.should_nudge(effort.XHIGH, effort.ULTRACODE) is False


def test_max_silences_the_feature():
    # The user has deliberately gone above anything we know how to recommend.
    assert effort.should_nudge(effort.MAX, effort.LOW) is False
    assert effort.should_nudge(effort.MAX, effort.ULTRACODE) is False


def test_missing_observation_suppresses_nudge():
    # First prompt of a session: sensor has not run yet. Never guess.
    assert effort.should_nudge(None, effort.XHIGH) is False
    assert effort.should_nudge(effort.LOW, None) is False


import json

from skill_advisor import paths


def test_write_recommendation_roundtrip():
    rec = effort.EffortRecommendation(level=effort.XHIGH, reason="5 tasks", source="parallelization")
    effort.write_recommendation(rec, session_id="s1")
    data = json.loads(paths.effort_file().read_text(encoding="utf-8"))
    assert data["level"] == "xhigh"
    assert data["reason"] == "5 tasks"
    assert data["source"] == "parallelization"
    assert data["session_id"] == "s1"


def test_write_recommendation_is_atomic_no_tmp_left():
    rec = effort.EffortRecommendation(level=effort.LOW, reason="r", source="heuristic")
    effort.write_recommendation(rec, session_id="s1")
    leftovers = list(paths.cache_dir().glob("effort.json.*"))
    assert leftovers == []


def test_read_observed_missing_file_returns_none():
    assert effort.read_observed() == (None, None)


def test_read_observed_parses_sensor_file():
    paths.ensure_dirs()
    paths.observed_effort_file().write_text(
        json.dumps({"session_id": "s9", "level": "medium", "ts": 1}), encoding="utf-8"
    )
    assert effort.read_observed() == ("s9", "medium")


def test_read_observed_rejects_corrupt_file():
    paths.ensure_dirs()
    paths.observed_effort_file().write_text("not json{", encoding="utf-8")
    assert effort.read_observed() == (None, None)


def test_read_observed_rejects_unknown_level():
    paths.ensure_dirs()
    paths.observed_effort_file().write_text(
        json.dumps({"session_id": "s9", "level": "turbo"}), encoding="utf-8"
    )
    assert effort.read_observed() == ("s9", None)
