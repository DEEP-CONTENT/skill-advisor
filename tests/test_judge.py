import json
from unittest.mock import patch

from skill_advisor import effort, judge
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config


def _candidates():
    return [
        CatalogEntry(kind="skill", name="alpha", namespace="user", description="first"),
        CatalogEntry(kind="skill", name="beta", namespace="user", description="second"),
    ]


def test_parse_valid_reply():
    envelope = {"result": json.dumps({"picks": [{"name": "alpha", "reason": "fits"}], "skip": False})}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result is not None
    assert len(result.picks) == 1
    assert result.picks[0].name == "alpha"
    assert result.picks[0].reason == "fits"


def test_rejects_hallucinated_names():
    envelope = {"result": json.dumps({"picks": [{"name": "not-in-catalog", "reason": "oops"}], "skip": False})}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks == []


def test_handles_skip_response():
    envelope = {"result": json.dumps({"picks": [], "skip": True})}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks == []


def test_strips_code_fence():
    inner = "```json\n" + json.dumps({"picks": [{"name": "beta", "reason": "ok"}], "skip": False}) + "\n```"
    envelope = {"result": inner}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks and result.picks[0].name == "beta"


def test_extracts_embedded_json_object():
    inner = "Here is the answer:\n" + json.dumps({"picks": [{"name": "alpha", "reason": "ok"}], "skip": False}) + "\nEnd."
    envelope = {"result": inner}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks and result.picks[0].name == "alpha"


def test_rejects_non_json_envelope():
    assert judge._parse_judge_reply("not json at all", _candidates()) is None


def test_rejects_envelope_without_result_string():
    envelope = {"result": {"picks": []}}  # result must be a string
    assert judge._parse_judge_reply(json.dumps(envelope), _candidates()) is None


def test_rank_returns_none_when_claude_not_on_path():
    with patch("skill_advisor.judge.shutil.which", return_value=None):
        result = judge.rank("anything", _candidates(), Config())
        assert result is None


def test_rank_returns_none_on_timeout():
    cfg = Config()
    import subprocess
    with patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"), patch(
        "skill_advisor.judge.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0),
    ):
        assert judge.rank("anything", _candidates(), cfg) is None


def test_rank_happy_path():
    cfg = Config()
    stdout = json.dumps({"result": json.dumps({"picks": [{"name": "alpha", "reason": "fit"}], "skip": False})})
    completed = type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
    with patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"), patch(
        "skill_advisor.judge.subprocess.run", return_value=completed
    ):
        result = judge.rank("anything", _candidates(), cfg)
        assert result and result.picks[0].name == "alpha"


def _entry(name):
    return CatalogEntry(kind="skill", name=name, namespace="user", description="d", path="/x")


def _envelope(inner: dict) -> str:
    return json.dumps({"result": json.dumps(inner)})


def test_judge_parses_effort_field():
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "skip": False, "effort": "xhigh"}),
        cands,
    )
    assert out.effort == effort.XHIGH
    assert [p.name for p in out.picks] == ["alpha"]


def test_judge_without_effort_field_still_parses():
    """Backward compatibility: a reply omitting `effort` must behave as today."""
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "skip": False}), cands
    )
    assert out.effort is None
    assert [p.name for p in out.picks] == ["alpha"]


def test_judge_drops_out_of_enum_effort():
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "effort": "turbo"}), cands
    )
    assert out.effort is None


def test_judge_drops_max_effort():
    """`max` is observable but never recommendable — the judge must not return it."""
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "effort": "max"}), cands
    )
    assert out.effort is None


def test_judge_skip_true_returns_empty_picks_with_effort():
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope({"picks": [], "skip": True, "effort": "low"}), cands
    )
    assert out.picks == []
    assert out.effort == effort.LOW
