"""Score the rotation pool and propose swaps. Pure functions over data.

No I/O: callers load the catalog, embeddings, sketch and telemetry stats and
hand them in. That keeps the interesting logic — hysteresis, the exploration
slice, the recency shield, the floor — testable with plain dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import centroids as centroids_mod
from .catalog import CatalogEntry
from .config import RotationConfig

# v1 weights. invocation_rate is computed and reported but weighted zero: the
# telemetry that produces it only started being collected in this same release,
# so it has no history. Promote the weight once weeks of data exist.
W_SEMANTIC = 0.6
W_PICK = 0.4
W_INVOCATION = 0.0


class RotationRefused(Exception):
    """Rotation declined to act. Carries a message fit to print to the user."""


@dataclass(frozen=True)
class Scored:
    entry: CatalogEntry
    semantic_fit: float
    pick_rate: float
    invocation_rate: float
    last_invoked_days: float | None
    total: float


@dataclass
class Proposal:
    promote: list[Scored] = field(default_factory=list)
    demote: list[Scored] = field(default_factory=list)
    reason_by_name: dict[str, str] = field(default_factory=dict)


def score_pool(
    entries: list[CatalogEntry],
    embeddings: np.ndarray,
    sketch: centroids_mod.Sketch,
    stats: dict[str, dict],
    cfg: RotationConfig,
) -> list[Scored]:
    if not entries:
        raise RotationRefused("rotation pool is empty; run `skill-advisor build`")
    fits = centroids_mod.fit(sketch, embeddings, min_observed=cfg.min_observed_prompts)
    if fits is None:
        raise RotationRefused(
            f"only {sketch.observed} prompts observed; rotation needs "
            f"{cfg.min_observed_prompts} before the centroid sketch means anything"
        )

    max_picks = max((int(s.get("picks", 0)) for s in stats.values()), default=0) or 1
    out: list[Scored] = []
    for i, entry in enumerate(entries):
        st = stats.get(entry.name, {})
        picks = int(st.get("picks", 0))
        invocations = int(st.get("invocations", 0))
        pick_rate = picks / max_picks
        invocation_rate = (invocations / picks) if picks else 0.0
        fit = float(fits[i])
        out.append(
            Scored(
                entry=entry,
                semantic_fit=fit,
                pick_rate=pick_rate,
                invocation_rate=invocation_rate,
                last_invoked_days=st.get("last_invoked_days"),
                total=W_SEMANTIC * fit + W_PICK * pick_rate + W_INVOCATION * invocation_rate,
            )
        )
    return out


def _shielded(s: Scored, cfg: RotationConfig) -> bool:
    """Recently invoked ⟹ never demote, whatever the score says."""
    d = s.last_invoked_days
    return d is not None and d <= cfg.recency_days


def propose(scored: list[Scored], cfg: RotationConfig) -> Proposal:
    ranked = sorted(scored, key=lambda s: s.total, reverse=True)
    target = max(int(cfg.target_active), 0)

    explore_slots = int(round(target * cfg.exploration_fraction))
    merit_slots = max(target - explore_slots, 0)

    chosen: list[Scored] = list(ranked[:merit_slots])
    chosen_names = {s.entry.name for s in chosen}

    # Exploration slice: highest semantic_fit among skills with no usage at all.
    # This is the deliberate cost of discovering skills the usage data cannot
    # recommend, and it is what makes this more than a leaderboard. Tracked
    # separately because these must BYPASS hysteresis below — an exploration
    # pick is a reserved slot, not a merit contest it has to win. Subjecting it
    # to the margin test would reject it every time (it loses on merit by
    # construction) and silently delete the whole feature.
    explore_names: set[str] = set()
    if explore_slots:
        unused = [
            s for s in sorted(scored, key=lambda s: s.semantic_fit, reverse=True)
            if s.pick_rate == 0.0 and s.invocation_rate == 0.0
            and s.entry.name not in chosen_names
        ]
        for s in unused[:explore_slots]:
            chosen.append(s)
            chosen_names.add(s.entry.name)
            explore_names.add(s.entry.name)

    incumbents = {s.entry.name for s in scored if s.entry.enabled}
    by_name = {s.entry.name: s for s in scored}
    prop = Proposal()

    # Best incumbent that did NOT make the cut — what a promotion displaces.
    displaced = [by_name[n].total for n in incumbents if n not in chosen_names]
    best_displaced = max(displaced, default=None)

    for name in sorted(chosen_names - incumbents):
        s = by_name[name]
        if name in explore_names:
            prop.promote.append(s)
            prop.reason_by_name[name] = (
                f"exploration slot: semantic_fit={s.semantic_fit:.3f}, no usage history"
            )
            continue
        # Hysteresis: a swap needs a score margin, not a bare ordering
        # difference. Without it, near-tied skills thrash every run.
        if best_displaced is not None and s.total < best_displaced + cfg.hysteresis:
            continue
        prop.promote.append(s)
        prop.reason_by_name[name] = (
            f"semantic_fit={s.semantic_fit:.3f} pick_rate={s.pick_rate:.3f} "
            f"total={s.total:.3f} (>= displaced {best_displaced:.3f} + {cfg.hysteresis:.3f})"
            if best_displaced is not None
            else f"semantic_fit={s.semantic_fit:.3f} total={s.total:.3f} (free slot)"
        )

    # Demote only to make room for a promotion that actually happened, or to
    # come back down to target. Demoting everything outside `chosen` would
    # shrink the active set even when hysteresis blocked every promotion — a
    # swap that never happened must not still cost a skill.
    n_demote = max(0, len(incumbents) + len(prop.promote) - target)
    candidates = sorted(
        (by_name[n] for n in incumbents - chosen_names if not _shielded(by_name[n], cfg)),
        key=lambda s: s.total,
    )
    prop.demote = candidates[:n_demote]

    # Determine which incumbents would rank in the top target by merit alone.
    # This is used to distinguish between demotions due to exploration reserves
    # vs demotions due to low merit score.
    top_target_names = {ranked[i].entry.name for i in range(min(target, len(ranked)))}

    # Identify exploration promotions for use in demotion reasons.
    explore_demotions = [e for e in prop.promote if e.entry.name in explore_names]

    for s in prop.demote:
        last = (
            f"last invoked {s.last_invoked_days:.0f}d ago"
            if s.last_invoked_days is not None
            else "never invoked"
        )

        # If this incumbent ranks in the top target but is demoted, it was
        # displaced by an exploration slot (a reserved discovery slot that
        # takes priority over merit ranking). Distinguish this from the
        # normal "low merit score" case.
        if s.entry.name in top_target_names and explore_demotions:
            lowest_explore = min(explore_demotions, key=lambda e: e.total)
            prop.reason_by_name[s.entry.name] = (
                f"displaced by exploration slot ({lowest_explore.entry.name}: "
                f"semantic_fit={lowest_explore.semantic_fit:.3f}); "
                f"incumbent scores {s.total:.3f}; {last}"
            )
        else:
            # Displaced on merit: incumbent's score is outside the top target.
            prop.reason_by_name[s.entry.name] = (
                f"total={s.total:.3f}, outside the top {target}; {last}"
            )

    # The recency shield can legitimately leave the set above target — a skill
    # used yesterday is never demoted whatever it scores. That overshoot is
    # intended; `min_active` is the only hard bound.
    resulting = len(incumbents) + len(prop.promote) - len(prop.demote)
    if resulting < cfg.min_active:
        raise RotationRefused(
            f"proposal would leave {resulting} skills active, below the floor of "
            f"{cfg.min_active}; refusing"
        )
    return prop
