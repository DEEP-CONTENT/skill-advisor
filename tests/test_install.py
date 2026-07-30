import json

import pytest

from skill_advisor import config
from skill_advisor import install as install_mod
from skill_advisor import paths


def _settings() -> dict:
    return json.loads(paths.settings_file().read_text(encoding="utf-8"))


def _config(*, enabled: bool = False, statusline: bool = True) -> config.Config:
    return config.Config(effort=config.EffortConfig(enabled=enabled, statusline=statusline))


def test_render_settings_points_to_skill_advisor(isolated_paths):
    path = install_mod.render_settings()
    data = json.loads(path.read_text(encoding="utf-8"))
    hook = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert hook["command"].endswith(" hook")
    assert hook["timeout"] == 3000


def test_render_settings_includes_all_three_advisor_hooks(isolated_paths):
    path = install_mod.render_settings()
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data["hooks"]
    assert "UserPromptSubmit" in events
    assert "PostToolUse" in events
    assert "Stop" in events

    def _first_cmd(event_name: str) -> str:
        return events[event_name][0]["hooks"][0]["command"]

    assert _first_cmd("UserPromptSubmit").endswith(" hook")
    assert _first_cmd("PostToolUse").endswith(" posttooluse")
    assert _first_cmd("Stop").endswith(" stop")


def test_render_settings_is_idempotent(isolated_paths):
    first = install_mod.render_settings().read_text(encoding="utf-8")
    second = install_mod.render_settings().read_text(encoding="utf-8")
    assert first == second


def test_render_settings_preserves_user_defined_hooks(isolated_paths):
    """A user's custom Stop hook must survive `skill-advisor install` regeneration."""
    settings_path = paths.settings_file()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"matcher": "", "hooks": [{"type": "command", "command": "/usr/local/bin/my-cleanup.sh", "timeout": 5000}]}
            ],
        },
    }), encoding="utf-8")

    install_mod.render_settings()

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    stop_blocks = data["hooks"]["Stop"]
    commands = [h["command"] for block in stop_blocks for h in block.get("hooks", [])]
    # User's hook survives, and ours is also present.
    assert "/usr/local/bin/my-cleanup.sh" in commands
    assert any(c.endswith(" stop") and "skill-advisor" in c for c in commands)


def test_render_settings_updates_in_place_on_path_change(isolated_paths, monkeypatch):
    """When the absolute path to skill-advisor changes, existing entry is replaced, not duplicated."""
    # First render with one fake which() result.
    monkeypatch.setattr("skill_advisor.install.shutil.which", lambda name: "/old/path/skill-advisor")
    install_mod.render_settings()

    # Second render with a different path.
    monkeypatch.setattr("skill_advisor.install.shutil.which", lambda name: "/new/path/skill-advisor")
    install_mod.render_settings()

    data = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    hooks = [h for block in data["hooks"]["UserPromptSubmit"] for h in block.get("hooks", [])]
    advisor_cmds = [h["command"] for h in hooks if "skill-advisor" in h["command"]]
    assert len(advisor_cmds) == 1  # no duplicate
    assert advisor_cmds[0] == "/new/path/skill-advisor hook"


def test_render_settings_recovers_from_malformed_file(isolated_paths):
    settings_path = paths.settings_file()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("not valid json{", encoding="utf-8")

    install_mod.render_settings()  # should not crash

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "UserPromptSubmit" in data["hooks"]


def test_write_default_config_is_idempotent(isolated_paths):
    first = install_mod.write_default_config()
    mtime = first.stat().st_mtime_ns
    second = install_mod.write_default_config()
    assert first == second
    # Unchanged because force=False.
    assert second.stat().st_mtime_ns == mtime


def test_write_default_config_force_rewrites(isolated_paths):
    first = install_mod.write_default_config()
    paths.config_file().write_text("# user edits\n", encoding="utf-8")
    install_mod.write_default_config(force=True)
    assert "[matcher]" in paths.config_file().read_text(encoding="utf-8")


