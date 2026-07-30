import json
from unittest.mock import patch

from skill_advisor import effort, judge
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config, MatcherConfig


def _candidates():
    return [
        CatalogEntry(kind="skill", name="alpha", namespace="user", description="first"),
        CatalogEntry(kind="skill", name="beta", namespace="user", description="second"),
    ]


def test_parse_valid_reply():
    envelope = {
        "result": json.dumps(
            {"picks": [{"name": "alpha", "reason": "fits"}], "skip": False}
        )
    }
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result is not None
    assert len(result.picks) == 1
    assert result.picks[0].name == "alpha"
    assert result.picks[0].reason == "fits"


def test_rejects_hallucinated_names():
    envelope = {
        "result": json.dumps(
            {"picks": [{"name": "not-in-catalog", "reason": "oops"}], "skip": False}
        )
    }
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks == []


def test_handles_skip_response():
    envelope = {"result": json.dumps({"picks": [], "skip": True})}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks == []


def test_strips_code_fence():
    inner = (
        "```json\n"
        + json.dumps({"picks": [{"name": "beta", "reason": "ok"}], "skip": False})
        + "\n```"
    )
    envelope = {"result": inner}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks and result.picks[0].name == "beta"


def test_extracts_embedded_json_object():
    inner = (
        "Here is the answer:\n"
        + json.dumps({"picks": [{"name": "alpha", "reason": "ok"}], "skip": False})
        + "\nEnd."
    )
    envelope = {"result": inner}
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result.picks and result.picks[0].name == "alpha"


def test_rejects_non_json_envelope():
    assert judge._parse_judge_reply("not json at all", _candidates()) is None


def test_rejects_envelope_without_result_string():
    envelope = {"result": {"picks": []}}  # result must be a string
    assert judge._parse_judge_reply(json.dumps(envelope), _candidates()) is None


def test_rank_happy_path():
    cfg = Config()
    stdout = json.dumps(
        {
            "result": json.dumps(
                {"picks": [{"name": "alpha", "reason": "fit"}], "skip": False}
            )
        }
    )
    completed = type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch("skill_advisor.judge.subprocess.run", return_value=completed),
    ):
        result = judge.rank("anything", _candidates(), cfg)
        assert result and result.picks[0].name == "alpha"


def _entry(name):
    return CatalogEntry(
        kind="skill", name=name, namespace="user", description="d", path="/x"
    )


def _envelope(inner: dict) -> str:
    return json.dumps({"result": json.dumps(inner)})


def test_judge_parses_effort_field():
    cands = [_entry("alpha")]
    out = judge._parse_judge_reply(
        _envelope(
            {
                "picks": [{"name": "alpha", "reason": "r"}],
                "skip": False,
                "effort": "xhigh",
            }
        ),
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
        _envelope({"picks": [{"name": "alpha", "reason": "r"}], "effort": "turbo"}),
        cands,
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


def test_rank_reports_cli_missing_not_none():
    with patch("skill_advisor.judge.shutil.which", return_value=None):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_CLI_MISSING
    assert result.picks == []


def test_rank_reports_timeout():
    import subprocess

    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch(
            "skill_advisor.judge.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0),
        ),
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_TIMEOUT


def test_rank_reports_nonzero_exit():
    completed = type("R", (), {"returncode": 3, "stdout": "", "stderr": "boom"})()
    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch("skill_advisor.judge.subprocess.run", return_value=completed),
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_EXIT


def test_rank_reports_unparseable_reply():
    completed = type("R", (), {"returncode": 0, "stdout": "not json", "stderr": ""})()
    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch("skill_advisor.judge.subprocess.run", return_value=completed),
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_UNPARSEABLE


def test_rank_reports_no_candidates():
    result = judge.rank("anything", [], Config())
    assert result.failure == judge.FAILURE_NO_CANDIDATES


def test_a_genuine_decline_is_not_a_failure():
    """The judge ran and said 'nothing fits'. That is a verdict, not an error."""
    inner = json.dumps({"picks": [], "skip": True})
    completed = type(
        "R",
        (),
        {"returncode": 0, "stdout": json.dumps({"result": inner}), "stderr": ""},
    )()
    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch("skill_advisor.judge.subprocess.run", return_value=completed),
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.picks == []
    assert result.failure is None


def test_rank_when_claude_not_on_path_yields_no_picks():
    with patch("skill_advisor.judge.shutil.which", return_value=None):
        result = judge.rank("anything", _candidates(), Config())
        assert result.picks == []


def test_rank_on_timeout_yields_no_picks():
    import subprocess

    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch(
            "skill_advisor.judge.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0),
        ),
    ):
        assert judge.rank("anything", _candidates(), Config()).picks == []


def test_judge_timeout_is_strictly_under_the_hook_alarm():
    """Two independent timeout layers guard the hot path:

      * judge.py:106-109  subprocess timeout = max(budget_seconds - 0.5, 0.5)
      * hook.py:133       SIGALRM            = max(int(budget_seconds + 0.5), 1)

    The alarm aborts the whole hook and returns silent, bypassing the embedding
    fallback entirely. If it ever fires first, the fallback is dead code. Pin the
    ordering by capturing the real timeout passed to subprocess.run.
    """
    import subprocess

    captured = {}

    def _spy(*a, timeout=None, **k):
        captured["t"] = timeout
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=timeout)

    for budget in (1.0, 4.0, 8.0, 25.0):
        captured.clear()
        cfg = Config(matcher=MatcherConfig(budget_seconds=budget))
        with (
            patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("skill_advisor.judge.subprocess.run", side_effect=_spy),
        ):
            judge.rank("prompt", _candidates(), cfg)

        # hook.py's real formula at line 133
        hook_alarm = max(int(budget + 0.5), 1)
        assert captured["t"] < hook_alarm, (
            f"judge timeout {captured['t']} must be strictly less than "
            f"hook alarm {hook_alarm} for budget {budget}"
        )
