"""Orchestrates triage → (lifecycle preference | embedding prefilter → judge) → final picks."""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

from . import effort as effort_mod
from . import index as index_mod
from . import judge, lifecycle, parallelization, triage
from .catalog import CatalogEntry
from .config import Config, load as load_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedPick:
    entry: CatalogEntry
    reason: str


@dataclass(frozen=True)
class PickResult:
    picks: list[ResolvedPick]
    state: lifecycle.LifecycleState | None  # None when not in an active lifecycle
    judge_effort: str | None = None          # raw verdict from the judge, if it ran
    effort: "effort_mod.EffortRecommendation | None" = None  # resolved recommendation


@dataclass(frozen=True)
class StatelessResult:
    picks: list[ResolvedPick]
    judge_effort: str | None = None
    # True ⟺ the judge subprocess ran and returned a verdict this call.
    judge_ran: bool = False
    # Non-None ⟺ the judge was asked but produced nothing usable; see judge.FAILURE_*.
    judge_failure: str | None = None


@dataclass
class JudgeTrace:
    """Mutable out-parameter recording what the judge actually did this call.

    Deliberately not folded into the return value: `_pick_inner` returns None
    whenever there are no picks, and the most important case to count — the
    judge running and declining — produces exactly that. A collector survives
    the None.
    """
    ran: bool = False
    failure: str | None = None


def _embedding_picks(
    ranked: list[tuple[CatalogEntry, float]],
    k_picks: int,
    min_score: float,
    *,
    fallback: bool = False,
) -> list[ResolvedPick]:
    """Turn a cosine ranking into picks, stopping at the score floor.

    `ranked` is already sorted descending, so the first sub-threshold entry
    ends the list. `fallback` only changes the reason string, so the event log
    can tell a confident embedding pick from a judge-failure rescue.
    """
    label = "embedding fallback" if fallback else "embedding match"
    out: list[ResolvedPick] = []
    for entry, score in ranked[:k_picks]:
        if score < min_score:
            break
        out.append(ResolvedPick(entry=entry, reason=f"{label} ({score:.2f})"))
    return out


def pick_stateless(
    prompt: str,
    cfg: Config,
    *,
    force_judge: bool | None = None,
    threshold: float | None = None,
    top_k: int | None = None,
    candidates: int | None = None,
    index: index_mod.Index | None = None,
    trace: "JudgeTrace | None" = None,
) -> StatelessResult:
    """Match a prompt against the catalog without consulting lifecycle state.

    Shared between `matcher.pick()` (hot path) and `skill-advisor match` (CLI).
    Overrides default to config values when None; `index` is injectable for tests.

    `trace`, if given, is populated with what the judge actually did this call
    — see `JudgeTrace` for why this can't just be read off the return value.
    """
    idx = index if index is not None else _load_index()
    if idx is None:
        return StatelessResult(picks=[])

    use_judge = cfg.matcher.use_judge if force_judge is None else force_judge
    k_picks = cfg.matcher.max_picks if top_k is None else top_k
    k_candidates = cfg.matcher.max_candidates if candidates is None else candidates
    # A pick must come from the shortlist; never request more picks than candidates.
    k_picks = min(k_picks, k_candidates)
    min_score = cfg.matcher.min_embedding_score if threshold is None else threshold

    ranked = index_mod.top_k(prompt, idx, k_candidates)

    if use_judge:
        cand_entries = [e for e, _ in ranked]
        if not cand_entries:
            return StatelessResult(picks=[])
        raw = judge.rank(prompt, cand_entries, cfg)
        if raw.failure is not None:
            # The judge produced no verdict. The embedding ranking is already in
            # hand and cost nothing extra — emitting it beats going silent.
            log.info("judge unavailable (%s); using embedding fallback", raw.failure)
            result = StatelessResult(
                picks=_embedding_picks(ranked, k_picks, min_score, fallback=True),
                judge_ran=False,
                judge_failure=raw.failure,
            )
        else:
            by_name = {e.name: e for e in cand_entries}
            out: list[ResolvedPick] = []
            for p in raw.picks[:k_picks]:
                entry = by_name.get(p.name)
                if entry is not None:
                    out.append(ResolvedPick(entry=entry, reason=p.reason))
            # An empty `out` here is the judge declining. Respect it — do not fall back.
            result = StatelessResult(picks=out, judge_effort=raw.effort, judge_ran=True)
    else:
        result = StatelessResult(picks=_embedding_picks(ranked, k_picks, min_score))

    if trace is not None:
        trace.ran = result.judge_ran
        trace.failure = result.judge_failure
    return result


