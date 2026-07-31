"""Tests for the `skill-advisor rotate` CLI subcommand."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone

import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import centroids, index as index_mod, paths
from skill_advisor.catalog import CatalogEntry


def _ns(**kw):
    """Namespace stub matching what the `rotate` parser produces."""
    base = {"apply": False, "target": None, "limit": None, "verbose": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _emb(values: list[float]) -> np.ndarray:
    """Unit vectors on the plane spanned by the first two embedding axes.

    `x` is the cosine similarity each row will have against a centroid
    pinned to axis 0 (see `_prime_rotatable_state`), so callers can dial in
    an exact semantic_fit per entry.
    """
    out = np.zeros((len(values), centroids.DIM), dtype=np.float32)
    for i, x in enumerate(values):
        out[i, 0] = x
        out[i, 1] = float(np.sqrt(max(0.0, 1.0 - x * x)))
    return out


def _write_rotation_config(min_observed_prompts: int = 200) -> None:
    """A small target/floor so a handful of catalog entries can exercise a
    real promote+demote swap without needing hundreds of fixture entries."""
    paths.ensure_dirs()
    paths.config_file().write_text(
        "[rotation]\n"
        "target_active = 6\n"
        "min_active = 2\n"
        "hysteresis = 0.0\n"
        "exploration_fraction = 0.0\n"
        "recency_days = 0\n"
        f"min_observed_prompts = {min_observed_prompts}\n",
        encoding="utf-8",
    )


def _prime_rotatable_state(isolated_paths, *, observed: int = 1000) -> None:
    """Catalog + embeddings + sketch + events for a deterministic rotate() run.

    5 active (enabled) entries with low semantic fit and no picks; 5
    candidate (disabled) entries with high semantic fit. Combined with
    `_write_rotation_config`'s target_active=6/hysteresis=0/exploration=0,
    this deterministically promotes all 5 candidates and demotes the 4
    lowest-scoring incumbents, leaving 1 incumbent + 5 promotions = 6 active.
    """
    entries: list[CatalogEntry] = []
    fits: list[float] = []
    for i in range(5):
        entries.append(
            CatalogEntry(
                kind="skill",
                name=f"active{i}",
                namespace="user",
                description=f"active skill {i}",
                path=f"/skills/active{i}/SKILL.md",
                enabled=True,
            )
        )
        fits.append(0.10 + i * 0.01)
    for i in range(5):
        entries.append(
            CatalogEntry(
                kind="skill",
                name=f"cand{i}",
                namespace="user",
                description=f"candidate skill {i}",
                path=f"/skills/cand{i}/SKILL.md",
                enabled=False,
            )
        )
        fits.append(0.95 - i * 0.01)

    embeddings = _emb(fits)
    # F5: `_cmd_rotate`'s freshness check re-scans the real filesystem and
    # compares against the hash recorded alongside catalog.json — it has no
    # way to know `entries` here are synthetic. Hash what a fresh
    # `catalog_mod.scan()` of this (empty) test environment actually
    # returns, not `entries`, so the freshness check sees "unchanged"
    # rather than "stale" for every test in this file.
    source_hash = catalog_mod.compute_hash(catalog_mod.scan())
    index_mod.save(entries, embeddings, source_hash)

    sketch = centroids.empty(k=2)
    v = np.zeros(centroids.DIM, dtype=np.float32)
    v[0] = 1.0
    sketch.vectors[0] = v
    sketch.counts[0] = observed
    sketch.observed = observed
    centroids.save(sketch)

    _write_rotation_config()

    now = datetime.now(timezone.utc)
    lines = [
        # A couple of picks for active4 — its fit (0.14) is still far below
        # the candidates', so this doesn't disturb which 6 entries win.
        json.dumps(
            {
                "schema": 1,
                "kind": "prompt",
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "picks": [
                    {"rank": 1, "name": "active4", "kind": "skill", "score": 0.5}
                ],
            }
        ),
        json.dumps(
            {
                "schema": 1,
                "kind": "prompt",
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "picks": [
                    {"rank": 1, "name": "active4", "kind": "skill", "score": 0.5}
                ],
            }
        ),
        # A stop event so one demoted incumbent (active2) has a real
        # last_invoked_days instead of "never invoked", exercising both
        # branches of the demotion-reason text.
        json.dumps(
            {
                "schema": 1,
                "kind": "stop",
                "ts": (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "skills": ["active2"],
            }
        ),
    ]
    paths.events_file().write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_dry_run_writes_nothing(isolated_paths, capsys):
    """The settings file must be byte-identical after a rotate without --apply."""
    from skill_advisor import cli

    paths.settings_file().write_text(
        '{"skillOverrides": {"a": "off"}}\n', encoding="utf-8"
    )
    before = paths.settings_file().read_bytes()

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns()) == 0

    assert paths.settings_file().read_bytes() == before
    assert "dry run" in capsys.readouterr().out.lower()


def test_apply_writes_the_overrides(isolated_paths, capsys):
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns(apply=True)) == 0

    table = json.loads(paths.settings_file().read_text(encoding="utf-8"))[
        "skillOverrides"
    ]
    assert table  # something changed
    assert table.get("cand0") == "on"
    assert any(v == "off" for k, v in table.items() if k.startswith("active"))


def test_rotate_refuses_and_explains_before_cold_start(isolated_paths, capsys):
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths, observed=3)
    assert cli._cmd_rotate(_ns()) == 1
    assert "prompts" in capsys.readouterr().out.lower()


def test_rotate_refuses_before_settings_write(isolated_paths, capsys):
    """A refusal must also leave the settings file untouched."""
    from skill_advisor import cli

    paths.settings_file().write_text('{"skillOverrides": {}}\n', encoding="utf-8")
    before = paths.settings_file().read_bytes()

    _prime_rotatable_state(isolated_paths, observed=3)
    assert cli._cmd_rotate(_ns(apply=True)) == 1
    assert paths.settings_file().read_bytes() == before


def test_dry_run_shows_score_signal_and_last_invocation(isolated_paths, capsys):
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns()) == 0
    out = capsys.readouterr().out
    assert "semantic_fit" in out
    assert "PROMOTE" in out or "DEMOTE" in out
    assert "never invoked" in out or "last invoked" in out


def test_dry_run_shows_pool_active_target_sketch(isolated_paths, capsys):
    """The pool/active/target/sketch line must print before the proposal so a
    refusal (elsewhere) is self-explanatory."""
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns()) == 0
    out = capsys.readouterr().out
    assert "pool 10" in out
    assert "active 5" in out
    assert "target 6" in out


def test_telemetry_disabled_note(isolated_paths, capsys):
    """When telemetry is off, rotate must plainly (and truthfully) say that
    no NEW usage data is being recorded.

    F10: the old assertions here (`"pick_rate" in out` and `"semantic_fit"
    in out`) were vacuous — those substrings already appear in the ordinary
    PROMOTE/DEMOTE reason text (`semantic_fit=... pick_rate=... total=...`)
    regardless of whether the NOTE prints at all; deleting the NOTE entirely
    still passed. Assert on text distinctive to the NOTE, and prove the
    assertion is not itself vacuous by checking the NOTE's absence once
    telemetry is enabled."""
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    # _write_rotation_config only sets [rotation]; telemetry.events_enabled
    # defaults to False, matching the scenario this test targets.
    assert cli._cmd_rotate(_ns()) == 0
    out = capsys.readouterr().out
    assert "telemetry is off" in out
    assert "no new usage data is being recorded" in out

    cfg_text = paths.config_file().read_text(encoding="utf-8")
    paths.config_file().write_text(
        cfg_text + "\n[telemetry]\nevents_enabled = true\n", encoding="utf-8"
    )
    assert cli._cmd_rotate(_ns()) == 0
    out_enabled = capsys.readouterr().out
    assert "telemetry is off" not in out_enabled


def test_telemetry_off_note_is_true_when_a_historical_log_still_decides(
    isolated_paths, capsys
):
    """F3: `rotate` reads `telemetry.iter_events()` unconditionally — turning
    telemetry off stops the hook from RECORDING new events, it does not
    blind rotate to an event log collected while telemetry was previously
    on. The banner must not claim usage data played no part when it
    demonstrably did: here "incumbent" survives the cut ONLY because of one
    recorded pick (pick_rate=1.0, total=0.46); on semantic_fit alone (0.06)
    it loses decisively to "challenger" (0.30) and would be demoted."""
    from datetime import datetime, timezone

    from skill_advisor import cli

    entries = [
        CatalogEntry(
            kind="skill",
            name="incumbent",
            namespace="user",
            description="d",
            path="/s/incumbent/SKILL.md",
            enabled=True,
        ),
        CatalogEntry(
            kind="skill",
            name="challenger",
            namespace="user",
            description="d",
            path="/s/challenger/SKILL.md",
            enabled=False,
        ),
    ]
    embeddings = _emb([0.10, 0.50])
    source_hash = catalog_mod.compute_hash(catalog_mod.scan())
    index_mod.save(entries, embeddings, source_hash)

    sketch = centroids.empty(k=2)
    v = np.zeros(centroids.DIM, dtype=np.float32)
    v[0] = 1.0
    sketch.vectors[0] = v
    sketch.counts[0] = 1000
    sketch.observed = 1000
    centroids.save(sketch)

    paths.ensure_dirs()
    paths.config_file().write_text(
        "[rotation]\n"
        "target_active = 1\n"
        "min_active = 0\n"
        "hysteresis = 0.0\n"
        "exploration_fraction = 0.0\n"
        "recency_days = 0\n"
        "min_observed_prompts = 200\n",
        encoding="utf-8",
    )
    # telemetry.events_enabled left at its default (False) — the scenario
    # under test — yet a pre-existing event log is present below.

    now = datetime.now(timezone.utc)
    paths.events_file().write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "prompt",
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "picks": [
                    {"rank": 1, "name": "incumbent", "kind": "skill", "score": 0.5}
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert cli._cmd_rotate(_ns()) == 0
    out = capsys.readouterr().out

    assert "no new usage data is being recorded" in out
    assert "unavailable" not in out
    # Proof the historical log actually decided the outcome: on fit alone,
    # "challenger" (0.30) beats "incumbent" (0.06) and a swap would occur.
    assert "no changes proposed" in out


def test_rotate_target_override(isolated_paths, capsys):
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns(target=3)) == 0
    out = capsys.readouterr().out
    assert "target 3" in out


def test_rotate_refuses_on_a_stale_catalog(isolated_paths, capsys):
    """F5: `rotate` never checked catalog freshness the way `doctor` does
    (re-scan the disk, compare against the hash recorded at the last
    `build`). The sharp case is an upgrade: an old catalog.json can load
    with `enabled=True` defaulted for entries the schema change never
    recorded a real value for, so `rotate --apply` before `build` would
    score and write from a fabricated starting state. Simulated here
    directly by corrupting the recorded hash — the same signal `doctor`
    already uses to declare "STALE"."""
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    paths.catalog_hash_file().write_text(
        "deliberately-wrong-hash", encoding="utf-8"
    )
    settings_existed_before = paths.settings_file().is_file()

    assert cli._cmd_rotate(_ns(apply=True)) == 1
    out = capsys.readouterr().out
    assert "stale" in out.lower()
    assert "skill-advisor build" in out

    # A refusal must not write, exactly like the cold-start refusal already
    # guarantees.
    assert paths.settings_file().is_file() == settings_existed_before


def _prime_state_with_unrotatable_entries(
    isolated_paths, *, observed: int = 1000
) -> None:
    """Same shape as `_prime_rotatable_state`, plus a subagent, a slash
    command, and a plugin-namespaced skill mixed into the catalog. None of
    the three has an `overrides.override_key()` — proves the rotation pool
    excludes them entirely rather than merely hiding them at print time.

    The subagent and command get very LOW fit while `enabled=True` — if the
    pool filter were missing, they'd be unmistakable DEMOTE candidates. The
    plugin skill gets very HIGH fit while `enabled=False` — if the filter
    were missing, it would be an unmistakable PROMOTE candidate.
    """
    entries: list[CatalogEntry] = []
    fits: list[float] = []
    for i in range(5):
        entries.append(
            CatalogEntry(
                kind="skill",
                name=f"active{i}",
                namespace="user",
                description=f"active skill {i}",
                path=f"/skills/active{i}/SKILL.md",
                enabled=True,
            )
        )
        fits.append(0.10 + i * 0.01)
    for i in range(5):
        entries.append(
            CatalogEntry(
                kind="skill",
                name=f"cand{i}",
                namespace="user",
                description=f"candidate skill {i}",
                path=f"/skills/cand{i}/SKILL.md",
                enabled=False,
            )
        )
        fits.append(0.95 - i * 0.01)

    entries.append(
        CatalogEntry(
            kind="subagent",
            name="my-subagent",
            namespace="builtin",
            description="a builtin subagent",
            path="",
            enabled=True,
        )
    )
    fits.append(0.01)
    entries.append(
        CatalogEntry(
            kind="command",
            name="/my-command",
            namespace="builtin",
            description="a builtin slash command",
            path="",
            enabled=True,
        )
    )
    fits.append(0.01)
    entries.append(
        CatalogEntry(
            kind="skill",
            name="plugin-skill",
            namespace="plugin:demo",
            description="a plugin-namespaced skill",
            path="/plugins/demo/skills/plugin-skill/SKILL.md",
            enabled=False,
        )
    )
    fits.append(0.99)

    embeddings = _emb(fits)
    # F5: `_cmd_rotate`'s freshness check re-scans the real filesystem and
    # compares against the hash recorded alongside catalog.json — it has no
    # way to know `entries` here are synthetic. Hash what a fresh
    # `catalog_mod.scan()` of this (empty) test environment actually
    # returns, not `entries`, so the freshness check sees "unchanged"
    # rather than "stale" for every test in this file.
    source_hash = catalog_mod.compute_hash(catalog_mod.scan())
    index_mod.save(entries, embeddings, source_hash)

    sketch = centroids.empty(k=2)
    v = np.zeros(centroids.DIM, dtype=np.float32)
    v[0] = 1.0
    sketch.vectors[0] = v
    sketch.counts[0] = observed
    sketch.observed = observed
    centroids.save(sketch)

    _write_rotation_config()


def test_unrotatable_entries_are_excluded_from_the_pool(isolated_paths, capsys):
    """A subagent, a slash command, and a plugin skill sit in the catalog
    alongside user skills. `overrides.override_key()` is None for all three,
    so none is addressable via skillOverrides — none may appear in either
    direction of the proposal, and the exclusion must be visible in the
    printed summary so the pool count reconciles with the catalog size."""
    from skill_advisor import cli

    _prime_state_with_unrotatable_entries(isolated_paths)
    assert cli._cmd_rotate(_ns()) == 0
    out = capsys.readouterr().out

    assert "my-subagent" not in out
    assert "/my-command" not in out
    assert "plugin-skill" not in out
    assert "excluded 3" in out


def _prime_large_pool(isolated_paths, *, n: int = 30, observed: int = 1000) -> None:
    """`n` enabled skills with a small fit spread, zero disabled candidates.
    With target_active well below `n`, this produces `n - target_active`
    demotions and zero promotions — comfortably more than a small --limit,
    to exercise the print cap and its explicit truncation."""
    entries: list[CatalogEntry] = []
    fits: list[float] = []
    for i in range(n):
        entries.append(
            CatalogEntry(
                kind="skill",
                name=f"skill{i:02d}",
                namespace="user",
                description=f"skill {i}",
                path=f"/skills/skill{i:02d}/SKILL.md",
                enabled=True,
            )
        )
        fits.append(0.05 + i * 0.001)

    embeddings = _emb(fits)
    # F5: `_cmd_rotate`'s freshness check re-scans the real filesystem and
    # compares against the hash recorded alongside catalog.json — it has no
    # way to know `entries` here are synthetic. Hash what a fresh
    # `catalog_mod.scan()` of this (empty) test environment actually
    # returns, not `entries`, so the freshness check sees "unchanged"
    # rather than "stale" for every test in this file.
    source_hash = catalog_mod.compute_hash(catalog_mod.scan())
    index_mod.save(entries, embeddings, source_hash)

    sketch = centroids.empty(k=2)
    v = np.zeros(centroids.DIM, dtype=np.float32)
    v[0] = 1.0
    sketch.vectors[0] = v
    sketch.counts[0] = observed
    sketch.observed = observed
    centroids.save(sketch)

    paths.ensure_dirs()
    paths.config_file().write_text(
        "[rotation]\n"
        "target_active = 5\n"
        "min_active = 2\n"
        "hysteresis = 0.0\n"
        "exploration_fraction = 0.0\n"
        "recency_days = 0\n"
        "min_observed_prompts = 200\n",
        encoding="utf-8",
    )


def test_proposal_list_is_capped_with_explicit_truncation(isolated_paths, capsys):
    """30 active skills, target_active=5 → 25 demotions. --limit 5 must still
    print complete summary counts, cap the printed lines at 5, and state the
    truncation explicitly rather than silently cutting the list."""
    from skill_advisor import cli

    _prime_large_pool(isolated_paths, n=30)
    assert cli._cmd_rotate(_ns(limit=5)) == 0
    out = capsys.readouterr().out

    assert "25 demotion(s)" in out
    assert "5 active" in out  # resulting active-set size in the summary
    assert out.count("DEMOTE") == 5  # printed lines capped
    assert "+20 more demotions" in out


def test_rotate_rejects_negative_limit(isolated_paths, capsys):
    from skill_advisor import cli

    _prime_rotatable_state(isolated_paths)
    assert cli._cmd_rotate(_ns(limit=-1)) == 2
    err = capsys.readouterr().err
    assert "--limit" in err


_TOTAL_RE = re.compile(r"total=(-?\d+\.\d+)")


def _parse_totals(out: str) -> dict[str, float]:
    """Map each printed PROMOTE/DEMOTE name to its reported `total=` score."""
    totals: dict[str, float] = {}
    for line in out.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("PROMOTE") or stripped.startswith("DEMOTE")):
            continue
        name = stripped.split(None, 2)[1]
        m = _TOTAL_RE.search(stripped)
        if m:
            totals[name] = float(m.group(1))
    return totals


def _prime_pool_with_picks_and_noisy_excluded_entry(
    isolated_paths, *, include_noisy: bool, observed: int = 1000
) -> None:
    """Same 10-entry pool skeleton as `_prime_rotatable_state` (5 active/low
    fit, 5 candidates/high fit), except `cand0` also gets 2 real picks — so
    its `total` is sensitive to `pick_rate`'s `max_picks` normalisation.

    When `include_noisy` is True, a rotation-ineligible subagent (excluded
    by `_rotatable_pool`, per its `kind`) also sits in the catalog and rack
    up 100 picks in the SAME event log — mirroring the coordinator's report:
    `index.pickable` is `entry.enabled`, and subagents/commands/plugin
    skills are always enabled, so the matcher recommends them routinely and
    they genuinely accrue picks. If `stats` were not scoped to the rotation
    pool before `rotate.score_pool()` sees it, this subagent's 100 picks
    would become `max_picks`, deflating `cand0`'s `pick_rate` from 2/2=1.0
    down to 2/100=0.02 and changing its printed `total` — even though the
    subagent itself never appears in the pool at all.
    """
    entries: list[CatalogEntry] = []
    fits: list[float] = []
    for i in range(5):
        entries.append(
            CatalogEntry(
                kind="skill",
                name=f"active{i}",
                namespace="user",
                description=f"active skill {i}",
                path=f"/skills/active{i}/SKILL.md",
                enabled=True,
            )
        )
        fits.append(0.10 + i * 0.01)
    for i in range(5):
        entries.append(
            CatalogEntry(
                kind="skill",
                name=f"cand{i}",
                namespace="user",
                description=f"candidate skill {i}",
                path=f"/skills/cand{i}/SKILL.md",
                enabled=False,
            )
        )
        fits.append(0.95 - i * 0.01)
    if include_noisy:
        entries.append(
            CatalogEntry(
                kind="subagent",
                name="noisy-subagent",
                namespace="builtin",
                description="a builtin subagent that the matcher recommends a lot",
                path="",
                enabled=True,
            )
        )
        fits.append(0.5)  # irrelevant — excluded from the pool regardless of fit

    embeddings = _emb(fits)
    # F5: `_cmd_rotate`'s freshness check re-scans the real filesystem and
    # compares against the hash recorded alongside catalog.json — it has no
    # way to know `entries` here are synthetic. Hash what a fresh
    # `catalog_mod.scan()` of this (empty) test environment actually
    # returns, not `entries`, so the freshness check sees "unchanged"
    # rather than "stale" for every test in this file.
    source_hash = catalog_mod.compute_hash(catalog_mod.scan())
    index_mod.save(entries, embeddings, source_hash)

    sketch = centroids.empty(k=2)
    v = np.zeros(centroids.DIM, dtype=np.float32)
    v[0] = 1.0
    sketch.vectors[0] = v
    sketch.counts[0] = observed
    sketch.observed = observed
    centroids.save(sketch)

    _write_rotation_config()

    now = datetime.now(timezone.utc)
    lines = [
        json.dumps(
            {
                "schema": 1,
                "kind": "prompt",
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "picks": [{"rank": 1, "name": "cand0", "kind": "skill", "score": 0.5}],
            }
        )
        for _ in range(2)
    ]
    if include_noisy:
        lines.extend(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "prompt",
                    "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "picks": [
                        {
                            "rank": 1,
                            "name": "noisy-subagent",
                            "kind": "subagent",
                            "score": 0.5,
                        }
                    ],
                }
            )
            for _ in range(100)
        )
    paths.events_file().write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_excluded_entry_picks_do_not_distort_pool_scoring(isolated_paths, capsys):
    """A subagent excluded from the rotation pool racks up 100 picks in the
    same telemetry log as a real pool entry (`cand0`, 2 picks). `cand0`'s
    printed `total` must be identical whether or not the noisy excluded
    entry is present — `stats` must be scoped to the pool before scoring,
    not just the catalog/embeddings."""
    from skill_advisor import cli

    _prime_pool_with_picks_and_noisy_excluded_entry(isolated_paths, include_noisy=False)
    assert cli._cmd_rotate(_ns()) == 0
    baseline_totals = _parse_totals(capsys.readouterr().out)

    _prime_pool_with_picks_and_noisy_excluded_entry(isolated_paths, include_noisy=True)
    assert cli._cmd_rotate(_ns()) == 0
    noisy_totals = _parse_totals(capsys.readouterr().out)

    assert "cand0" in baseline_totals
    assert "cand0" in noisy_totals
    assert baseline_totals["cand0"] == noisy_totals["cand0"]
