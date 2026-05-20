"""Tests for parallelization.detect() — the TodoWrite parallelizability judge."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from skill_advisor.config import Config, ParallelizationConfig


def _envelope(inner: dict | str) -> str:
    inner_text = inner if isinstance(inner, str) else json.dumps(inner)
    return json.dumps({"result": inner_text})


def test_detect_returns_none_when_tasks_empty():
    from skill_advisor import parallelization
    assert parallelization.detect([], Config()) is None


def test_detect_returns_none_when_claude_not_on_path(monkeypatch):
    from skill_advisor import parallelization
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    assert parallelization.detect(["a", "b", "c"], Config()) is None


def test_detect_parses_parallel_reply():
    from skill_advisor import parallelization
    tasks = ["Add parser", "Wire CLI", "Write tests"]
    stdout = _envelope({
        "parallel": True,
        "groups": [[0, 2], [1]],
        "reason": "No shared files across these tasks.",
    })
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )
    with patch("skill_advisor.parallelization.shutil.which", return_value="/usr/bin/claude"), \
         patch("skill_advisor.parallelization.subprocess.run", return_value=completed):
        result = parallelization.detect(tasks, Config())
    assert result is not None
    assert result.parallel is True
    assert result.groups == [[0, 2], [1]]
    assert result.reason.startswith("No shared files")


def test_detect_hallucination_guard_drops_out_of_range_indices():
    from skill_advisor import parallelization
    tasks = ["A", "B", "C"]
    stdout = _envelope({
        "parallel": True,
        "groups": [[0, 99], [1], [2, -1]],
        "reason": "ok",
    })
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )
    with patch("skill_advisor.parallelization.shutil.which", return_value="/usr/bin/claude"), \
         patch("skill_advisor.parallelization.subprocess.run", return_value=completed):
        result = parallelization.detect(tasks, Config())
    assert result is not None
    assert result.parallel is True
    # Out-of-range indices dropped; empty groups removed.
    assert result.groups == [[0], [1], [2]]


def test_detect_downgrades_to_sequential_when_all_groups_wiped():
    """If model claims parallel=true but every group is hallucinated indices,
    downgrade to parallel=false rather than emit a misleading verdict."""
    from skill_advisor import parallelization
    tasks = ["A", "B", "C"]
    stdout = _envelope({
        "parallel": True,
        "groups": [[99, 100], [50]],   # all indices out of range
        "reason": "hallucinated",
    })
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )
    with patch("skill_advisor.parallelization.shutil.which", return_value="/usr/bin/claude"), \
         patch("skill_advisor.parallelization.subprocess.run", return_value=completed):
        result = parallelization.detect(tasks, Config())
    assert result is not None
    assert result.parallel is False
    assert result.groups == []


def test_detect_parses_sequential_reply():
    from skill_advisor import parallelization
    tasks = ["Design schema", "Migrate rows based on schema"]
    stdout = _envelope({
        "parallel": False,
        "groups": [],
        "reason": "Task 2 depends on task 1's schema.",
    })
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )
    with patch("skill_advisor.parallelization.shutil.which", return_value="/usr/bin/claude"), \
         patch("skill_advisor.parallelization.subprocess.run", return_value=completed):
        result = parallelization.detect(tasks, Config())
    assert result is not None
    assert result.parallel is False
    assert result.groups == []


def test_detect_returns_none_on_timeout():
    from skill_advisor import parallelization
    with patch("skill_advisor.parallelization.shutil.which", return_value="/usr/bin/claude"), \
         patch(
             "skill_advisor.parallelization.subprocess.run",
             side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0),
         ):
        assert parallelization.detect(["a", "b", "c"], Config()) is None


def test_detect_returns_none_on_non_json_reply():
    from skill_advisor import parallelization
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="not json", stderr=""
    )
    with patch("skill_advisor.parallelization.shutil.which", return_value="/usr/bin/claude"), \
         patch("skill_advisor.parallelization.subprocess.run", return_value=completed):
        assert parallelization.detect(["a", "b", "c"], Config()) is None


def test_detect_respects_custom_timeout_from_config():
    from skill_advisor import parallelization
    cfg = Config(parallelization=ParallelizationConfig(enabled=True, judge_timeout_seconds=3.0))
    captured = {}
    def _fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=_envelope({
            "parallel": False, "groups": [], "reason": "ok",
        }), stderr="")
    with patch("skill_advisor.parallelization.shutil.which", return_value="/usr/bin/claude"), \
         patch("skill_advisor.parallelization.subprocess.run", side_effect=_fake_run):
        parallelization.detect(["a", "b", "c"], cfg)
    assert captured["timeout"] == 3.0
