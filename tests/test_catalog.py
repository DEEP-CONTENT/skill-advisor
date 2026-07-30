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
