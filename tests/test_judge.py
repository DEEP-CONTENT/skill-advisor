import json
from unittest.mock import patch

from skill_advisor import judge
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config


def _candidates():
    return [
        CatalogEntry(kind="skill", name="alpha", namespace="user", description="first"),
        CatalogEntry(kind="skill", name="beta", namespace="user", description="second"),
    ]


def test_parse_valid_reply():
    envelope = {"result": json.dumps({"picks": [{"name": "alpha", "reason": "fits"}], "skip": False})}
    picks = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert picks is not None
    assert len(picks) == 1
    assert picks[0].name == "alpha"
    assert picks[0].reason == "fits"


def test_rejects_hallucinated_names():
    envelope = {"result": json.dumps({"picks": [{"name": "not-in-catalog", "reason": "oops"}], "skip": False})}
    picks = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert picks == []


def test_handles_skip_response():
    envelope = {"result": json.dumps({"picks": [], "skip": True})}
    picks = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert picks == []


def test_strips_code_fence():
    inner = "```json\n" + json.dumps({"picks": [{"name": "beta", "reason": "ok"}], "skip": False}) + "\n```"
    envelope = {"result": inner}
    picks = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert picks and picks[0].name == "beta"


def test_extracts_embedded_json_object():
    inner = "Here is the answer:\n" + json.dumps({"picks": [{"name": "alpha", "reason": "ok"}], "skip": False}) + "\nEnd."
    envelope = {"result": inner}
    picks = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert picks and picks[0].name == "alpha"


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
        picks = judge.rank("anything", _candidates(), cfg)
        assert picks and picks[0].name == "alpha"
