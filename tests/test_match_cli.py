"""Tests for the `skill-advisor match` CLI subcommand."""
from __future__ import annotations

import io
import json
import sys
from argparse import Namespace
from unittest.mock import patch

import numpy as np
import pytest

from skill_advisor import catalog as catalog_mod
from skill_advisor import cli, index as index_mod
from skill_advisor.catalog import CatalogEntry


def _prime(scores_by_name: list[tuple[str, float]]):
    """Save a catalog + embeddings that produce the given cosine scores for query [1,0,0]."""
    entries = [
        CatalogEntry(kind="skill", name=name, namespace="user", description=f"desc of {name}")
        for name, _ in scores_by_name
    ]
    embeddings = np.zeros((len(entries), 3), dtype=np.float32)
    for i, (_, score) in enumerate(scores_by_name):
        embeddings[i, 0] = score
        embeddings[i, 1] = float(np.sqrt(max(0.0, 1.0 - score * score)))

    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "hash")

    class _FixedEmbed:
        def embed(self, texts):
            q = np.zeros(3, dtype=np.float32)
            q[0] = 1.0
            for _ in texts:
                yield q

    return _FixedEmbed()


def _ns(**overrides) -> Namespace:
    base = dict(
        prompt=None,
        top_k=None,
        candidates=None,
        threshold=None,
        judge=None,
        phase=None,
        show_triage=False,
        json=False,
        match_verbose=False,
    )
    base.update(overrides)
    return Namespace(**base)


def test_match_prints_ranked_picks(isolated_paths, capsys):
    stub = _prime([("alpha", 0.9), ("beta", 0.7), ("gamma", 0.5)])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        rc = cli._cmd_match(_ns(prompt="a test prompt", top_k=3, candidates=3, threshold=0.0))

    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and "gamma" in out
    # Ranked 1 -> 2 -> 3
    assert out.index("alpha") < out.index("beta") < out.index("gamma")


def test_match_threshold_filters(isolated_paths, capsys):
    stub = _prime([("alpha", 0.9), ("beta", 0.5), ("gamma", 0.2)])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        rc = cli._cmd_match(_ns(prompt="p", top_k=3, candidates=3, threshold=0.6))

    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" not in out
    assert "gamma" not in out


def test_match_top_k_capped_by_candidates(isolated_paths, capsys):
    stub = _prime([("alpha", 0.9), ("beta", 0.7), ("gamma", 0.5)])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        rc = cli._cmd_match(_ns(prompt="p", top_k=100, candidates=2, threshold=0.0))

    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    assert "gamma" not in out


def test_match_reads_from_piped_stdin(isolated_paths, capsys, monkeypatch):
    stub = _prime([("alpha", 0.9)])
    monkeypatch.setattr("sys.stdin", io.StringIO("a piped prompt"))  # isatty() is False on StringIO
    with patch.object(index_mod, "_embed_model", return_value=stub):
        rc = cli._cmd_match(_ns(prompt=None, top_k=1, candidates=1, threshold=0.0))

    assert rc == 0
    out = capsys.readouterr().out
    assert "a piped prompt" in out
    assert "alpha" in out


def test_match_tty_without_arg_errors(isolated_paths, capsys, monkeypatch):
    _prime([("alpha", 0.9)])

    class _FakeTTY:
        def isatty(self):
            return True

        def read(self):
            return ""

    monkeypatch.setattr("sys.stdin", _FakeTTY())
    rc = cli._cmd_match(_ns(prompt=None))

    assert rc == 2
    err = capsys.readouterr().err
    assert "provide a prompt" in err


def test_match_empty_prompt_errors(isolated_paths, capsys, monkeypatch):
    _prime([("alpha", 0.9)])
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    rc = cli._cmd_match(_ns(prompt=None))

    assert rc == 2
    err = capsys.readouterr().err
    assert "empty" in err


def test_match_missing_catalog_errors(isolated_paths, capsys):
    # No _prime → index.load() raises FileNotFoundError.
    rc = cli._cmd_match(_ns(prompt="some prompt"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "skill-advisor build" in err


def test_match_json_output_shape(isolated_paths, capsys):
    stub = _prime([("alpha", 0.9), ("beta", 0.7)])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        rc = cli._cmd_match(_ns(
            prompt="refactor the auth middleware", top_k=2, candidates=2, threshold=0.0, json=True,
        ))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["prompt"] == "refactor the auth middleware"
    assert isinstance(payload["picks"], list)
    assert payload["picks"][0]["rank"] == 1
    assert payload["picks"][0]["name"] == "alpha"
    assert payload["picks"][0]["kind"] == "skill"
    assert "triage_skip" not in payload  # absent unless --show-triage


def test_match_show_triage_adds_line(isolated_paths, capsys):
    stub = _prime([("alpha", 0.9)])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        # "thanks" is an ACK — triage would skip.
        rc = cli._cmd_match(_ns(prompt="thanks", show_triage=True, top_k=1, candidates=1, threshold=0.0))

    assert rc == 0
    out = capsys.readouterr().out
    assert "triage:" in out
    assert "would skip" in out


def test_match_show_triage_json_includes_flag(isolated_paths, capsys):
    stub = _prime([("alpha", 0.9)])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        rc = cli._cmd_match(_ns(
            prompt="refactor the auth middleware big enough prompt",
            show_triage=True, json=True, top_k=1, candidates=1, threshold=0.0,
        ))

    payload = json.loads(capsys.readouterr().out)
    assert payload["triage_skip"] is False


def test_match_phase_override_uses_phase_preferences(isolated_paths, capsys):
    # Build a catalog that includes a phase-preferred skill ("fix-review" is in CORRECTION prefs).
    entries = [
        CatalogEntry(kind="skill", name="fix-review", namespace="user", description="fix reviewer findings"),
        CatalogEntry(kind="skill", name="unrelated", namespace="user", description="unrelated skill"),
    ]
    embeddings = np.eye(2, 3, dtype=np.float32)
    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "hash")

    # No need to patch _embed_model — phase path never calls top_k.
    rc = cli._cmd_match(_ns(prompt="ignored content", phase="correction", top_k=3))

    assert rc == 0
    out = capsys.readouterr().out
    assert "fix-review" in out
    assert "phase preference" in out


def test_match_no_judge_flag_overrides_config(isolated_paths, capsys, monkeypatch):
    from skill_advisor.config import Config, MatcherConfig
    stub = _prime([("alpha", 0.9)])

    # Force config.use_judge=True via load_config patch.
    monkeypatch.setattr(cli, "load_config", lambda: Config(matcher=MatcherConfig(use_judge=True)))

    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        rc = cli._cmd_match(_ns(prompt="some prompt", judge=False, top_k=1, candidates=1, threshold=0.0))

    assert rc == 0
    mock_rank.assert_not_called()


def test_match_verbose_prints_description_and_path(isolated_paths, capsys):
    entries = [
        CatalogEntry(
            kind="skill", name="alpha", namespace="user",
            description="the alpha skill description",
            path="/some/skills/alpha/SKILL.md",
        ),
    ]
    embeddings = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "hash")

    class _FixedEmbed:
        def embed(self, texts):
            q = np.array([1.0, 0.0, 0.0], dtype=np.float32)
            for _ in texts:
                yield q

    with patch.object(index_mod, "_embed_model", return_value=_FixedEmbed()):
        rc = cli._cmd_match(_ns(
            prompt="p", top_k=1, candidates=1, threshold=0.0, match_verbose=True,
        ))

    assert rc == 0
    out = capsys.readouterr().out
    assert "the alpha skill description" in out
    assert "/some/skills/alpha/SKILL.md" in out
