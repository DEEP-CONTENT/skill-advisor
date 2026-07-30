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
