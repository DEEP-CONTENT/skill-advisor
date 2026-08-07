import numpy as np
import pytest

from skill_advisor import centroids, rotate
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import RotationConfig


def _entry(name, enabled=True):
    return CatalogEntry(kind="skill", name=name, namespace="user", description="d",
                        path=f"/s/{name}/SKILL.md", enabled=enabled)


def _sketch(observed=1000):
    s = centroids.empty(k=2)
    v = np.zeros(centroids.DIM, dtype=np.float32)
    v[0] = 1.0
    s.vectors[0] = v
    s.counts[0] = observed
    s.observed = observed
    return s


def _emb(rows):
    out = np.zeros((len(rows), centroids.DIM), dtype=np.float32)
    for i, x in enumerate(rows):
        out[i, 0] = x
        out[i, 1] = float(np.sqrt(max(0.0, 1.0 - x * x)))
    return out


def test_refuses_before_the_cold_start_floor():
    cold = centroids.empty()
    with pytest.raises(rotate.RotationRefused, match="prompts"):
        rotate.score_pool([_entry("a")], _emb([0.9]), cold, {}, RotationConfig())


def test_a_zero_usage_skill_can_still_score():
    """The feedback trap: a disabled skill is never invoked, so usage data alone
    can only ever confirm the existing selection. semantic_fit is the escape."""
    scored = rotate.score_pool(
        [_entry("never-used", enabled=False)], _emb([0.95]), _sketch(), {}, RotationConfig()
    )
    assert scored[0].semantic_fit > 0.9
    assert scored[0].total > 0.0


def test_hysteresis_prevents_thrash_on_near_ties():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.05,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry("incumbent", enabled=True), _entry("challenger", enabled=False)]
    scored = rotate.score_pool(entries, _emb([0.90, 0.91]), _sketch(), {}, cfg)
    prop = rotate.propose(scored, cfg)
    assert prop.promote == []
    # And critically: a blocked promotion must not still cost the incumbent.
    # Asserting only on `promote` lets a set-shrinking bug through silently.
    assert prop.demote == []


def test_a_clear_winner_does_swap():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.05,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry("incumbent", enabled=True), _entry("challenger", enabled=False)]
    scored = rotate.score_pool(entries, _emb([0.50, 0.99]), _sketch(), {}, cfg)
    prop = rotate.propose(scored, cfg)
    assert [s.entry.name for s in prop.promote] == ["challenger"]
    assert [s.entry.name for s in prop.demote] == ["incumbent"]


def test_a_recently_invoked_skill_is_never_demoted():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.0,
                         exploration_fraction=0.0, recency_days=30)
    entries = [_entry("used-yesterday", enabled=True), _entry("great-fit", enabled=False)]
    stats = {"used-yesterday": {"picks": 0, "invocations": 1, "last_invoked_days": 1.0}}
    scored = rotate.score_pool(entries, _emb([0.10, 0.99]), _sketch(), stats, cfg)
    assert rotate.propose(scored, cfg).demote == []


def test_the_exploration_slice_promotes_a_zero_usage_skill():
    cfg = RotationConfig(target_active=10, min_active=0, hysteresis=0.0,
                         exploration_fraction=0.20, recency_days=0)
    entries = [_entry(f"used{i}", enabled=True) for i in range(9)]
    entries.append(_entry("unused-but-fits", enabled=False))
    stats = {f"used{i}": {"picks": 50, "invocations": 5, "last_invoked_days": None} for i in range(9)}
    scored = rotate.score_pool(entries, _emb([0.4] * 9 + [0.92]), _sketch(), stats, cfg)
    prop = rotate.propose(scored, cfg)
    assert "unused-but-fits" in [s.entry.name for s in prop.promote]


def test_refuses_to_breach_the_active_set_floor():
    cfg = RotationConfig(target_active=1, min_active=5, hysteresis=0.0,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry(f"s{i}", enabled=True) for i in range(3)]
    scored = rotate.score_pool(entries, _emb([0.9, 0.8, 0.7]), _sketch(), {}, cfg)
    with pytest.raises(rotate.RotationRefused, match="floor"):
        rotate.propose(scored, cfg)


def test_every_proposal_carries_a_reason():
    cfg = RotationConfig(target_active=1, min_active=0, hysteresis=0.05,
                         exploration_fraction=0.0, recency_days=0)
    entries = [_entry("incumbent", enabled=True), _entry("challenger", enabled=False)]
    scored = rotate.score_pool(entries, _emb([0.50, 0.99]), _sketch(), {}, cfg)
    prop = rotate.propose(scored, cfg)
    for s in prop.promote + prop.demote:
        assert prop.reason_by_name[s.entry.name]


def test_negative_exploration_fraction_is_clamped_not_runaway():
    """F8: `exploration_fraction` had no bounds validation.
    `round(target * exploration_fraction)` goes negative for an
    out-of-range value (e.g. a hand-edited config.toml with
    exploration_fraction=-0.1), and `unused[:explore_slots]` then reads as
    a NEGATIVE-index slice — "all but the last N" — which for a large pool
    of zero-usage skills promotes nearly all of them. `min_active` is a
    floor on the RESULT, not on this arithmetic, so it cannot catch it.
    Measured (unclamped): target_active=75, exploration_fraction=-0.1 ->
    explore_slots=-8 -> 222 promotions, ending at 292 active against a
    target of 75."""
    cfg = RotationConfig(
        target_active=10,
        min_active=0,
        hysteresis=0.0,
        exploration_fraction=-0.1,
        recency_days=0,
    )
    incumbents = [_entry(f"inc{i}", enabled=True) for i in range(3)]
    candidates = [_entry(f"cand{i:02d}", enabled=False) for i in range(20)]
    entries = incumbents + candidates
    fits = [0.50, 0.49, 0.48] + [0.40 - i * 0.01 for i in range(20)]

    scored = rotate.score_pool(entries, _emb(fits), _sketch(), {}, cfg)
    prop = rotate.propose(scored, cfg)

    # Clamped to exploration_fraction=0.0 -> explore_slots=0, merit_slots=10
    # -> exactly the top-10-by-fit chosen: 3 incumbents + the 7 highest-fit
    # candidates. Without the clamp this promotes 19 of the 20 candidates.
    assert len(prop.promote) == 7
    assert len(prop.demote) == 0
    resulting = len(incumbents) + len(prop.promote) - len(prop.demote)
    assert resulting == 10


