from pathlib import Path

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


def test_skill_roots_includes_default_fallback_when_primary_differs(monkeypatch, tmp_path):
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
    # Default ~/.claude appended as fallback.
    assert (home / ".claude" / "skills") in roots
    assert (home / ".claude" / "plugins" / "marketplaces") in roots


def test_skill_roots_no_duplicate_when_primary_equals_default(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    roots = paths.skill_roots()
    # Exactly two entries when primary == $HOME/.claude.
    assert roots == [home / ".claude" / "skills", home / ".claude" / "plugins" / "marketplaces"]


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
