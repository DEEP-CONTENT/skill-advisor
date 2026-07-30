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
