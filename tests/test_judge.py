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


def test_parse_judge_reply_num_turns_one_with_valid_reply_parses_normally():
    """num_turns == 1 is the clean case (238/238 in the 250-row corpus);
    it must not be mistaken for contamination."""
    envelope = {
        "num_turns": 1,
        "result": json.dumps(
            {"picks": [{"name": "alpha", "reason": "fits"}], "skip": False}
        ),
    }
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result is not None
    assert result.failure is None
    assert [p.name for p in result.picks] == ["alpha"]


def test_num_turns_field_absent_is_not_treated_as_contaminated():
    """Backward compatibility: an envelope with no `num_turns` key at all
    (older `claude` CLI, or a hand-built test fixture) must still parse."""
    envelope = {
        "result": json.dumps(
            {"picks": [{"name": "alpha", "reason": "fits"}], "skip": False}
        )
    }
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result is not None
    assert result.failure is None


def test_parse_judge_reply_num_turns_greater_than_one_is_hook_contaminated():
    """The production bug: a nested `claude -p` inherits the calling
    session's hooks. When one fires (e.g. a Stop hook demanding a code
    review), it forces extra turns and `result` becomes the nested
    session's reply to the hook instead of the judge's verdict."""
    envelope = {
        "num_turns": 5,
        "result": "Sure — here's a code review of your last change: ...",
    }
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result is not None
    assert result.failure == judge.FAILURE_HOOK_CONTAMINATED
    assert result.picks == []


def test_hook_contamination_takes_priority_even_if_reply_would_otherwise_parse():
    """num_turns is checked before the inner reply is ever interpreted — a
    hijacked session is not a parse failure, so it must not be classified
    by what the (irrelevant) reply text happens to contain."""
    envelope = {
        "num_turns": 2,
        "result": json.dumps(
            {"picks": [{"name": "alpha", "reason": "fits"}], "skip": False}
        ),
    }
    result = judge._parse_judge_reply(json.dumps(envelope), _candidates())
    assert result is not None
    assert result.failure == judge.FAILURE_HOOK_CONTAMINATED
    assert result.picks == []


def test_rank_reports_hook_contamination_when_num_turns_exceeds_one():
    stdout = json.dumps(
        {
            "num_turns": 5,
            "result": "I reviewed your code and found three issues...",
        }
    )
    completed = type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch("skill_advisor.judge.subprocess.run", return_value=completed),
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_HOOK_CONTAMINATED
    assert result.picks == []


def test_rank_reports_unparseable_when_num_turns_is_one_and_reply_is_malformed():
    """The counterpart to the contamination test above: a genuinely
    malformed single-turn reply must still be FAILURE_UNPARSEABLE, not
    swept into the new failure mode."""
    stdout = json.dumps({"num_turns": 1, "result": "not json at all"})
    completed = type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()
    with (
        patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
        patch("skill_advisor.judge.subprocess.run", return_value=completed),
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_UNPARSEABLE


def test_judge_timeout_is_strictly_under_the_hook_alarm():
    """Two independent timeout layers guard the hot path:

      * judge.rank()'s subprocess timeout = max(budget_seconds - 0.5, 0.5)   (judge.py:106-110)
      * hook.alarm_seconds(cfg)           = max(int(budget_seconds + 0.5), 1) (hook.py)

    The alarm aborts the whole hook and returns silent, bypassing the embedding
    fallback entirely. If it ever fires first, the fallback is dead code. Pin the
    ordering by capturing the real timeout passed to subprocess.run *and* calling
    hook's real `alarm_seconds()` function — not a restated copy of its formula —
    so an edit to either side of the invariant is caught.

    The margin between the two layers is `round(b) - (b - 0.5)`, which ranges
    over (0, 1] depending on budget_seconds's fractional part; 1.0/4.0/8.0/25.0
    all happen to land on the maximal 0.5s margin, so 2.49 and 8.4 are included
    to exercise the thin end of that range too. Only strict ordering is
    asserted — there's no guaranteed margin floor, and there shouldn't be one.
    """
    import subprocess

    from skill_advisor import hook

    captured = {}

    def _spy(*a, timeout=None, **k):
        captured["t"] = timeout
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=timeout)

    for budget in (1.0, 2.49, 4.0, 8.0, 8.4, 25.0):
        captured.clear()
        cfg = Config(matcher=MatcherConfig(budget_seconds=budget))
        with (
            patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("skill_advisor.judge.subprocess.run", side_effect=_spy),
        ):
            judge.rank("prompt", _candidates(), cfg)

        hook_alarm = hook.alarm_seconds(cfg)
        assert captured["t"] < hook_alarm, (
            f"judge timeout {captured['t']} must be strictly less than "
            f"hook alarm {hook_alarm} for budget {budget}"
        )
