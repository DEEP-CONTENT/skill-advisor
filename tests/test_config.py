from skill_advisor import config, paths


def test_defaults_when_no_config_file(isolated_paths):
    cfg = config.load()
    assert cfg.matcher.model.startswith("claude-haiku")
    assert cfg.matcher.max_candidates == 15
    assert cfg.matcher.max_picks == 3
    assert cfg.matcher.budget_seconds == 4.0
    assert cfg.matcher.use_judge is False
    assert cfg.matcher.min_embedding_score == 0.35
    assert cfg.catalog.extra_roots == ()
    assert cfg.triage.skip_if_shorter_than == 6


def test_user_overrides_applied(isolated_paths):
    paths.config_file().write_text(
        """
[matcher]
model = "claude-opus-4-7"
max_candidates = 8
budget_seconds = 4.0

[catalog]
extra_roots = ["/tmp/team-skills"]
exclude_names = ["noisy-skill"]

[triage]
skip_if_shorter_than = 3
extra_skip_patterns = ["^wip:"]
""",
        encoding="utf-8",
    )
    cfg = config.load()
    assert cfg.matcher.model == "claude-opus-4-7"
    assert cfg.matcher.max_candidates == 8
    assert cfg.matcher.budget_seconds == 4.0
    assert cfg.catalog.extra_roots == ("/tmp/team-skills",)
    assert cfg.catalog.exclude_names == ("noisy-skill",)
    assert cfg.triage.skip_if_shorter_than == 3
    assert cfg.triage.extra_skip_patterns == ("^wip:",)


def test_parallelization_config_defaults(isolated_paths):
    cfg = config.load()
    assert cfg.parallelization.enabled is False
    assert cfg.parallelization.min_tasks == 3
    assert cfg.parallelization.judge_timeout_seconds == 20.0


def test_parallelization_config_parses_toml(tmp_path):
    from skill_advisor.config import load
    path = tmp_path / "config.toml"
    path.write_text(
        """
[parallelization]
enabled = true
min_tasks = 5
judge_timeout_seconds = 8.5
""",
        encoding="utf-8",
    )
    cfg = load(path)
    assert cfg.parallelization.enabled is True
    assert cfg.parallelization.min_tasks == 5
    assert cfg.parallelization.judge_timeout_seconds == 8.5
