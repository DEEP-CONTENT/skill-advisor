import pytest

from skill_advisor import triage
from skill_advisor.config import Config, TriageConfig


@pytest.mark.parametrize(
    "prompt",
    [
        "/help",
        "/loop 5m /foo",
        "yes",
        "ok",
        "thanks!",
        "continue",
        "Perfect.",
        "  ",
        "use the brainstorming skill",
        "Please use the Plan agent",
    ],
)
def test_skips_trivial_prompts(prompt):
    assert triage.should_skip(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "refactor the authentication middleware to use JWT instead of sessions",
        "there's a bug in the checkout flow when tax is zero",
        "plan a migration from mongodb to postgres",
        "review this PR for security issues",
        "write tests for the user service",
    ],
)
def test_runs_on_substantive_prompts(prompt):
    assert triage.should_skip(prompt) is False


def test_short_prompt_with_tech_signal_runs():
    # Under word threshold but contains a technical keyword → don't skip.
    assert triage.should_skip("fix bug") is False


def test_short_prompt_without_tech_signal_skips():
    assert triage.should_skip("sounds good") is True


def test_extra_skip_patterns_from_config():
    cfg = Config(triage=TriageConfig(extra_skip_patterns=(r"^wip:",)))
    assert triage.should_skip("wip: refactor auth middleware", cfg) is True
    assert triage.should_skip("refactor auth middleware", cfg) is False


def test_invalid_regex_in_extra_patterns_does_not_raise():
    cfg = Config(triage=TriageConfig(extra_skip_patterns=("(unclosed",)))
    # Should silently ignore the broken pattern.
    assert triage.should_skip("refactor auth middleware", cfg) is False
