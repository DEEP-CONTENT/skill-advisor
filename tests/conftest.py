"""Shared fixtures — redirect every filesystem path through temp dirs."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Every test gets its own CLAUDE_HOME, XDG config, and XDG cache."""
    claude_home = tmp_path / "claude"
    config_home = tmp_path / "config"
    cache_home = tmp_path / "cache"
    claude_home.mkdir(parents=True, exist_ok=True)
    config_home.mkdir(parents=True, exist_ok=True)
    cache_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("SKILL_ADVISOR_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("SKILL_ADVISOR_CACHE_HOME", str(cache_home))
    # Keep HOME stable so alias installers don't touch the real user's rc.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    return {
        "claude_home": claude_home,
        "config_home": config_home,
        "cache_home": cache_home,
        "home": tmp_path / "home",
    }


@pytest.fixture
def fake_claude_home(isolated_paths):
    """Populate the isolated CLAUDE_HOME with a realistic SKILL.md layout."""
    home = isolated_paths["claude_home"]

    user_skill_dir = home / "skills" / "demo-skill"
    user_skill_dir.mkdir(parents=True)
    (user_skill_dir / "SKILL.md").write_text(
        "---\n"
        'name: demo-skill\n'
        'description: "Use when the user asks about the demo skill test fixture."\n'
        "---\n\n"
        "# Demo skill body.\n",
        encoding="utf-8",
    )

    plugin_skill_dir = home / "plugins" / "marketplaces" / "demo" / "plugins" / "x" / "skills" / "y"
    plugin_skill_dir.mkdir(parents=True)
    (plugin_skill_dir / "SKILL.md").write_text(
        "---\n"
        'name: plugin-skill\n'
        'description: "Plugin-namespaced skill used in catalog scan tests."\n'
        "---\n",
        encoding="utf-8",
    )

    # Malformed skill — missing description; should be dropped with a debug log.
    bad_dir = home / "skills" / "no-desc"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text(
        "---\nname: no-desc\n---\n",
        encoding="utf-8",
    )

    return home
