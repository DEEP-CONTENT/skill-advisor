"""Tests for the `skill-advisor replay` CLI subcommand.

Previously zero coverage — see F1 fix-round-2: `_cmd_replay` both emitted
the frontmatter `name` instead of `invoke_name` (the same bug fixed at
every other emission site) AND, independently, crashed on any prompt that
actually produced picks: `picks = matcher.pick(prompt, cfg)` assigns a
`PickResult` (or `None`), not a list, so the old `for p in picks` iterated
the `PickResult` object itself and raised
`TypeError: 'PickResult' object is not iterable`. The missing test
coverage is exactly why both slipped through.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import cli, index as index_mod
from skill_advisor.catalog import CatalogEntry


class _FixedEmbed:
    """Deterministic embedder: every prompt scores highest against the
    single catalog entry set up by `_prime_diverging_entry`."""

    def embed(self, texts):
        q = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        for _ in texts:
            yield q


def _prime_diverging_entry() -> None:
    """One skill whose invoke_name diverges from its frontmatter name —
    the xlsx/xlsx-official pattern used elsewhere in this suite, and the
    exact shape of F1's reported live repro (a plugin skill's bare
    frontmatter name vs its namespaced invoke_name)."""
    entries = [
        CatalogEntry(
            kind="skill",
            name="brainstorming",
            namespace="plugin:superpowers",
            description="Explore an idea before building it.",
            path="/plugins/superpowers/skills/brainstorming/SKILL.md",
            invoke_name="superpowers:brainstorming",
        ),
    ]
    embeddings = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "hash")


def test_replay_renders_invoke_name_not_frontmatter_name(
    isolated_paths, tmp_path, capsys
):
    """The pick shown for each replayed prompt must be the string the Skill
    tool actually accepts, not the frontmatter `name:` — same bug already
    fixed in inject.py and the `match` renderer."""
    _prime_diverging_entry()
    prompts_file = tmp_path / "prompts.txt"
    prompts_file.write_text("let's brainstorm something creative\n", encoding="utf-8")

    with patch.object(index_mod, "_embed_model", return_value=_FixedEmbed()):
        rc = cli._cmd_replay(argparse.Namespace(prompts=str(prompts_file)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "superpowers:brainstorming (skill)" in out
    assert "brainstorming (skill)" not in out.replace(
        "superpowers:brainstorming (skill)", ""
    )


def test_replay_handles_multiple_prompts_including_a_skip(
    isolated_paths, tmp_path, capsys
):
    """Guards the independent crash this test file's absence let through:
    `matcher.pick()` returns a `PickResult`, not a list — iterating it
    directly (rather than its `.picks` attribute) raised TypeError for any
    prompt that produced picks at all. A JSONL line and a plain-text line
    must both replay without raising, and the summary line must print."""
    _prime_diverging_entry()
    prompts_file = tmp_path / "prompts.jsonl"
    prompts_file.write_text(
        '{"prompt": "let\'s brainstorm something creative"}\n'
        "thanks\n",  # triage-skipped -> matcher.pick() returns None -> "<skip>"
        encoding="utf-8",
    )

    with patch.object(index_mod, "_embed_model", return_value=_FixedEmbed()):
        rc = cli._cmd_replay(argparse.Namespace(prompts=str(prompts_file)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "superpowers:brainstorming (skill)" in out
    assert "<skip>" in out
    assert "n=2" in out


def test_replay_missing_prompts_file_errors(isolated_paths, tmp_path, capsys):
    rc = cli._cmd_replay(argparse.Namespace(prompts=str(tmp_path / "nope.txt")))
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err