def test_default_config_text_effort_section_matches_effortconfig_defaults():
    """The `[effort]` block the installer writes must parse to the exact same
    values as `EffortConfig()`'s dataclass defaults — keeps install.py's bundled
    text and config.py's schema from drifting apart silently."""
    import dataclasses
    import tomllib

    text = install_mod._default_config_text()
    parsed = tomllib.loads(text)
    assert "effort" in parsed, "_default_config_text() has no [effort] section"

    expected = dataclasses.asdict(config.EffortConfig())
    assert parsed["effort"] == expected


def test_uncommenting_the_parallelization_example_keeps_judge_timeout_in_section():
    """Regression: `judge_timeout_seconds` previously sat above `# [parallelization]`,
    still inside the preceding [telemetry] section's comment block. A user who
    uncommented the example as shipped would have TOML silently parse the key into
    [telemetry], where config.load() ignores unknown keys -- no error, no effect."""
    import tomllib

    text = install_mod._default_config_text()
    commented_block = (
        "# [parallelization]\n"
        "# enabled = false\n"
        "# min_tasks = 3\n"
        "# judge_timeout_seconds = 5.0\n"
    )
    assert commented_block in text, "shipped [parallelization] example text changed; update this test"
    uncommented_block = "[parallelization]\nenabled = false\nmin_tasks = 3\njudge_timeout_seconds = 5.0\n"

    parsed = tomllib.loads(text.replace(commented_block, uncommented_block))
    assert parsed["parallelization"]["judge_timeout_seconds"] == 5.0
    assert "judge_timeout_seconds" not in parsed.get("telemetry", {})


def test_install_alias_is_idempotent(isolated_paths, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    shell = install_mod.detect_shell()
    assert shell is not None

    changed = install_mod.install_alias(shell)
    assert changed is True
    changed_again = install_mod.install_alias(shell)
    assert changed_again is False

    content = shell.rc_file.read_text(encoding="utf-8")
    assert content.count(install_mod.ALIAS_BEGIN) == 1
    assert content.count(install_mod.ALIAS_END) == 1
    assert "claudeskill" in content


def test_install_alias_updates_when_settings_path_changes(isolated_paths, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    shell1 = install_mod.detect_shell()
    install_mod.install_alias(shell1)

    # Simulate re-install after settings move — strip + reinsert.
    new_line = shell1.alias_line.replace("claudeskill", "claudeskill2")
    shell2 = install_mod.ShellTarget(name=shell1.name, rc_file=shell1.rc_file, alias_line=new_line)
    changed = install_mod.install_alias(shell2)
    assert changed is True
    content = shell1.rc_file.read_text(encoding="utf-8")
    assert content.count(install_mod.ALIAS_BEGIN) == 1
    assert "claudeskill2" in content
    assert "alias claudeskill=" not in content.replace("claudeskill2", "")


def test_uninstall_alias_removes_block(isolated_paths, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    shell = install_mod.detect_shell()
    install_mod.install_alias(shell)
    assert shell.rc_file.is_file()
    removed = install_mod.uninstall_alias(shell)
    assert removed is True
    assert install_mod.ALIAS_BEGIN not in shell.rc_file.read_text(encoding="utf-8")


def test_detect_shell_unknown_returns_none(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/tcsh")
    assert install_mod.detect_shell() is None


def test_detect_shell_fish_uses_config_fish(isolated_paths, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    shell = install_mod.detect_shell()
    assert shell is not None
    assert shell.name == "fish"
    assert shell.rc_file.name == "config.fish"
    assert 'alias claudeskill' in shell.alias_line


def test_statusline_registered_when_enabled(isolated_paths, monkeypatch):
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: _config(enabled=True, statusline=True),
    )
    install_mod.render_settings()
    data = _settings()
    assert data["statusLine"]["type"] == "command"
    assert data["statusLine"]["command"].endswith("statusline.sh")
    assert paths.statusline_script().is_file()

    # OUR entry is upserted in place on every render — a re-run must be idempotent.
    install_mod.render_settings()
    assert _settings()["statusLine"] == data["statusLine"]


def test_statusline_absent_when_feature_disabled(isolated_paths, monkeypatch):
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: _config(),
    )
    install_mod.render_settings()
    assert "statusLine" not in _settings()


def test_render_settings_preserves_effortlevel_written_by_baseline(isolated_paths, monkeypatch):
    """A baseline write must survive a later `skill-advisor install`."""
    paths.ensure_dirs()
    paths.settings_file().write_text(json.dumps({"effortLevel": "high"}), encoding="utf-8")
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: _config(),
    )
    install_mod.render_settings()
    assert _settings()["effortLevel"] == "high"


def test_render_settings_preserves_foreign_statusline(isolated_paths, monkeypatch):
    """Never clobber a status line the user configured themselves."""
    paths.ensure_dirs()
    paths.settings_file().write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/usr/local/bin/mine.sh"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: _config(enabled=True, statusline=True),
    )
    install_mod.render_settings()
    assert _settings()["statusLine"]["command"] == "/usr/local/bin/mine.sh"


def test_render_settings_preserves_foreign_statusline_with_matching_suffix(isolated_paths, monkeypatch):
    """A foreign script that merely ENDS in 'statusline.sh' must not be mistaken for ours.

    Guards against a naive `.endswith("statusline.sh")` check, which would wrongly
    classify "/opt/other/user-statusline.sh" as our own entry and clobber it.
    """
    paths.ensure_dirs()
    paths.settings_file().write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/opt/other/user-statusline.sh"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: _config(enabled=True, statusline=True),
    )
    install_mod.render_settings()
    assert _settings()["statusLine"]["command"] == "/opt/other/user-statusline.sh"


