from pathlib import Path

from skill_advisor import catalog
from skill_advisor.config import CatalogConfig, Config


def test_scan_finds_user_and_plugin_skills(fake_claude_home):
    entries = catalog.scan()
    names = {e.name: e for e in entries}
    assert "demo-skill" in names
    assert names["demo-skill"].namespace == "user"
    assert names["demo-skill"].kind == "skill"
    assert "plugin-skill" in names
    assert names["plugin-skill"].namespace == "plugin:x"
    # Malformed skill (missing description) is dropped.
    assert "no-desc" not in names


def test_scan_includes_builtin_subagents_and_commands(fake_claude_home):
    entries = catalog.scan()
    kinds = {e.kind for e in entries}
    assert "subagent" in kinds
    assert "command" in kinds
    names = {e.name for e in entries}
    assert "Explore" in names
    assert "/loop" in names


def test_exclude_names_filters_catalog(fake_claude_home):
    cfg = Config(catalog=CatalogConfig(exclude_names=("demo-skill", "/loop")))
    entries = catalog.scan(cfg)
    names = {e.name for e in entries}
    assert "demo-skill" not in names
    assert "/loop" not in names
    # Other builtins still present.
    assert "Explore" in names


def test_extra_roots_scanned(fake_claude_home, tmp_path):
    extra = tmp_path / "team-skills" / "analytics"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text(
        "---\nname: team-analytics\ndescription: Team-internal analytics helper.\n---\n",
        encoding="utf-8",
    )
    cfg = Config(catalog=CatalogConfig(extra_roots=(str(tmp_path / "team-skills"),)))
    entries = catalog.scan(cfg)
    assert any(e.name == "team-analytics" for e in entries)


def test_save_load_round_trip(fake_claude_home):
    entries = catalog.scan()
    saved = catalog.save(entries)
    assert Path(saved).is_file()
    loaded = catalog.load()
    assert [e.name for e in loaded] == [e.name for e in entries]


def test_compute_hash_changes_when_skill_description_changes(fake_claude_home):
    entries = catalog.scan()
    h1 = catalog.compute_hash(entries)

    # Bump mtime on an actual skill file.
    target = fake_claude_home / "skills" / "demo-skill" / "SKILL.md"
    original = target.read_text(encoding="utf-8")
    target.write_text(original + "\n# edit\n", encoding="utf-8")

    entries2 = catalog.scan()
    h2 = catalog.compute_hash(entries2)
    assert h1 != h2


def test_parse_frontmatter_rejects_non_mapping(tmp_path):
    f = tmp_path / "weird.md"
    f.write_text("---\n- list item\n---\nbody\n", encoding="utf-8")
    assert catalog.parse_frontmatter(f) is None


def test_parse_frontmatter_returns_none_without_frontmatter(tmp_path):
    f = tmp_path / "plain.md"
    f.write_text("# No frontmatter here\n", encoding="utf-8")
    assert catalog.parse_frontmatter(f) is None


def test_scan_marks_disabled_skills(fake_claude_home):
    from skill_advisor import catalog
    from skill_advisor.config import Config

    entries = catalog.scan(Config(), overrides_table={"demo-skill": "off"})
    demo = next(e for e in entries if e.name == "demo-skill")
    assert demo.enabled is False
    # Still present — the rotation pool needs it.
    assert demo in entries


def test_scan_defaults_to_enabled_with_no_overrides(fake_claude_home):
    from skill_advisor import catalog
    from skill_advisor.config import Config

    entries = catalog.scan(Config(), overrides_table={})
    assert all(e.enabled for e in entries)


def test_scan_sets_invoke_name_for_plugin_skills(fake_claude_home):
    from skill_advisor import catalog
    from skill_advisor.config import Config

    entries = catalog.scan(Config(), overrides_table={})
    plug = next(e for e in entries if e.namespace == "plugin:x")
    assert plug.invoke_name == "x:y"


