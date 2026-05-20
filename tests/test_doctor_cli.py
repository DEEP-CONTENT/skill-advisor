"""Tests for the `skill-advisor doctor` CLI subcommand — parallelization budget check."""
from __future__ import annotations

import pytest


def test_doctor_warns_when_parallelization_budget_too_low(capsys, monkeypatch):
    from skill_advisor import cli, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[matcher]
budget_seconds = 4.0
[parallelization]
enabled = true
judge_timeout_seconds = 12.0
""",
        encoding="utf-8",
    )
    # Doctor may exit non-zero when it flags warnings; tolerate either.
    try:
        cli.main(["doctor"])
    except SystemExit:
        pass
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "parallelization" in out.lower()
    assert "budget_seconds" in out.lower()


def test_doctor_silent_when_parallelization_budget_ok(capsys, monkeypatch):
    from skill_advisor import cli, paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(
        """
[matcher]
budget_seconds = 20.0
[parallelization]
enabled = true
judge_timeout_seconds = 12.0
""",
        encoding="utf-8",
    )
    try:
        cli.main(["doctor"])
    except SystemExit:
        pass
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "parallelization" not in out.lower() or "ok" in out.lower()