def test_render_settings_honors_settings_file_override_for_statusline(isolated_paths, monkeypatch, tmp_path):
    """Regression test for the bug this feature exists to fix.

    A real Claude Code `--settings` file is not always named
    claudeskill-settings.json. Before SKILL_ADVISOR_SETTINGS_FILE existed, this
    scenario made render_settings() silently write the statusLine registration
    into a SECOND file the user's actual `claude` invocation never reads — no
    error, no warning, the feature just did nothing. With the override set,
    the write must land in the user's actual file, and no default-named file
    may be created alongside it.
    """
    override = tmp_path / "custom-dir" / "claudew-settings.json"
    monkeypatch.setenv("SKILL_ADVISOR_SETTINGS_FILE", str(override))
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: _config(enabled=True, statusline=True),
    )

    result = install_mod.render_settings()

    # Checked FIRST and on its own: pre-fix, render_settings() ignores the
    # override entirely and writes the default-named file instead, so this
    # assertion alone is enough to fail red without depending on anything
    # below it (which pre-fix would instead error out reading a file that
    # was never created at `override`).
    default_named_file = isolated_paths["config_home"] / "claudeskill-settings.json"
    assert not default_named_file.exists()

    assert result == override
    data = json.loads(override.read_text(encoding="utf-8"))
    assert data["statusLine"]["type"] == "command"
    assert data["statusLine"]["command"].endswith("statusline.sh")
    assert "UserPromptSubmit" in data["hooks"]


def test_render_settings_raises_clean_error_on_unwritable_override(monkeypatch, tmp_path):
    """A bad SKILL_ADVISOR_SETTINGS_FILE (typo, permission-restricted parent)
    must fail with a message naming the path and the env var — not a raw
    OSError traceback from the unguarded `paths.ensure_dirs()` call that
    now also has to create this override's parent directory.
    """
    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir()
    readonly_root.chmod(0o500)  # r-x: mkdir of a child directory fails (EACCES)
    override = readonly_root / "nested" / "claudew-settings.json"
    monkeypatch.setenv("SKILL_ADVISOR_SETTINGS_FILE", str(override))

    try:
        with pytest.raises(install_mod.RenderSettingsError) as excinfo:
            install_mod.render_settings()
        message = str(excinfo.value)
        assert str(override) in message
        assert "SKILL_ADVISOR_SETTINGS_FILE" in message
    finally:
        readonly_root.chmod(0o700)  # restore so tmp_path teardown can remove it


def test_render_settings_preserves_foreign_statusline_with_embedded_suffix(isolated_paths, monkeypatch):
    """Same guard, for a command whose tail literally spells 'statusline.sh' without
    being our script at all — e.g. "/opt/other/notstatusline.sh".
    """
    paths.ensure_dirs()
    paths.settings_file().write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/opt/other/notstatusline.sh"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "skill_advisor.install.load_config",
        lambda: _config(enabled=True, statusline=True),
    )
    install_mod.render_settings()
    assert _settings()["statusLine"]["command"] == "/opt/other/notstatusline.sh"
