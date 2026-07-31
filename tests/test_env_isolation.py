"""Guards that the test environment cannot reach the developer's real files.

Every path the tool writes is redirected into `tmp_path` by the autouse
`isolated_paths` fixture. One of them — the Claude Code settings file — is
resolved from an env var that takes precedence over the config dir, so
isolating the config dir alone is NOT sufficient:

    paths.settings_file():
      1. $SKILL_ADVISOR_SETTINGS_FILE   <- wins outright
      2. config_dir() / "claudeskill-settings.json"

The `claudew` alias exports (1) pointing at the user's live settings. Before
this guard, running the suite from a claudew shell made `migrate-excludes` /
`rotate` / installer tests operate on that live file — observed 2026-07-31
deleting it outright and clobbering its `.pre-migrate.bak` with fixture data,
which also made `migrate-excludes --revert` restore garbage.

These tests fail if any writable path escapes tmp_path.
"""

from __future__ import annotations

import os

from skill_advisor import paths


def test_settings_file_is_isolated_to_tmp(isolated_paths, tmp_path):
    """The settings path must live under tmp_path even when the ambient
    environment exports SKILL_ADVISOR_SETTINGS_FILE at a real location."""
    resolved = paths.settings_file()
    assert tmp_path in resolved.parents, (
        f"settings_file() resolved to {resolved}, outside the test sandbox "
        f"{tmp_path} — the suite would read and write the real settings file"
    )


def test_settings_env_var_is_set_not_merely_unset(isolated_paths, tmp_path):
    """Pinning the var beats deleting it: an unset var silently falls back to
    config_dir(), so a future change to that fallback could re-escape without
    any test noticing. Assert the var itself points inside the sandbox."""
    value = os.environ.get("SKILL_ADVISOR_SETTINGS_FILE")
    assert value is not None, "SKILL_ADVISOR_SETTINGS_FILE must be pinned, not unset"
    assert str(tmp_path) in value


def test_every_writable_root_is_under_tmp(isolated_paths, tmp_path):
    """The full set of roots the tool writes to, in one assertion — so adding a
    new writable path without isolating it fails here rather than in the
    developer's home directory."""
    for label, path in (
        ("settings_file", paths.settings_file()),
        ("config_dir", paths.config_dir()),
        ("cache_dir", paths.cache_dir()),
    ):
        assert (
            tmp_path in path.parents or path == tmp_path or tmp_path in path.parents
        ), f"{label} resolved to {path}, outside {tmp_path}"
