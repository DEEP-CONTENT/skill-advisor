"""Floor-level guard, not a benchmark.

Guards the embedding-only path — `matcher.pick_stateless()` with
`use_judge=False`, the shipped production default (see MatcherConfig in
config.py). The plan's Tasks 4/6 would have added a confidence-based
escalation policy on top of this, but Task 2 found no rule that separates
usefully (see task-2-report.md), so nothing ships a threshold and this
default embedding-only path stays the one and only "confident path" in
production today.

The ceiling is loose on purpose — this must not go flaky on a loaded CI box.
CEILING_SECONDS = 2.0 against a measured local warm cost of ~261 ms
(progress.md) and the README's ~0 ms claim for triage-skipped prompts: that's
~7.7x headroom over the slowest real leg, generous enough to absorb a heavily
loaded CI box. It's still far below the cost of a single reintroduced
subprocess round-trip — `claude -p` measures p50 11,888 ms / p95 24,811 ms
live (progress.md), and even a fast judge call runs 5-15s of session-startup
overhead per MatcherConfig.use_judge's docstring — so this reliably catches a
whole extra `claude -p` call sneaking back into the confident path (a >40x
violation of the ceiling), not a marginal 2x one.

This test also patches `_embed_model` with a fixed in-memory stub, so the
measured elapsed time here never includes fastembed's real load/inference
cost in the first place — the wall-clock assertion is a defense-in-depth
backstop for a subprocess call from *anywhere* in the call graph, not the
primary guard. The primary guard is the explicit explode-on-call patches
below, which fail loudly and immediately if judge.py or parallelization.py
ever spawns a subprocess on this path again.
"""

import time
from unittest.mock import patch

import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import index as index_mod, matcher
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config, MatcherConfig

CEILING_SECONDS = 2.0


def test_the_confident_path_spawns_no_subprocess(isolated_paths):
    entries = [
        CatalogEntry(
            kind="skill",
            name="alpha",
            namespace="user",
            description="d",
            path="/s/alpha/SKILL.md",
            enabled=True,
        ),
        CatalogEntry(
            kind="skill",
            name="beta",
            namespace="user",
            description="d",
            path="/s/beta/SKILL.md",
            enabled=True,
        ),
    ]
    emb = np.array([[1.0, 0.0], [0.2, 0.98]], dtype=np.float32)
    catalog_mod.save(entries)
    index_mod.save(entries, emb, "h")

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    # use_judge=False is the shipped production default — the embedding-only
    # hot path this test guards. (Escalation config from the plan's Tasks 4/6
    # never shipped; see the module docstring.)
    cfg = Config(matcher=MatcherConfig(use_judge=False))

    def _explode(*a, **k):
        raise AssertionError("a subprocess was spawned on the confident path")

    with (
        patch.object(index_mod, "_embed_model", return_value=_Fixed()),
        patch("skill_advisor.judge.subprocess.run", _explode),
        patch("skill_advisor.parallelization.subprocess.run", _explode),
    ):
        started = time.monotonic()
        result = matcher.pick_stateless("q", cfg, top_k=2, candidates=2, threshold=0.0)
        elapsed = time.monotonic() - started

    assert result.picks
    assert elapsed < CEILING_SECONDS, f"confident path took {elapsed:.2f}s"
