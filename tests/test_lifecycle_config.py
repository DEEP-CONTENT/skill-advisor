"""Config-driven overrides for lifecycle behavior."""
from __future__ import annotations

from unittest.mock import patch

import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import index as index_mod
from skill_advisor import lifecycle, matcher, paths
from skill_advisor.catalog import CatalogEntry
from skill_advisor.config import Config, LifecycleConfig, MatcherConfig, load as load_config


def _toy_catalog() -> list[CatalogEntry]:
    return [
        CatalogEntry(kind="subagent", name="Plan", namespace="builtin", description="design"),
        CatalogEntry(kind="skill", name="team-planner", namespace="user", description="team-specific"),
        CatalogEntry(kind="skill", name="team-review-checklist", namespace="user", description="team review"),
        CatalogEntry(kind="skill", name="code-reviewer", namespace="user", description="review"),
        CatalogEntry(kind="skill", name="commit", namespace="user", description="commit"),
    ]


def _prime_index(isolated_paths):
    entries = _toy_catalog()
    rng = np.random.default_rng(0)
    embeddings = rng.standard_normal((len(entries), 8)).astype(np.float32)
    embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
    catalog_mod.save(entries)
    index_mod.save(entries, embeddings, "hash")

    class _Stub:
        def embed(self, texts):
            for _ in texts:
                yield rng.standard_normal(8).astype(np.float32)

    return _Stub()


# ---------- parsing ----------


def test_load_lifecycle_defaults(isolated_paths):
    cfg = load_config()
    assert cfg.lifecycle.enabled is True
    assert cfg.lifecycle.max_correction_cycles == 3
    assert cfg.lifecycle.extra_trigger_patterns == ()
    assert cfg.lifecycle.phase_additions == {}


def test_load_lifecycle_parses_phase_additions(isolated_paths):
    paths.config_file().write_text(
        """
[lifecycle]
enabled = true
max_correction_cycles = 5
extra_trigger_patterns = ["\\\\bstand\\\\s+up\\\\b"]
extra_disable_patterns = ["^prototype:"]

[lifecycle.phase_additions]
planning = ["skill:team-planner"]

[lifecycle.phase_candidates]
review = ["skill:team-review-checklist"]
""",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.lifecycle.max_correction_cycles == 5
    assert cfg.lifecycle.extra_trigger_patterns == (r"\bstand\s+up\b",)
    assert cfg.lifecycle.extra_disable_patterns == ("^prototype:",)
    assert cfg.lifecycle.phase_additions == {"planning": (("skill", "team-planner"),)}
    assert cfg.lifecycle.phase_candidates == {"review": (("skill", "team-review-checklist"),)}


def test_load_lifecycle_drops_invalid_entries(isolated_paths):
    paths.config_file().write_text(
        """
[lifecycle.phase_additions]
planning = ["bogus-format-no-colon", "unknownkind:foo", "skill:ok"]
""",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.lifecycle.phase_additions == {"planning": (("skill", "ok"),)}


# ---------- trigger/disable pattern overrides ----------


def test_extra_trigger_pattern_fires():
    cfg = LifecycleConfig(extra_trigger_patterns=(r"\bstand\s+up\b",))
    assert lifecycle.is_trigger("please stand up the observability stack", cfg) is True
    # Still needs to be substantive (≥4 words).
    assert lifecycle.is_trigger("stand up", cfg) is False


def test_extra_disable_pattern_suppresses():
    cfg = LifecycleConfig(extra_disable_patterns=(r"^prototype:",))
    assert lifecycle.is_trigger("prototype: build a rate limiter for the api", cfg) is False


def test_disabled_never_triggers():
    cfg = LifecycleConfig(enabled=False)
    assert lifecycle.is_trigger("build a rate limiter for the api endpoints", cfg) is False


# ---------- phase preference merging ----------


def test_phase_additions_prepend_to_builtin():
    cfg = LifecycleConfig(phase_additions={"planning": (("skill", "team-planner"),)})
    picks = lifecycle.pick_candidates_for_phase(
        lifecycle.PLANNING, _toy_catalog(), limit=3, config=cfg
    )
    assert picks[0].name == "team-planner"
    # Built-in defaults still follow (Plan is one of the built-in preferences).
    assert any(p.name == "Plan" for p in picks)


def test_phase_candidates_fully_replace_builtin():
    cfg = LifecycleConfig(phase_candidates={"review": (("skill", "team-review-checklist"),)})
    picks = lifecycle.pick_candidates_for_phase(
        lifecycle.REVIEW, _toy_catalog(), limit=3, config=cfg
    )
    names = [p.name for p in picks]
    assert names == ["team-review-checklist"]


# ---------- end-to-end via matcher.pick ----------


def test_matcher_honors_enabled_false(isolated_paths):
    stub = _prime_index(isolated_paths)
    cfg = Config(lifecycle=LifecycleConfig(enabled=False))
    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick("build a rate limiter for the api", session_id="s1", config=cfg)
    # No lifecycle started; either picks come from the default matcher, or None.
    assert result is None or result.state is None
    assert lifecycle.load("s1") is None


def test_matcher_uses_custom_max_cycles(isolated_paths):
    stub = _prime_index(isolated_paths)
    cfg = Config(
        lifecycle=LifecycleConfig(max_correction_cycles=1),
        matcher=MatcherConfig(max_picks=3),
    )
    state = lifecycle.start("s1", "build a rate limiter for the api")
    lifecycle.advance(state, config=cfg.lifecycle)  # implementation
    lifecycle.advance(state, config=cfg.lifecycle)  # review

    # 1 correction allowed.
    lifecycle.advance(state, had_issues=True, config=cfg.lifecycle)  # correction
    lifecycle.advance(state, config=cfg.lifecycle)  # back to review
    # 2nd correction should be capped — forces COMPLETE.
    lifecycle.advance(state, had_issues=True, config=cfg.lifecycle)
    assert state.phase == lifecycle.COMPLETE


def test_matcher_uses_phase_additions_in_picks(isolated_paths):
    stub = _prime_index(isolated_paths)
    cfg = Config(
        lifecycle=LifecycleConfig(
            phase_additions={"planning": (("skill", "team-planner"),)}
        ),
    )
    with patch.object(index_mod, "_embed_model", return_value=stub):
        result = matcher.pick(
            "build a rate limiter for the api endpoints",
            session_id="s1",
            config=cfg,
        )
    assert result is not None
    # team-planner should appear in picks, and ideally first.
    names = [p.entry.name for p in result.picks]
    assert "team-planner" in names
    assert names[0] == "team-planner"
