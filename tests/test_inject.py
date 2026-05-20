from skill_advisor import inject, lifecycle
from skill_advisor.catalog import CatalogEntry
from skill_advisor.matcher import PickResult, ResolvedPick


def _pick(name: str, kind: str = "skill", reason: str = "") -> ResolvedPick:
    return ResolvedPick(
        entry=CatalogEntry(kind=kind, name=name, namespace="user", description="..."),
        reason=reason,
    )


def test_format_renders_numbered_list_without_lifecycle():
    result = PickResult(
        picks=[
            _pick("brainstorming", "skill", "vague idea"),
            _pick("Plan", "subagent", "design first"),
        ],
        state=None,
    )
    output = inject.format(result)
    assert output.startswith("<skill-advisor>")
    assert output.endswith("</skill-advisor>")
    assert "1. brainstorming (skill) — vague idea" in output
    assert "2. Plan (subagent) — design first" in output
    # No lifecycle banner when state is None.
    assert "Lifecycle:" not in output


def test_format_tolerates_empty_reason():
    result = PickResult(picks=[_pick("brainstorming", reason="")], state=None)
    output = inject.format(result)
    assert "matches the prompt" in output


def test_format_includes_lifecycle_banner_when_state_active():
    state = lifecycle.LifecycleState(
        session_id="s1",
        original_prompt="build a rate limiter for the api",
        phase=lifecycle.REVIEW,
        cycles={"correction": 1},
    )
    result = PickResult(picks=[_pick("code-reviewer", "skill", "review phase")], state=state)
    output = inject.format(result)
    assert "Lifecycle:" in output
    assert "[review]" in output
    assert "Original task: build a rate limiter" in output
    assert "correction cycle 1" in output
    assert "correction (if review flags issues)" in output


def test_format_complete_phase_mentions_closing_the_cycle():
    state = lifecycle.LifecycleState(
        session_id="s1",
        original_prompt="build a rate limiter",
        phase=lifecycle.COMPLETE,
    )
    result = PickResult(picks=[_pick("commit", "skill", "complete")], state=state)
    output = inject.format(result)
    assert "This lifecycle is complete" in output


def test_inject_banner_includes_parallelization_check_phase():
    from skill_advisor import catalog, inject, lifecycle, matcher
    state = lifecycle.LifecycleState(
        session_id="s", original_prompt="build X",
        phase=lifecycle.PARALLELIZATION_CHECK,
        pending_todos=["A", "B", "C"],
    )
    entry = catalog.CatalogEntry(
        kind="skill",
        name="superpowers:dispatching-parallel-agents",
        namespace="plugin:superpowers",
        description="Parallel subagents.",
    )
    result = matcher.PickResult(
        picks=[matcher.ResolvedPick(entry=entry, reason="parallelization: disjoint files")],
        state=state,
    )
    rendered = inject.format(result)
    # Phase chain must include the new phase and highlight the current one.
    assert "parallelization_check" in rendered
    assert "[parallelization_check]" in rendered
    # The pick reason prefix is surfaced so the model sees *why* this picks fired.
    assert "parallelization: disjoint files" in rendered
    # Next-phase hint points at implementation.
    assert "implementation" in rendered.lower()
    # Nudge instructs the model to use isolated worktrees for parallel work.
    assert "isolated worktrees" in rendered


def test_matcher_to_inject_pipeline_renders_parallelization_nudge(monkeypatch):
    """Reviewer I1: end-to-end matcher.pick() → inject.format() must surface
    the parallelization_check banner + nudge when the detector says parallel=True."""
    from skill_advisor import catalog, index, inject, lifecycle, matcher, parallelization
    from skill_advisor.config import Config, ParallelizationConfig

    entries = [
        catalog.CatalogEntry(
            kind="skill",
            name="superpowers:dispatching-parallel-agents",
            namespace="plugin:superpowers",
            description="Dispatch parallel subagents.",
        ),
        catalog.CatalogEntry(
            kind="skill",
            name="superpowers:using-git-worktrees",
            namespace="plugin:superpowers",
            description="Set up isolated worktrees.",
        ),
    ]
    fake_index = index.Index(catalog=entries, embeddings=None)  # type: ignore[arg-type]
    monkeypatch.setattr("skill_advisor.matcher._load_index", lambda: fake_index)
    monkeypatch.setattr(
        "skill_advisor.matcher.parallelization.detect",
        lambda *_a, **_k: parallelization.ParallelizationResult(
            parallel=True, groups=[[0, 2], [1]], reason="disjoint files"
        ),
    )

    cfg = Config(parallelization=ParallelizationConfig(enabled=True, min_tasks=3))
    state = lifecycle.start("sess-i1", "build feature")
    lifecycle.enter_parallelization_check(state, ["A", "B", "C"])

    result = matcher.pick("go", cfg, session_id="sess-i1")
    assert result is not None
    rendered = inject.format(result)
    # Banner must show the parallelization_check phase highlighted.
    assert "[parallelization_check]" in rendered
    # The nudge text from inject.py must appear (not just the phase chain).
    assert "isolated worktrees" in rendered
    # On-disk state still advanced to IMPLEMENTATION for the next turn.
    reloaded = lifecycle.load("sess-i1")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.IMPLEMENTATION