def _parallelization_picks(
    state: lifecycle.LifecycleState,
    cfg: Config,
    idx: index_mod.Index,
) -> list[ResolvedPick]:
    """Run the detector and return parallel-exec picks if parallel=True, else []."""
    todos = list(state.pending_todos or [])
    if not todos:
        return []
    try:
        verdict = parallelization.detect(todos, cfg)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("parallelization.detect raised: %s", exc)
        return []
    if verdict is None or not verdict.parallel:
        return []
    phase_picks = lifecycle.pick_candidates_for_phase(
        lifecycle.PARALLELIZATION_CHECK,
        idx.catalog,
        limit=cfg.matcher.max_picks,
        config=cfg.lifecycle,
    )
    reason = verdict.reason or "parallel execution recommended"
    return [
        ResolvedPick(entry=e, reason=f"parallelization: {reason}")
        for e in phase_picks
    ]


def _phase_picks(phase: str, catalog: list[CatalogEntry], cfg: Config) -> list[ResolvedPick]:
    entries = lifecycle.pick_candidates_for_phase(
        phase, catalog, limit=cfg.matcher.max_picks, config=cfg.lifecycle
    )
    return [
        ResolvedPick(entry=e, reason=f"{phase} phase preference")
        for e in entries
    ]


def _default_picks(
    prompt: str, cfg: Config, idx: index_mod.Index, trace: "JudgeTrace | None" = None
) -> "StatelessResult":
    return pick_stateless(prompt, cfg, index=idx, trace=trace)


def _load_index() -> index_mod.Index | None:
    try:
        return index_mod.load()
    except FileNotFoundError:
        log.warning("catalog not built; run `skill-advisor build`")
    except ValueError as exc:
        log.warning("catalog load failed: %s", exc)
    return None


