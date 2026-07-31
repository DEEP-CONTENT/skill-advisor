"""End-to-end: observe prompts -> update centroids -> score -> propose -> apply
-> rebuild catalog -> confirm the matcher's pickable set actually changed.

Unit tests calling `rotate.score_pool`, `overrides.write`, `catalog.compute_hash`
etc. in isolation prove each function works when called the way its OWN test
calls it. They cannot catch a defect in the SEQUENCE production actually runs
them in — the effort work's worst defect was exactly that: a dead code path
390 unit tests reported healthy because they never drove the real order.

These tests drive the real production chain end to end, through the real
`catalog.scan()` -> `build` -> `rotate --apply` -> `build` hash-gate -> matcher
`top_k`, with no shortcut construction of an in-memory `Index`. Two coverage
gaps motivate the two non-trivial tests below:

  * The `entry.name` <-> `invoke_name` key-space translation in
    `cli._rotation_stats` was previously provably untested: forcing its
    `invoke_to_name` map empty left the entire 539-test suite green. See
    `test_stop_event_key_space_translation_shields_a_diverging_skill`.
  * `catalog.compute_hash` must fold in `enabled`, or `skill-advisor build`
    silently no-ops after `rotate --apply` (toggling skillOverrides moves no
    file mtime). Both `test_full_rotation_loop_changes_what_the_matcher_can_pick`
    (through the real hash-gated `build` command) and
    `test_the_catalog_hash_actually_changed` (a direct, cheap pin) guard it.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import centroids, cli, index as index_mod, matcher, paths
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config

DIM = centroids.DIM


def _vec(x: float) -> np.ndarray:
    """A unit vector on the plane spanned by axes 0/1, whose cosine against
    the axis-0 unit vector is exactly `x`. Lets a test dial in an exact
    semantic_fit (for rotate) or an exact query similarity (for the matcher)
    per catalog entry, without touching the real embedding model."""
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = x
    v[1] = float(np.sqrt(max(0.0, 1.0 - x * x)))
    return v


# Cosine of this against any `_vec(x)` with x in [-1, 1] is comfortably below
# every threshold used here, so entries that fall through to it (builtins —
# always present via `catalog.scan()`, never explicitly mapped) never win a
# `top_k` slot or leak into a `pick_stateless` result.
_FALLBACK = _vec(-0.99)


class _StubEmbedder:
    """Deterministic fastembed stand-in: exact-text lookup, or `fallback`.

    No test here may embed with the real model — `index._embed_model` is
    monkeypatched to return one of these for the whole test, so `index.build`
    and `index.embed_one` never import fastembed or touch the network.
    """

    def __init__(
        self, mapping: dict[str, np.ndarray], fallback: np.ndarray = _FALLBACK
    ):
        self._mapping = mapping
        self._fallback = fallback

    def embed(self, texts):
        for t in texts:
            yield self._mapping.get(t, self._fallback)


def _write_skill(claude_home: Path, dirname: str, name: str, description: str) -> None:
    d = claude_home / "skills" / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: "{name}"\ndescription: "{description}"\n---\n\nBody.\n',
        encoding="utf-8",
    )


def _ns(**kw) -> argparse.Namespace:
    """Namespace stub matching what the `rotate` argparse parser produces."""
    base = {"apply": False, "target": None, "limit": None, "verbose": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _rotation_config_text(**kw) -> str:
    cfg = {
        "target_active": 1,
        "min_active": 0,
        "hysteresis": 0.0,
        "exploration_fraction": 0.0,
        "recency_days": 0,
        "min_observed_prompts": 200,
    }
    cfg.update(kw)
    body = "\n".join(f"{k} = {v}" for k, v in cfg.items())
    return f"[rotation]\n{body}\n"


def test_full_rotation_loop_changes_what_the_matcher_can_pick(
    isolated_paths, monkeypatch
):
    """observe -> centroids -> `rotate --apply` -> a REAL `build` rebuild
    (through the hash-gate, not a bypass) -> the matcher's reachable set
    actually flips.

    `fits` starts disabled and semantically on-topic; `stale` starts enabled
    and off-topic. Only a rotation that (a) writes skillOverrides AND (b) is
    then noticed by `build`'s hash-unchanged short-circuit can flip which one
    the matcher can reach — this is "the assertion that matters": what the
    user-facing matcher can pick, not merely that some file was written.
    """
    claude_home = isolated_paths["claude_home"]
    _write_skill(
        claude_home,
        "stale",
        "stale",
        "Unrelated filler skill for the rotation e2e test.",
    )
    _write_skill(
        claude_home,
        "fits",
        "fits",
        "On-topic skill matching the observed prompt centroid.",
    )

    stale_text = "stale: Unrelated filler skill for the rotation e2e test."
    fits_text = "fits: On-topic skill matching the observed prompt centroid."
    prompt = "please help me with the on-topic thing"
    mapping = {
        stale_text: _vec(0.10),
        fits_text: _vec(0.99),
        prompt: _vec(0.99),
    }
    monkeypatch.setattr(index_mod, "_embed_model", lambda: _StubEmbedder(mapping))

    # `fits` starts OFF in the advisor's own settings file (never Claude
    # Code's ~/.claude/settings.json — see overrides.py).
    paths.ensure_dirs()
    paths.settings_file().write_text(
        json.dumps({"skillOverrides": {"fits": "off"}}), encoding="utf-8"
    )

    # 1. First real build: scan the fixture skills + builtins off disk, embed
    # (stubbed), write catalog.json / embeddings.npz / catalog.hash.
    assert cli._cmd_build(argparse.Namespace(force=False)) == 0

    # 2. Observe prompts, all in the region `fits` occupies.
    sketch = centroids.empty()
    for _ in range(250):
        centroids.observe(sketch, _vec(0.99))
    centroids.save(sketch)

    # Before: the matcher can reach `stale` but not the still-disabled `fits`.
    before = matcher.pick_stateless(
        prompt, Config(), top_k=2, candidates=2, threshold=0.0
    )
    assert [p.entry.name for p in before.picks] == ["stale"]

    # 3. Rotate and apply.
    paths.config_file().write_text(_rotation_config_text(), encoding="utf-8")
    assert cli._cmd_rotate(_ns(apply=True)) == 0

    written = json.loads(paths.settings_file().read_text(encoding="utf-8"))[
        "skillOverrides"
    ]
    assert written["fits"] == "on"
    assert written["stale"] == "off"

    # 4. Rebuild through the REAL `build` command — the same one a human runs
    # after `rotate --apply` prints "run `skill-advisor build`". This is the
    # hash-gate: if `compute_hash` doesn't fold in `enabled`, this call sees
    # the same hash as step 1 (no fixture file's mtime moved) and no-ops,
    # leaving catalog.json's stale `enabled` flags in place.
    assert cli._cmd_build(argparse.Namespace(force=False)) == 0

    # 5. After: the matcher reaches `fits` and can no longer reach `stale`.
    after = matcher.pick_stateless(
        prompt, Config(), top_k=2, candidates=2, threshold=0.0
    )
    assert [p.entry.name for p in after.picks] == ["fits"]


def test_stop_event_key_space_translation_shields_a_diverging_skill(
    isolated_paths, monkeypatch
):
    """`kind=prompt` telemetry keys picks by `entry.name`; `kind=stop` keys
    invocations by `invoke_name` (the directory name Claude Code's Skill tool
    actually accepts). `cli._rotation_stats` reconciles the two so a skill
    whose frontmatter `name:` diverges from its directory still gets credit
    for a recent invocation and is shielded from demotion.

    `create-database-migration` (directory) declares
    `name: "Create database migration"` — mirroring the real xlsx/
    xlsx-official divergence documented in overrides.py. It scores worst in
    the pool (lowest semantic fit, thin pick history), so absent the recency
    shield it is the obvious — indeed the ONLY possible — demotion target
    given `target_active=2` against 3 incumbents. A `kind=stop` event from 2
    days ago (well inside `recency_days=30`), keyed by its `invoke_name`,
    must still protect it. Provable only by going through the real
    `_cmd_rotate` (which calls `_rotation_stats` itself) — not by calling
    `rotate.score_pool` directly with hand-translated stats, which is exactly
    what the previous test suite did and why this was untested.
    """
    claude_home = isolated_paths["claude_home"]
    _write_skill(
        claude_home,
        "create-database-migration",
        "Create database migration",
        "Generates a new database migration file.",
    )
    _write_skill(
        claude_home, "also-active", "also-active", "A skill with steady usage."
    )
    _write_skill(
        claude_home, "third-active", "third-active", "A skill nobody has picked yet."
    )

    a_text = "Create database migration: Generates a new database migration file."
    b_text = "also-active: A skill with steady usage."
    c_text = "third-active: A skill nobody has picked yet."
    mapping = {a_text: _vec(0.01), b_text: _vec(0.50), c_text: _vec(0.60)}
    monkeypatch.setattr(index_mod, "_embed_model", lambda: _StubEmbedder(mapping))

    assert cli._cmd_build(argparse.Namespace(force=False)) == 0

    # Sketch pinned to axis 0 so `_vec(x)`'s cosine against it is exactly x —
    # the same trick as `_vec` itself; gives A/B/C exactly the fits above.
    sketch = centroids.empty(k=2)
    axis0 = np.zeros(DIM, dtype=np.float32)
    axis0[0] = 1.0
    sketch.vectors[0] = axis0
    sketch.counts[0] = 1000
    sketch.observed = 1000
    centroids.save(sketch)

    now = datetime.now(timezone.utc)

    def _prompt_event(name: str) -> str:
        return json.dumps(
            {
                "schema": 1,
                "kind": "prompt",
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "picks": [{"rank": 1, "name": name, "kind": "skill", "score": 0.5}],
            }
        )

    # A: 1 pick (entry.name-keyed, as real kind=prompt events always are) +
    # 1 recent invocation (invoke_name-keyed, as real kind=stop events always
    # are). B: 5 picks, no invocations — sets max_picks so A's pick_rate
    # stays small and A remains the pool's lowest scorer either way. C: no
    # usage at all.
    events = [_prompt_event("Create database migration")]
    events += [_prompt_event("also-active") for _ in range(5)]
    events.append(
        json.dumps(
            {
                "schema": 1,
                "kind": "stop",
                "ts": (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "skills": ["create-database-migration"],
            }
        )
    )
    paths.ensure_dirs()
    paths.events_file().write_text("\n".join(events) + "\n", encoding="utf-8")

    paths.config_file().write_text(
        _rotation_config_text(target_active=2, recency_days=30), encoding="utf-8"
    )

    assert cli._cmd_rotate(_ns(apply=True)) == 0

    table: dict[str, str] = {}
    if paths.settings_file().is_file():
        table = json.loads(paths.settings_file().read_text(encoding="utf-8")).get(
            "skillOverrides", {}
        )
    assert table.get("create-database-migration") != "off", (
        "the recently-invoked diverging-name skill was demoted — the "
        "invoke_name -> name translation in cli._rotation_stats is not "
        "reaching rotate.score_pool's recency shield"
    )


def test_the_catalog_hash_actually_changed():
    """Guards the Task 2 failure mode directly: if `compute_hash` ignores
    `enabled`, `build` no-ops after a rotation and the rebuild step in
    `test_full_rotation_loop_changes_what_the_matcher_can_pick` above would
    silently do nothing in real use."""
    a = [
        CatalogEntry(
            kind="skill",
            name="x",
            namespace="user",
            description="d",
            path="/s/x/SKILL.md",
            enabled=True,
        )
    ]
    b = [
        CatalogEntry(
            kind="skill",
            name="x",
            namespace="user",
            description="d",
            path="/s/x/SKILL.md",
            enabled=False,
        )
    ]
    assert catalog_mod.compute_hash(a) != catalog_mod.compute_hash(b)
