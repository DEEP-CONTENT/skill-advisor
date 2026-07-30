import json

from skill_advisor import overrides, paths


def test_override_key_is_the_directory_name_not_the_frontmatter_name():
    """settings.json keys by directory; the catalog keys by frontmatter `name`.
    They differ for 7 skills on the author's machine — `xlsx` vs `xlsx-official`."""
    key = overrides.override_key(
        kind="skill",
        namespace="user",
        path="/home/u/.claude/skills/xlsx/SKILL.md",
        name="xlsx-official",
    )
    assert key == "xlsx"


def test_override_key_is_none_for_builtins():
    assert (
        overrides.override_key(
            kind="subagent", namespace="builtin", path="", name="Plan"
        )
        is None
    )
    assert (
        overrides.override_key(
            kind="command", namespace="builtin", path="", name="review"
        )
        is None
    )


def test_invoke_name_namespaces_plugin_skills():
    """Claude Code invokes plugin skills as `<plugin>:<skill>`; the catalog
    currently emits the bare frontmatter name, which the Skill tool rejects."""
    got = overrides.invoke_name(
        kind="skill",
        namespace="plugin:dc-sprints",
        path="/x/marketplaces/m/plugins/dc-sprints/skills/dc-board-overview/SKILL.md",
        name="dc-board-overview",
    )
    assert got == "dc-sprints:dc-board-overview"


def test_invoke_name_is_the_directory_name_for_user_skills():
    got = overrides.invoke_name(
        kind="skill",
        namespace="user",
        path="/home/u/.claude/skills/xlsx/SKILL.md",
        name="xlsx-official",
    )
    assert got == "xlsx"


def test_missing_key_means_enabled():
    assert overrides.is_enabled("never-seen", {}) is True
    assert overrides.is_enabled(None, {"anything": "off"}) is True


def test_off_means_disabled():
    assert overrides.is_enabled("muted", {"muted": "off"}) is False


def test_read_merges_advisor_settings_over_claude_settings(isolated_paths):
    (isolated_paths["claude_home"] / "settings.json").write_text(
        json.dumps({"skillOverrides": {"a": "off", "b": "off"}}), encoding="utf-8"
    )
    paths.settings_file().write_text(
        json.dumps({"skillOverrides": {"b": "on"}}), encoding="utf-8"
    )
    table = overrides.read()
    assert table["a"] == "off"
    assert table["b"] == "on"


def test_read_survives_malformed_settings(isolated_paths):
    paths.settings_file().write_text("{ not json", encoding="utf-8")
    assert overrides.read() == {}
