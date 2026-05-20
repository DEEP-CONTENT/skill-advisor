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
