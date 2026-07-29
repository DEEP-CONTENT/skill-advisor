import json

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
