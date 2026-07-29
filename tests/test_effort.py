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


def test_write_recommendation_swallows_ensure_dirs_oserror(monkeypatch):
    """write_recommendation must not raise even if ensure_dirs fails."""
    rec = effort.EffortRecommendation(level=effort.LOW, reason="r", source="heuristic")
    monkeypatch.setattr("skill_advisor.paths.ensure_dirs", lambda: (_ for _ in ()).throw(OSError("disk full")))
    # Must not raise; must return normally.
    effort.write_recommendation(rec, session_id="s1")


from skill_advisor import config as config_mod
from skill_advisor import lifecycle


def _cfg(**kw):
    base = dict(enabled=True, ultracode_nudge=True)
    base.update(kw)
    return config_mod.Config(effort=config_mod.EffortConfig(**base))


def test_rung1_parallel_wins_and_yields_ultracode():
    rec = effort.classify(
        phase=lifecycle.PLANNING, judge_effort=effort.LOW, parallel=True,
        cfg=_cfg(), prompt="build the thing",
    )
    assert rec.level == effort.ULTRACODE
    assert rec.source == "parallelization"


def test_rung1_suppressed_when_ultracode_nudge_off():
    rec = effort.classify(
        phase=lifecycle.PLANNING, judge_effort=effort.LOW, parallel=True,
        cfg=_cfg(ultracode_nudge=False), prompt="build the thing",
    )
    assert rec.level == effort.LOW
    assert rec.source == "judge"


def test_rung2_judge_used_when_not_parallel():
    rec = effort.classify(
        phase=None, judge_effort=effort.XHIGH, parallel=False, cfg=_cfg(), prompt="x"
    )
    assert rec.level == effort.XHIGH
    assert rec.source == "judge"


def test_rung3_phase_used_when_judge_silent():
    rec = effort.classify(
        phase=lifecycle.COMPLETE, judge_effort=None, parallel=False, cfg=_cfg(), prompt="x"
    )
    assert rec.level == effort.LOW
    assert rec.source == "phase"


def test_rung4_heuristic_long_technical_prompt():
    prompt = "refactor the auth middleware and migrate the session schema to postgres"
    rec = effort.classify(
        phase=None, judge_effort=None, parallel=False, cfg=_cfg(), prompt=prompt
    )
    assert rec.source == "heuristic"
    assert rec.level in (effort.HIGH, effort.XHIGH)


def test_rung4_heuristic_short_prompt_is_low():
    rec = effort.classify(
        phase=None, judge_effort=None, parallel=False, cfg=_cfg(), prompt="rename this var"
    )
    assert rec.source == "heuristic"
    assert rec.level == effort.LOW


def test_disabled_config_returns_none():
    rec = effort.classify(
        phase=lifecycle.PLANNING, judge_effort=effort.XHIGH, parallel=True,
        cfg=_cfg(enabled=False), prompt="x",
    )
    assert rec is None


def test_classify_never_returns_max():
    for phase in (lifecycle.PLANNING, lifecycle.REVIEW, lifecycle.COMPLETE, None):
        rec = effort.classify(
            phase=phase, judge_effort=None, parallel=False, cfg=_cfg(), prompt="a b c d e f g"
        )
        assert rec is None or rec.level != effort.MAX
