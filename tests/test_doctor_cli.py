"""Tests for the `skill-advisor doctor` CLI subcommand — parallelization budget check."""

from __future__ import annotations


def test_doctor_warns_when_parallelization_budget_too_low(capsys, monkeypatch):
    from skill_advisor import cli, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[matcher]
budget_seconds = 4.0
[parallelization]
enabled = true
judge_timeout_seconds = 12.0
""",
        encoding="utf-8",
    )
    # Doctor may exit non-zero when it flags warnings; tolerate either.
    try:
        cli.main(["doctor"])
    except SystemExit:
        pass
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "parallelization" in out.lower()
    assert "budget_seconds" in out.lower()


def test_doctor_silent_when_parallelization_budget_ok(capsys, monkeypatch):
    from skill_advisor import cli, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[matcher]
budget_seconds = 20.0
[parallelization]
enabled = true
judge_timeout_seconds = 12.0
""",
        encoding="utf-8",
    )
    try:
        cli.main(["doctor"])
    except SystemExit:
        pass
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "parallelization" not in out.lower() or "ok" in out.lower()


def test_doctor_reports_statusline_when_effort_enabled(capsys, monkeypatch):
    from skill_advisor import cli, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[effort]
enabled = true
""",
        encoding="utf-8",
    )
    try:
        cli.main(["doctor"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "jq" in out.lower()
    assert "statusline" in out.lower()
    assert "effort state" in out.lower()


def test_doctor_silent_on_effort_when_feature_disabled(capsys, monkeypatch):
    from skill_advisor import cli, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("", encoding="utf-8")
    try:
        cli.main(["doctor"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "statusline" not in out.lower()
    assert "effort state" not in out.lower()


def test_doctor_still_warns_when_budget_is_below_the_detector_timeout(
    isolated_paths, capsys
):
    """The check must stay honest after the defaults move."""
    from skill_advisor import cli

    (isolated_paths["config_home"] / "config.toml").write_text(
        "[matcher]\nbudget_seconds = 4.0\n\n"
        "[parallelization]\nenabled = true\njudge_timeout_seconds = 5.0\n",
        encoding="utf-8",
    )
    try:
        cli.main(["doctor"])  # ends in sys.exit; tests/test_doctor_cli.py:23 idiom
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "budget_seconds" in out


def test_doctor_reports_unparseable_skill_files(
    isolated_paths, fake_claude_home, capsys
):
    """23% of SKILL.md files on the author's machine have no frontmatter and are
    invisible to the scanner. Silence about that reads as full coverage."""
    from skill_advisor import cli

    broken = fake_claude_home / "skills" / "no-frontmatter"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "# Just a heading, no frontmatter\n", encoding="utf-8"
    )

    try:
        cli.main(["doctor"])  # ends in sys.exit; tests/test_doctor_cli.py:23 idiom
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "unparseable" in out.lower()
    assert "no-frontmatter" in out


def test_doctor_reports_pickable_versus_pool(isolated_paths, fake_claude_home, capsys):
    from skill_advisor import cli

    paths_mod = __import__("skill_advisor.paths", fromlist=["paths"])
    paths_settings = paths_mod.settings_file()
    paths_settings.parent.mkdir(parents=True, exist_ok=True)
    paths_settings.write_text(
        '{"skillOverrides": {"demo-skill": "off"}}', encoding="utf-8"
    )

    try:
        cli.main(["doctor"])  # ends in sys.exit; tests/test_doctor_cli.py:23 idiom
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "pickable" in out.lower()
    assert "pool" in out.lower()