def _pick_inner(
    prompt: str,
    cfg: Config,
    session_id: str | None,
    trace: "JudgeTrace | None" = None,
) -> PickResult | None:
    """Return picks for a prompt. None means the advisor should stay silent.

    When `session_id` is provided, a persistent lifecycle state machine is
    consulted: planning → implementation → review → (correction)* → complete.
    """
    # Honour the master toggle: skip lifecycle bookkeeping entirely when disabled.
    state = lifecycle.load(session_id) if (session_id and cfg.lifecycle.enabled) else None
    in_lifecycle = state is not None and state.is_active()

    # Triage is bypassed during an active lifecycle — short prompts like "go",
    # "cancel", "looks good" are the lifecycle's signal vocabulary.
    if not in_lifecycle and triage.should_skip(prompt, cfg):
        return None

    idx = _load_index()
    if idx is None:
        return None

    text = lifecycle.strip_markers(prompt)

    # -------- Active lifecycle: handle signals first --------
    if state is not None and state.is_active():
        if state.phase == lifecycle.PARALLELIZATION_CHECK:
            parallel_picks = _parallelization_picks(state, cfg, idx)
            # Persist auto-advance to disk for the next turn (one-shot phase).
            state.pending_todos = []
            lifecycle.advance(
                state,
                note="auto: parallelization check complete",
                source="auto",
                config=cfg.lifecycle,
            )
            if parallel_picks:
                # Verdict was YES — show the parallelization_check banner so the
                # nudge in inject.py is reached. State on disk is already IMPLEMENTATION
                # for the next turn.
                display_state = dataclasses.replace(state, phase=lifecycle.PARALLELIZATION_CHECK)
                return PickResult(picks=parallel_picks, state=display_state)
            # Verdict was NO/None — already advanced; show implementation picks.
            impl_picks = _phase_picks(state.phase, idx.catalog, cfg)
            return PickResult(picks=impl_picks, state=state) if impl_picks else None

        if lifecycle.is_cancel_signal(text):
            lifecycle.cancel(state, note="user cancelled")
            # Route the actual prompt through the normal matcher too.
            sr = _default_picks(text, cfg, idx, trace)
            return PickResult(picks=sr.picks, state=None, judge_effort=sr.judge_effort) if sr.picks else None

        if lifecycle.is_complete_signal(text) and state.phase in {lifecycle.REVIEW, lifecycle.CORRECTION}:
            lifecycle.force_complete(state, note="user signalled complete")
            complete_picks = _phase_picks(lifecycle.COMPLETE, idx.catalog, cfg)
            judge_effort = None
            if not complete_picks:
                sr = _default_picks(text, cfg, idx, trace)
                complete_picks = sr.picks
                judge_effort = sr.judge_effort
            return PickResult(picks=complete_picks, state=state, judge_effort=judge_effort)

        if lifecycle.is_continue_signal(text):
            had_issues = False
            if state.phase == lifecycle.REVIEW and lifecycle.mentions_issues(text):
                had_issues = True
            lifecycle.advance(
                state, had_issues=had_issues, note=f"continuation: '{text[:40]}'", config=cfg.lifecycle
            )
            picks = _phase_picks(state.phase, idx.catalog, cfg)
            judge_effort = None
            if not picks:
                sr = _default_picks(state.original_prompt, cfg, idx, trace)
                picks = sr.picks
                judge_effort = sr.judge_effort
            return PickResult(picks=picks, state=state, judge_effort=judge_effort) if picks else None

        # User typed a substantive prompt that's not a continuation signal →
        # treat as off-topic; silently cancel the lifecycle and route normally.
        if len(text.split()) >= 4:
            lifecycle.cancel(state, note="off-topic follow-up")
            sr = _default_picks(text, cfg, idx, trace)
            return PickResult(picks=sr.picks, state=None, judge_effort=sr.judge_effort) if sr.picks else None

        # Short prompt, no signal matched — stay silent, don't advance.
        return None

    # -------- No active lifecycle: maybe start one --------
    # Note: pass the raw prompt so the [no-lifecycle] marker is honoured.
    if (
        session_id
        and cfg.lifecycle.enabled
        and lifecycle.is_trigger(prompt, cfg.lifecycle)
    ):
        new_state = lifecycle.start(session_id, text)
        picks = _phase_picks(new_state.phase, idx.catalog, cfg)
        judge_effort = None
        if not picks:
            sr = _default_picks(new_state.original_prompt, cfg, idx, trace)
            picks = sr.picks
            judge_effort = sr.judge_effort
        return PickResult(picks=picks, state=new_state, judge_effort=judge_effort) if picks else None

    # -------- Default: stateless matcher --------
    sr = _default_picks(text, cfg, idx, trace)
    return PickResult(picks=sr.picks, state=None, judge_effort=sr.judge_effort) if sr.picks else None


def pick(
    prompt: str,
    config: Config | None = None,
    session_id: str | None = None,
    *,
    trace: JudgeTrace | None = None,
) -> PickResult | None:
    """Return picks for a prompt, with an effort recommendation attached."""
    cfg = config or load_config()
    result = _pick_inner(prompt, cfg, session_id, trace)
    if result is None or not cfg.effort.enabled:
        return result

    phase = result.state.phase if result.state else None
    parallel = any(p.reason.startswith("parallelization:") for p in result.picks)
    rec = effort_mod.classify(
        phase=phase,
        judge_effort=result.judge_effort,
        parallel=parallel,
        cfg=cfg,
        prompt=prompt,
    )
    return dataclasses.replace(result, effort=rec)
