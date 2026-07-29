"""Integration tests for matcher.pick with lifecycle wiring."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import index as index_mod
from skill_advisor import lifecycle, matcher
from skill_advisor.catalog import CatalogEntry


def _toy_catalog() -> list[CatalogEntry]:
    return [
        CatalogEntry(kind="subagent", name="Plan", namespace="builtin", description="design impl plan"),
        CatalogEntry(kind="subagent", name="feature-dev:code-reviewer", namespace="builtin", description="review code"),
        CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="brainstorm"),
        CatalogEntry(kind="skill", name="fix-review", namespace="user", description="fix review findings"),
        CatalogEntry(kind="skill", name="commit", namespace="user", description="commit changes"),
        CatalogEntry(kind="skill", name="auth-implementation-patterns", namespace="user", description="auth patterns"),
    ]


def _prime_index(isolated_paths):
    entries = _toy_catalog()
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((len(entries), 8)).astype(np.float32)
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)

    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "hash")

    # Stub embedder so top_k doesn't try to load fastembed.
    class _Stub:
        def embed(self, texts):
            for _ in texts:
                yield rng.standard_normal(8).astype(np.float32)

    return _Stub()


def test_pick_starts_lifecycle_on_trigger_prompt(isolated_paths):
    stub = _prime_index(isolated_paths)
    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick("build a rate limiter for the api", session_id="s1")

    assert result is not None
    assert result.state is not None
    assert result.state.phase == lifecycle.PLANNING
    assert any(p.entry.name == "Plan" for p in result.picks)


def test_pick_no_lifecycle_marker_disables_trigger(isolated_paths):
    stub = _prime_index(isolated_paths)
    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick(
            "[no-lifecycle] build a rate limiter for the api", session_id="s1"
        )
    # Lifecycle not started.
    assert result is None or result.state is None
    assert lifecycle.load("s1") is None


def test_pick_advances_on_continue_signal(isolated_paths):
    stub = _prime_index(isolated_paths)
    lifecycle.start("s1", "build a rate limiter for the api")  # planning

    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick("go", session_id="s1")

    assert result is not None
    assert result.state.phase == lifecycle.IMPLEMENTATION


def test_pick_review_with_issues_advances_to_correction(isolated_paths):
    stub = _prime_index(isolated_paths)
    state = lifecycle.start("s1", "build a rate limiter")
    lifecycle.advance(state)  # implementation
    lifecycle.advance(state)  # review

    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick("continue — fix these issues", session_id="s1")

    assert result is not None
    assert result.state.phase == lifecycle.CORRECTION
    assert any(p.entry.name == "fix-review" for p in result.picks)


def test_pick_complete_signal_moves_to_complete(isolated_paths):
    stub = _prime_index(isolated_paths)
    state = lifecycle.start("s1", "build a rate limiter")
    lifecycle.advance(state)  # implementation
    lifecycle.advance(state)  # review

    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick("looks good", session_id="s1")

    assert result is not None
    assert result.state.phase == lifecycle.COMPLETE
    assert any(p.entry.name == "commit" for p in result.picks)


def test_pick_cancel_signal_cancels_lifecycle(isolated_paths):
    stub = _prime_index(isolated_paths)
    lifecycle.start("s1", "build a rate limiter")

    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick("cancel this task", session_id="s1")

    state = lifecycle.load("s1")
    assert state.phase == lifecycle.CANCELLED
    # Follow-up still routes through normal matcher if picks exist.
    assert result is None or result.state is None


def test_pick_off_topic_prompt_cancels_lifecycle(isolated_paths):
    stub = _prime_index(isolated_paths)
    lifecycle.start("s1", "build a rate limiter")

    with patch.object(index_mod, "_embed_model", return_value=stub):
        matcher.pick("write unit tests for the password reset flow", session_id="s1")

    state = lifecycle.load("s1")
    assert state.phase == lifecycle.CANCELLED


def test_pick_without_session_id_never_starts_lifecycle(isolated_paths):
    stub = _prime_index(isolated_paths)
    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick("build a rate limiter for the api")
    # No session → no state, falls through to default matcher.
    assert result is None or result.state is None


# ---------------------------------------------------------------------------
# matcher.pick_stateless — shared helper used by `pick()` and `skill-advisor match`
# ---------------------------------------------------------------------------


def _stateless_catalog() -> list[CatalogEntry]:
    # Distinct entries with controllable embedding positions so threshold logic is testable.
    return [
        CatalogEntry(kind="skill", name="alpha", namespace="user", description="first entry"),
        CatalogEntry(kind="skill", name="beta", namespace="user", description="second entry"),
        CatalogEntry(kind="skill", name="gamma", namespace="user", description="third entry"),
    ]


def _prime_stateless_index(scores: list[float]):
    """Save a catalog + embeddings that will produce the given cosine scores for the query vector [1,0,0]."""
    import numpy as np

    entries = _stateless_catalog()
    assert len(scores) == len(entries)

    # Query vector will be [1, 0, 0]; to get target cosine S, entry vector = [S, sqrt(1-S^2), 0].
    embeddings = np.zeros((len(entries), 3), dtype=np.float32)
    for i, s in enumerate(scores):
        s_clamped = max(-1.0, min(1.0, s))
        embeddings[i, 0] = s_clamped
        embeddings[i, 1] = float(np.sqrt(max(0.0, 1.0 - s_clamped * s_clamped)))

    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "hash")

    class _FixedEmbed:
        def embed(self, texts):
            q = np.zeros(3, dtype=np.float32)
            q[0] = 1.0
            for _ in texts:
                yield q

    return _FixedEmbed()


def test_pick_stateless_returns_top_k_in_score_order(isolated_paths):
    from skill_advisor.config import Config

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        picks = matcher.pick_stateless("q", Config(), top_k=3, candidates=3, threshold=0.0).picks

    assert [p.entry.name for p in picks] == ["alpha", "beta", "gamma"]
    # Reason contains the score for embedding picks.
    assert "0.90" in picks[0].reason


def test_pick_stateless_threshold_filters(isolated_paths):
    from skill_advisor.config import Config

    stub = _prime_stateless_index([0.9, 0.5, 0.2])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        picks = matcher.pick_stateless("q", Config(), top_k=3, candidates=3, threshold=0.6).picks

    assert [p.entry.name for p in picks] == ["alpha"]


def test_pick_stateless_top_k_cannot_exceed_candidates(isolated_paths):
    from skill_advisor.config import Config

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    with patch.object(index_mod, "_embed_model", return_value=stub):
        picks = matcher.pick_stateless("q", Config(), top_k=100, candidates=2, threshold=0.0).picks

    assert len(picks) == 2


def test_pick_stateless_force_judge_true_calls_judge(isolated_paths):
    from skill_advisor.config import Config
    from skill_advisor.judge import JudgeResult
    from skill_advisor.judge import Pick as JudgePick

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        mock_rank.return_value = JudgeResult(
            picks=[JudgePick(name="beta", reason="because reasons")]
        )
        result = matcher.pick_stateless(
            "q", Config(), force_judge=True, top_k=3, candidates=3, threshold=0.0,
        )

    mock_rank.assert_called_once()
    picks = result.picks
    assert [p.entry.name for p in picks] == ["beta"]
    assert picks[0].reason == "because reasons"


def test_pick_stateless_force_judge_false_overrides_config(isolated_paths):
    from skill_advisor.config import Config, MatcherConfig

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    cfg_with_judge = Config(matcher=MatcherConfig(use_judge=True))
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        picks = matcher.pick_stateless(
            "q", cfg_with_judge, force_judge=False, top_k=3, candidates=3, threshold=0.0,
        ).picks

    mock_rank.assert_not_called()
    assert [p.entry.name for p in picks] == ["alpha", "beta", "gamma"]


def test_pick_stateless_no_index_returns_empty(isolated_paths):
    from skill_advisor.config import Config

    # No catalog.json / embeddings.npz written.
    result = matcher.pick_stateless("q", Config(), top_k=3, candidates=3, threshold=0.0)
    assert result.picks == []


def test_pick_stateless_parity_with_pick_on_nonlifecycle(isolated_paths):
    """Property: for a prompt that doesn't trigger lifecycle, stateless and pick()
    return the same list of (name, reason) tuples."""
    from skill_advisor.config import Config

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    cfg = Config()
    # Prompt is long enough + tech-enough to clear triage, but session_id=None so
    # the lifecycle path is never taken even though "refactor" is a trigger verb.
    prompt = "review the authentication middleware for subtle security bugs"
    with patch.object(index_mod, "_embed_model", return_value=stub):
        stateless = matcher.pick_stateless(prompt, cfg, threshold=0.0).picks
        full = matcher.pick(prompt, cfg, session_id=None)

    assert full is not None
    assert [(p.entry.name, p.reason) for p in stateless] == [(p.entry.name, p.reason) for p in full.picks]


def test_matcher_parallel_yes_routes_to_parallel_skills(monkeypatch, tmp_path):
    from skill_advisor import catalog, index, lifecycle, matcher, parallelization
    from skill_advisor.config import Config, ParallelizationConfig

    entries = [
        catalog.CatalogEntry(
            kind="skill",
            name="superpowers:dispatching-parallel-agents",
            namespace="plugin:superpowers",
            description="Dispatch parallel subagents for independent tasks.",
        ),
        catalog.CatalogEntry(
            kind="skill",
            name="superpowers:using-git-worktrees",
            namespace="plugin:superpowers",
            description="Set up isolated worktrees for parallel work.",
        ),
    ]
    fake_index = index.Index(catalog=entries, embeddings=None)  # type: ignore[arg-type]
    monkeypatch.setattr("skill_advisor.matcher._load_index", lambda: fake_index)

    cfg = Config(parallelization=ParallelizationConfig(enabled=True, min_tasks=3))
    state = lifecycle.start("sess-m1", "build feature")
    lifecycle.enter_parallelization_check(state, ["A", "B", "C"])

    def _fake_detect(tasks, _cfg, **_kw):
        return parallelization.ParallelizationResult(
            parallel=True, groups=[[0, 2], [1]], reason="disjoint files"
        )
    monkeypatch.setattr("skill_advisor.matcher.parallelization.detect", _fake_detect)

    result = matcher.pick("go", cfg, session_id="sess-m1")
    assert result is not None
    names = [p.entry.name for p in result.picks]
    assert "superpowers:dispatching-parallel-agents" in names
    assert "superpowers:using-git-worktrees" in names
    # Auto-advanced to implementation after the nudge.
    reloaded = lifecycle.load("sess-m1")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.IMPLEMENTATION
    assert reloaded.pending_todos == []


def test_matcher_parallel_no_advances_and_uses_implementation_picks(monkeypatch):
    from skill_advisor import catalog, index, lifecycle, matcher, parallelization
    from skill_advisor.config import Config, ParallelizationConfig

    entries = [
        catalog.CatalogEntry(
            kind="skill", name="superpowers:executing-plans",
            namespace="plugin:superpowers",
            description="Execute plans task-by-task.",
        ),
    ]
    fake_index = index.Index(catalog=entries, embeddings=None)  # type: ignore[arg-type]
    monkeypatch.setattr("skill_advisor.matcher._load_index", lambda: fake_index)

    cfg = Config(parallelization=ParallelizationConfig(enabled=True, min_tasks=3))
    state = lifecycle.start("sess-m2", "build feature")
    lifecycle.enter_parallelization_check(state, ["A", "B", "C"])

    def _fake_detect(tasks, _cfg, **_kw):
        return parallelization.ParallelizationResult(parallel=False, groups=[], reason="sequential")
    monkeypatch.setattr("skill_advisor.matcher.parallelization.detect", _fake_detect)

    result = matcher.pick("go", cfg, session_id="sess-m2")
    assert result is not None
    names = [p.entry.name for p in result.picks]
    assert "superpowers:executing-plans" in names
    reloaded = lifecycle.load("sess-m2")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.IMPLEMENTATION
    assert reloaded.pending_todos == []


def test_matcher_detector_returns_none_silently_advances(monkeypatch):
    """Judge failure (timeout, no PATH) must not fail the hook — just advance."""
    from skill_advisor import catalog, index, lifecycle, matcher
    from skill_advisor.config import Config, ParallelizationConfig

    entries = [
        catalog.CatalogEntry(
            kind="skill", name="superpowers:executing-plans",
            namespace="plugin:superpowers",
            description="Execute plans task-by-task.",
        ),
    ]
    fake_index = index.Index(catalog=entries, embeddings=None)  # type: ignore[arg-type]
    monkeypatch.setattr("skill_advisor.matcher._load_index", lambda: fake_index)

    cfg = Config(parallelization=ParallelizationConfig(enabled=True, min_tasks=3))
    state = lifecycle.start("sess-m3", "build feature")
    lifecycle.enter_parallelization_check(state, ["A", "B", "C"])

    monkeypatch.setattr("skill_advisor.matcher.parallelization.detect", lambda *a, **k: None)

    result = matcher.pick("go", cfg, session_id="sess-m3")
    assert result is not None  # We still produce picks from the implementation phase.
    reloaded = lifecycle.load("sess-m3")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.IMPLEMENTATION


def test_judge_verdict_returned_explicitly_not_via_module_state():
    assert not hasattr(matcher, "_LAST_JUDGE_EFFORT")
    assert "judge_effort" in matcher.StatelessResult.__dataclass_fields__
    assert "judge_effort" in matcher.PickResult.__dataclass_fields__