def test_scan_resolves_enabled_off_the_directory_not_the_frontmatter_name(
    fake_claude_home,
):
    """`~/.claude/skills/xlsx/` declares `name: xlsx-official`. settings.json
    keys skillOverrides by directory (`xlsx`), not by frontmatter `name`.
    Both directions must hold, or a join on `entry.name` would slip through:
    off-by-directory disables it, off-by-frontmatter-name does NOT."""
    from skill_advisor import catalog
    from skill_advisor.config import Config

    off_by_dir = catalog.scan(Config(), overrides_table={"xlsx": "off"})
    entry = next(e for e in off_by_dir if e.name == "xlsx-official")
    assert entry.enabled is False

    off_by_frontmatter_name = catalog.scan(
        Config(), overrides_table={"xlsx-official": "off"}
    )
    entry2 = next(e for e in off_by_frontmatter_name if e.name == "xlsx-official")
    assert entry2.enabled is True


def test_hash_changes_when_a_skill_is_disabled(fake_claude_home):
    """Toggling skillOverrides changes no file mtime. If the hash misses it,
    `build` no-ops and `rotate --apply` silently does nothing."""
    from skill_advisor import catalog
    from skill_advisor.config import Config

    on = catalog.scan(Config(), overrides_table={})
    off = catalog.scan(Config(), overrides_table={"demo-skill": "off"})
    assert catalog.compute_hash(on) != catalog.compute_hash(off)


def test_scan_finds_versioned_plugin_cache_skills(isolated_paths):
    """55 SKILL.md files live under plugins/cache/<marketplace>/<plugin>/<version>/skills/,
    a layout the marketplaces glob never matched. The whole superpowers plugin
    was invisible, which is why every `superpowers:*` phase preference missed."""
    from skill_advisor import catalog
    from skill_advisor.config import Config

    d = (
        isolated_paths["claude_home"]
        / "plugins"
        / "cache"
        / "official"
        / "superpowers"
        / "6.2.0"
        / "skills"
        / "writing-plans"
    )
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        '---\nname: writing-plans\ndescription: "Use when you have a spec."\n---\n',
        encoding="utf-8",
    )

    entries = catalog.scan(Config(), overrides_table={})
    got = next(e for e in entries if e.name == "writing-plans")
    assert got.namespace == "plugin:superpowers"
    assert got.invoke_name == "superpowers:writing-plans"


def test_scan_keeps_both_user_and_plugin_skills_with_same_name(isolated_paths):
    """User skill 'foo' and plugin skill 'foo' are distinct, invocable, and both
    survive scan() because the dedup key is invoke_name, not frontmatter name.
    Before the fix, the second would be discarded."""
    from skill_advisor import catalog
    from skill_advisor.config import Config

    # Create user skill named 'brainstorming'
    user_d = isolated_paths["claude_home"] / "skills" / "brainstorming"
    user_d.mkdir(parents=True)
    (user_d / "SKILL.md").write_text(
        '---\nname: brainstorming\ndescription: "User brainstorming skill."\n---\n',
        encoding="utf-8",
    )

    # Create plugin cache skill also named 'brainstorming'
    plugin_d = (
        isolated_paths["claude_home"]
        / "plugins"
        / "cache"
        / "official"
        / "superpowers"
        / "6.2.0"
        / "skills"
        / "brainstorming"
    )
    plugin_d.mkdir(parents=True)
    (plugin_d / "SKILL.md").write_text(
        '---\nname: brainstorming\ndescription: "Plugin brainstorming skill."\n---\n',
        encoding="utf-8",
    )

    entries = catalog.scan(Config(), overrides_table={})
    entries_by_invoke = {e.invoke_name: e for e in entries}

    # Both should be in the catalog with different invoke_names
    assert "brainstorming" in entries_by_invoke
    assert "superpowers:brainstorming" in entries_by_invoke

    user_entry = entries_by_invoke["brainstorming"]
    plugin_entry = entries_by_invoke["superpowers:brainstorming"]

    assert user_entry.namespace == "user"
    assert user_entry.name == "brainstorming"
    assert plugin_entry.namespace == "plugin:superpowers"
    assert plugin_entry.name == "brainstorming"