def test_exploration_driven_demotion_reason_distinguishes_cause():
    """When exploration picks cause a demotion of an incumbent ranking in the
    top N by merit, the reason must not falsely claim it is 'outside the top N'."""
    cfg = RotationConfig(
        target_active=10, min_active=0, hysteresis=0.0,
        exploration_fraction=0.20,  # 2 exploration slots, 8 merit slots
        recency_days=0
    )

    # 11 incumbents, 1 exploration pick
    # All incumbents have equal semantic fit (0.5)
    # Exploration pick has higher semantic fit (0.95)
    entries = [_entry(f"inc{i:02d}", enabled=True) for i in range(11)]
    entries.append(_entry("exp-pick", enabled=False))

    stats = {f"inc{i:02d}": {"picks": 10, "invocations": 1, "last_invoked_days": None} for i in range(11)}
    # All incumbents score 0.5 semantic fit, exploration pick 0.95
    emb = _emb([0.5] * 11 + [0.95])

    scored = rotate.score_pool(entries, emb, _sketch(), stats, cfg)
    prop = rotate.propose(scored, cfg)

    # Verify preconditions: exploration pick promoted
    assert "exp-pick" in [s.entry.name for s in prop.promote]
    assert len(prop.demote) > 0

    # Get rank of each skill (0-indexed)
    sorted_by_total = sorted(scored, key=lambda s: s.total, reverse=True)
    rank_by_name = {s.entry.name: i for i, s in enumerate(sorted_by_total)}

    # Check demoted incumbents
    for demoted in prop.demote:
        rank = rank_by_name[demoted.entry.name]
        reason = prop.reason_by_name[demoted.entry.name]

        # If this incumbent ranks in the top 10 (rank < 10, 0-indexed)
        if rank < 10:
            # Then the reason must NOT claim it's "outside the top 10"
            assert "outside the top 10" not in reason, (
                f"{demoted.entry.name} ranks #{rank + 1} of 12 but falsely claims "
                f"'outside the top 10' in reason: {reason}"
            )
            # And it SHOULD mention that it's displaced by exploration
            assert "exploration" in reason.lower(), (
                f"{demoted.entry.name} ranks #{rank + 1} (top 10) but doesn't mention "
                f"exploration in reason: {reason}"
            )


def test_demotion_reason_true_when_explore_slot_claimed_by_an_incumbent():
    """F9: the same defect class as above, one layer deeper. The old code
    derived its explore-attribution from `prop.promote` alone
    (`explore_demotions = [e for e in prop.promote if e.entry.name in
    explore_names]`), which is EMPTY whenever the exploration slot happens
    to land on a skill that is ALREADY enabled — filling the slot adds it
    to `chosen` without any promotion ever occurring. Here "X" wins the
    single exploration slot (highest fit among zero-usage entries) but is
    already an incumbent, so `prop.promote` stays empty even though "Y" — a
    true top-10-by-total incumbent — gets bumped out of `chosen` by that
    same slot and demoted. The old code's `... and explore_demotions` guard
    was falsy, so it fell through to the merit branch and printed a false
    "outside the top 10" for an incumbent that, by construction, ranks IN
    the top 10."""
    cfg = RotationConfig(
        target_active=10,
        min_active=0,
        hysteresis=0.0,
        exploration_fraction=0.1,  # 1 exploration slot, 9 merit slots
        recency_days=0,
    )
    merit = [_entry(f"merit{i}", enabled=True) for i in range(9)]
    # Y: incumbent with a little real usage — ranks #10 by total, just
    # below the 9 merit entries, so it belongs in target_active=10 by
    # merit alone.
    y = _entry("Y", enabled=True)
    # X: incumbent, ZERO usage — the only "unused" entry left once the
    # merit slots are excluded, so it wins the exploration slot even
    # though it is already active and no promotion results.
    x = _entry("X", enabled=True)
    # bigpicker: disabled, real usage — exists only to set max_picks so
    # Y's pick_rate is a small fraction rather than automatically 1.0.
    bigpicker = _entry("bigpicker", enabled=False)

    entries = merit + [y, x, bigpicker]
    fits = [0.90 - i * 0.01 for i in range(9)] + [0.65, 0.60, 0.01]
    stats = {
        "Y": {"picks": 1, "invocations": 0, "last_invoked_days": None},
        "bigpicker": {"picks": 10, "invocations": 0, "last_invoked_days": None},
    }

    scored = rotate.score_pool(entries, _emb(fits), _sketch(), stats, cfg)
    prop = rotate.propose(scored, cfg)

    # Preconditions matching the scenario's whole point: the explore slot
    # is claimed by an already-active skill, so no promotion happens at
    # all, yet Y — ranking #10 of 12 by total — still gets demoted.
    assert prop.promote == []
    assert [s.entry.name for s in prop.demote] == ["Y"]

    reason = prop.reason_by_name["Y"]
    assert "outside the top 10" not in reason, reason
    assert "displaced by exploration slot" in reason
    assert "X" in reason
