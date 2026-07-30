from skill_advisor import paths


def test_overrides_via_env(isolated_paths):
    assert paths.claude_home() == isolated_paths["claude_home"]
    assert paths.config_dir() == isolated_paths["config_home"]
    assert paths.cache_dir() == isolated_paths["cache_home"]


def test_derived_files_live_under_overrides(isolated_paths):
    assert paths.config_file().parent == isolated_paths["config_home"]
    assert paths.settings_file().parent == isolated_paths["config_home"]
    assert paths.catalog_file().parent == isolated_paths["cache_home"]
    assert paths.embeddings_file().parent == isolated_paths["cache_home"]
    assert paths.log_file().parent == isolated_paths["cache_home"]


def test_skill_roots_relative_to_claude_home(isolated_paths):
    roots = paths.skill_roots()
    assert roots[0] == isolated_paths["claude_home"] / "skills"
    assert roots[1] == isolated_paths["claude_home"] / "plugins" / "marketplaces"
    assert roots[2] == isolated_paths["claude_home"] / "plugins" / "cache"


def test_claude_config_dir_takes_precedence_over_default(monkeypatch, tmp_path):
    primary = tmp_path / "work-claude"
    primary.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(primary))
    monkeypatch.setenv("HOME", str(home))
    assert paths.claude_home() == primary


def test_claude_home_env_beats_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "forced"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ignored"))
    assert paths.claude_home() == tmp_path / "forced"


def test_skill_roots_includes_default_fallback_when_primary_differs(
    monkeypatch, tmp_path
):
    primary = tmp_path / "work"
    primary.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(primary))
    monkeypatch.setenv("HOME", str(home))
    roots = paths.skill_roots()
    # Primary first.
    assert roots[0] == primary / "skills"
    assert roots[1] == primary / "plugins" / "marketplaces"
    assert roots[2] == primary / "plugins" / "cache"
    # Default ~/.claude appended as fallback.
    assert (home / ".claude" / "skills") in roots
    assert (home / ".claude" / "plugins" / "marketplaces") in roots
    assert (home / ".claude" / "plugins" / "cache") in roots


def test_skill_roots_no_duplicate_when_primary_equals_default(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    roots = paths.skill_roots()
    # Exactly three entries when primary == $HOME/.claude.
    assert roots == [
        home / ".claude" / "skills",
        home / ".claude" / "plugins" / "marketplaces",
        home / ".claude" / "plugins" / "cache",
    ]


def test_xdg_fallback_without_override(monkeypatch, tmp_path):
    home = tmp_path / "fallback-home"
    home.mkdir()
    monkeypatch.delenv("SKILL_ADVISOR_CONFIG_HOME", raising=False)
    monkeypatch.delenv("SKILL_ADVISOR_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert paths.config_dir() == home / ".config" / "skill-advisor"
    assert paths.cache_dir() == home / ".cache" / "skill-advisor"


def test_ensure_dirs_creates_both(isolated_paths):
    # Remove auto-created dirs first.
    for d in (isolated_paths["config_home"], isolated_paths["cache_home"]):
        for child in d.iterdir():
            child.unlink() if child.is_file() else None
    paths.ensure_dirs()
    assert isolated_paths["config_home"].is_dir()
    assert isolated_paths["cache_home"].is_dir()


def test_effort_paths_live_in_cache_dir():
    assert paths.effort_file().parent == paths.cache_dir()
    assert paths.observed_effort_file().parent == paths.cache_dir()
    assert paths.baseline_file().parent == paths.cache_dir()


def test_statusline_script_lives_in_config_dir():
    assert paths.statusline_script().parent == paths.config_dir()
    assert paths.statusline_script().name == "statusline.sh"


def test_settings_file_default_is_claudeskill_settings_json(isolated_paths):
    assert (
        paths.settings_file()
        == isolated_paths["config_home"] / "claudeskill-settings.json"
    )


def test_settings_file_honors_full_path_override(monkeypatch, tmp_path):
    override = tmp_path / "elsewhere" / "claudew-settings.json"
    monkeypatch.setenv("SKILL_ADVISOR_SETTINGS_FILE", str(override))
    assert paths.settings_file() == override


def test_settings_file_override_can_point_outside_config_dir(
    monkeypatch, tmp_path, isolated_paths
):
    """The override is a FULL PATH — it need not live under config_dir() at all."""
    override = tmp_path / "totally-unrelated-dir" / "settings.json"
    monkeypatch.setenv("SKILL_ADVISOR_SETTINGS_FILE", str(override))
    resolved = paths.settings_file()
    assert resolved == override
    assert resolved.parent != isolated_paths["config_home"]


def test_settings_file_empty_override_treated_as_unset(monkeypatch, isolated_paths):
    monkeypatch.setenv("SKILL_ADVISOR_SETTINGS_FILE", "")
    assert (
        paths.settings_file()
        == isolated_paths["config_home"] / "claudeskill-settings.json"
    )


def test_ensure_dirs_creates_override_settings_parent(monkeypatch, tmp_path):
    override = tmp_path / "deep" / "nested" / "dir" / "claudew-settings.json"
    monkeypatch.setenv("SKILL_ADVISOR_SETTINGS_FILE", str(override))
    assert not override.parent.exists()
    paths.ensure_dirs()
    assert override.parent.is_dir()


def test_skill_roots_dedupes_symlinked_subdirectories(monkeypatch, tmp_path):
    """When primary home's subdirs are symlinks to default home's subdirs,
    skill_roots() should return resolved paths only once."""
    home = tmp_path / "symlink-test-home"
    home.mkdir(parents=True, exist_ok=True)
    default_claude = home / ".claude"
    default_claude.mkdir()
    (default_claude / "skills").mkdir()
    (default_claude / "plugins").mkdir()
    (default_claude / "plugins" / "marketplaces").mkdir()
    (default_claude / "plugins" / "cache").mkdir()

    work_claude = home / ".claude-work"
    work_claude.mkdir()
    (work_claude / "skills").symlink_to(default_claude / "skills")
    (work_claude / "plugins").symlink_to(default_claude / "plugins")

    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work_claude))
    monkeypatch.setenv("HOME", str(home))

    roots = paths.skill_roots()
    resolved_roots = [r.resolve() for r in roots]

    # Should have 3 entries (primary only), not 6 (primary + duplicate default).
    assert len(roots) == 3
    # All resolved paths should be unique.
    assert len(set(resolved_roots)) == 3
    # All should resolve to default_claude subdirs.
    assert resolved_roots[0] == (default_claude / "skills").resolve()
    assert resolved_roots[1] == (
        default_claude / "plugins" / "marketplaces"
    ).resolve()
    assert resolved_roots[2] == (default_claude / "plugins" / "cache").resolve()
