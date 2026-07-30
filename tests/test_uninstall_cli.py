"""Tests for `skill-advisor uninstall` — removal of effort-signalling artifacts."""
from __future__ import annotations

import pytest


def _effort_artifact_paths():
    from skill_advisor import paths

    return [
        paths.effort_file(),
        paths.observed_effort_file(),
        paths.baseline_file(),
        paths.statusline_script(),
    ]


def test_uninstall_removes_effort_artifacts(capsys):
    from skill_advisor import cli, paths

    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.cache_dir().mkdir(parents=True, exist_ok=True)

    for path in _effort_artifact_paths():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["uninstall"])
    assert exc_info.value.code == 0

    for path in _effort_artifact_paths():
        assert not path.exists(), f"{path} should have been removed by uninstall"

    out = capsys.readouterr().out
    for path in _effort_artifact_paths():
        assert f"removed: {path}" in out


def test_uninstall_does_not_raise_when_effort_artifacts_missing():
    from skill_advisor import cli, paths

    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.cache_dir().mkdir(parents=True, exist_ok=True)

    for path in _effort_artifact_paths():
        assert not path.exists()

    # Silent-on-error contract: removing artifacts that were never created
    # must not raise, even though every listed path is missing.
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["uninstall"])
    assert exc_info.value.code == 0
