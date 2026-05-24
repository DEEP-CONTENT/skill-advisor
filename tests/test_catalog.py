from pathlib import Path

import pytest

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


def _write_agent_md(path, name, description=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = f"---\nname: {name}\n"
    if description is not None:
        frontmatter += f"description: {description}\n"
    path.write_text(frontmatter + "---\n", encoding="utf-8")


def test_scan_finds_user_agents(isolated_paths):
    agents_dir = isolated_paths["claude_home"] / "agents"
    _write_agent_md(agents_dir / "x.md", "agent-x", "User agent X.")

    entries = catalog.scan()
    names = {entry.name: entry for entry in entries}

    assert "agent-x" in names
    assert names["agent-x"].kind == "subagent"
    assert names["agent-x"].namespace == "user"


def test_namespaced_agents_scanned(isolated_paths):
    agents_dir = isolated_paths["claude_home"] / "agents"
    _write_agent_md(agents_dir / "sub" / "z.md", "agent-z", "Nested user agent Z.")

    entries = catalog.scan()

    assert any(entry.name == "agent-z" for entry in entries)


def test_reviewers_subdir_excluded_from_agents(isolated_paths):
    agents_dir = isolated_paths["claude_home"] / "agents"
    _write_agent_md(agents_dir / "reviewers" / "y.md", "reviewer-y", "Reviewer agent Y.")

    entries = catalog.scan()

    assert all(entry.name != "reviewer-y" for entry in entries)


def test_agents_without_description_dropped(isolated_paths):
    agents_dir = isolated_paths["claude_home"] / "agents"
    _write_agent_md(agents_dir / "nodesc.md", "agent-nodesc")

    entries = catalog.scan()

    assert all(entry.name != "agent-nodesc" for entry in entries)


def test_symlink_agents_outside_home_skipped(isolated_paths, tmp_path):
    agents_dir = isolated_paths["claude_home"] / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-agent.md"
    _write_agent_md(outside, "outside-agent", "Agent outside Claude home.")
    link = agents_dir / "outside.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    entries = catalog.scan()

    assert all(entry.name != "outside-agent" for entry in entries)


def _write_command_md(path, description=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = "---\n"
    if description is not None:
        frontmatter += f"description: {description}\n"
    path.write_text(frontmatter + "---\n", encoding="utf-8")


def test_scan_finds_user_commands(isolated_paths):
    commands_dir = isolated_paths["claude_home"] / "commands"
    _write_command_md(commands_dir / "deploy.md", "Deploy command.")

    entries = catalog.scan()
    names = {entry.name: entry for entry in entries}

    assert "/deploy" in names
    assert names["/deploy"].kind == "command"
    assert names["/deploy"].namespace == "user"


def test_namespaced_commands_scanned(isolated_paths):
    commands_dir = isolated_paths["claude_home"] / "commands"
    _write_command_md(commands_dir / "git" / "sync.md", "Nested sync command.")

    entries = catalog.scan()

    assert any(entry.name == "/sync" for entry in entries)


def test_commands_without_description_dropped(isolated_paths):
    commands_dir = isolated_paths["claude_home"] / "commands"
    _write_command_md(commands_dir / "nodesc.md")

    entries = catalog.scan()

    assert all(entry.name != "/nodesc" for entry in entries)


def test_symlink_commands_outside_home_skipped(isolated_paths, tmp_path):
    commands_dir = isolated_paths["claude_home"] / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-command.md"
    _write_command_md(outside, "Command outside Claude home.")
    link = commands_dir / "outside.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    entries = catalog.scan()

    assert all(entry.name != "/outside" for entry in entries)


def test_manifest_excludes_skill_by_name(fake_claude_home, tmp_path):
    import json

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"excluded_subskills": ["demo-skill"]}),
        encoding="utf-8",
    )
    cfg = Config(catalog=CatalogConfig(catalog_manifest=str(manifest_path)))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "demo-skill" not in names


def test_manifest_and_config_excludes_union(fake_claude_home, tmp_path):
    import json

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"excluded_subskills": ["demo-skill"]}),
        encoding="utf-8",
    )
    cfg = Config(
        catalog=CatalogConfig(
            catalog_manifest=str(manifest_path),
            exclude_names=("/loop",),
        )
    )

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "demo-skill" not in names
    assert "/loop" not in names
    assert "Explore" in names


def test_manifest_missing_is_noop(fake_claude_home):
    cfg = Config(catalog=CatalogConfig(catalog_manifest=""))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "demo-skill" in names
    assert "plugin-skill" in names
    assert "/loop" in names


def test_slash_insensitive_config_strips_leading_slash(fake_claude_home):
    cfg = Config(catalog=CatalogConfig(exclude_names=("loop",)))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "/loop" not in names


def test_slash_insensitive_with_leading_slash(fake_claude_home):
    cfg = Config(catalog=CatalogConfig(exclude_names=("/loop",)))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "/loop" not in names


def test_slash_insensitive_manifest_exclude_matches_command(fake_claude_home, tmp_path):
    import json

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"excluded_subagents": ["loop"]}),
        encoding="utf-8",
    )
    cfg = Config(catalog=CatalogConfig(catalog_manifest=str(manifest_path)))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "/loop" not in names


def test_tristate_whitelist_absent_scans_all_plugins(fake_claude_home, tmp_path):
    import json

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"excluded_subskills": []}), encoding="utf-8")
    cfg = Config(catalog=CatalogConfig(catalog_manifest=str(manifest_path)))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "plugin-skill" in names


def test_tristate_whitelist_empty_excludes_all_plugins(fake_claude_home, tmp_path):
    import json

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"plugin_whitelist": []}), encoding="utf-8")
    cfg = Config(catalog=CatalogConfig(catalog_manifest=str(manifest_path)))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "plugin-skill" not in names
    assert "demo-skill" in names


def test_tristate_whitelist_named_scans_only_listed(fake_claude_home, tmp_path):
    import json

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"plugin_whitelist": ["x"]}), encoding="utf-8")
    cfg = Config(catalog=CatalogConfig(catalog_manifest=str(manifest_path)))

    names = {entry.name for entry in catalog.scan(cfg)}

    assert "plugin-skill" in names

    manifest_path.write_text(
        json.dumps({"plugin_whitelist": ["other"]}),
        encoding="utf-8",
    )
    other_names = {entry.name for entry in catalog.scan(cfg)}

    assert "plugin-skill" not in other_names