def test_scan_dedupes_multiple_versions_of_same_plugin_skill(isolated_paths):
    """Multiple installed versions of the same plugin skill collapse to one entry,
    with the lowest version string winning due to sorted() in the glob."""
    from skill_advisor import catalog
    from skill_advisor.config import Config

    # Create two versions of the same skill
    for version in ("5.0.0", "6.2.0"):
        d = (
            isolated_paths["claude_home"]
            / "plugins"
            / "cache"
            / "official"
            / "superpowers"
            / version
            / "skills"
            / "writing-plans"
        )
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f'---\nname: writing-plans\ndescription: "Plan skill v{version}."\n---\n',
            encoding="utf-8",
        )

    entries = catalog.scan(Config(), overrides_table={})
    writing_plans = [e for e in entries if e.invoke_name == "superpowers:writing-plans"]

    # Only one entry should survive; the lowest version string wins
    assert len(writing_plans) == 1
    assert writing_plans[0].namespace == "plugin:superpowers"
    assert writing_plans[0].name == "writing-plans"


def test_from_json_ignores_unknown_keys():
    """A catalog.json written by a newer binary can carry keys an older
    installed CatalogEntry doesn't know about. from_json must drop them
    rather than blow up with a TypeError that hook.run() can't distinguish
    from a real failure."""
    data = {
        "kind": "skill",
        "name": "demo",
        "namespace": "user",
        "description": "A demo skill.",
        "path": "",
        "enabled": True,
        "invoke_name": "demo",
        "some_future_field": "unexpected",
    }
    entry = catalog.CatalogEntry.from_json(data)
    assert entry.name == "demo"
    assert not hasattr(entry, "some_future_field")


def test_from_json_missing_optional_fields_uses_defaults():
    data = {
        "kind": "skill",
        "name": "demo",
        "namespace": "user",
        "description": "A demo skill.",
    }
    entry = catalog.CatalogEntry.from_json(data)
    assert entry.path == ""
    assert entry.enabled is True
    assert entry.invoke_name == ""


def test_from_json_raises_on_missing_required_field():
    data = {
        "kind": "skill",
        "namespace": "user",
        "description": "A demo skill.",
        # "name" is missing — this is corruption, not a schema drift, and
        # must still raise rather than be silently swallowed.
    }
    try:
        catalog.CatalogEntry.from_json(data)
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError for missing required field")


def test_pool_health_unparseable_count_is_per_file_not_deduped(isolated_paths, tmp_path):
    """When two roots contain unparseable files with the same directory name,
    the per-file count must be used for arithmetic, not the deduplicated count."""
    import unittest.mock

    from skill_advisor import paths as paths_mod

    # Create two roots, each with an unparseable skill directory named "broken"
    root1 = tmp_path / "root1"
    root1.mkdir()
    (root1 / "broken").mkdir()
    (root1 / "broken" / "SKILL.md").write_text("# No frontmatter\n", encoding="utf-8")
    (root1 / "good").mkdir()
    (root1 / "good" / "SKILL.md").write_text(
        "---\nname: good-skill\ndescription: A working skill\n---\n# Content\n",
        encoding="utf-8",
    )

    # Create a second root with another "broken" directory
    root2 = tmp_path / "root2"
    root2.mkdir()
    (root2 / "broken").mkdir()
    (root2 / "broken" / "SKILL.md").write_text(
        "# Another no frontmatter\n", encoding="utf-8"
    )

    # Mock skill_roots to return both roots
    def mocked_roots():
        return [root1, root2]

    with unittest.mock.patch.object(
        paths_mod, "skill_roots", side_effect=mocked_roots
    ):
        health = catalog.pool_health()

    # Should find 3 files total: 2 unparseable (in two "broken" dirs), 1 parseable
    assert health["skill_md_files"] == 3
    assert health["parseable"] == 1
    assert health["unparseable_count"] == 2  # per-file count
    # unparseable_dirs will be deduplicated to just ["broken"]
    assert health["unparseable_dirs"] == ["broken"]
    # Reconciliation: parseable + unparseable_count == skill_md_files must hold
    assert (
        health["parseable"] + health["unparseable_count"]
        == health["skill_md_files"]
    )
