import pytest

from skill_advisor import lifecycle
from skill_advisor.catalog import CatalogEntry


# ---------- Signal detection ----------


@pytest.mark.parametrize(
    "prompt",
    [
        "build a rate limiter for the api",
        "implement JWT auth in the middleware",
        "create a new onboarding flow for power users",
        "refactor the checkout module to use factories",
        "design a real-time notification system",
        "add a feature flag to the billing engine",
        "migrate mongodb user data to postgres",
    ],
)
def test_is_trigger_substantive_prompts(prompt):
    assert lifecycle.is_trigger(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "thanks",
        "just fix the typo in the readme",
        "quick tweak to the css",
        "one-off script to rename files",
        "create",  # too short
        "[no-lifecycle] build a rate limiter",
        "refactor",  # too short
    ],
)
def test_is_trigger_negatives(prompt):
    assert lifecycle.is_trigger(prompt) is False


@pytest.mark.parametrize(
    "p", ["go", "next", "continue", "OK", "ship it", "do it", "proceed"]
)
def test_is_continue_signal(p):
    assert lifecycle.is_continue_signal(p) is True


@pytest.mark.parametrize("p", ["stop", "cancel this please", "nevermind, new topic"])
def test_is_cancel_signal(p):
    assert lifecycle.is_cancel_signal(p) is True


@pytest.mark.parametrize("p", ["done", "looks good", "LGTM", "finished"])
def test_is_complete_signal(p):
    assert lifecycle.is_complete_signal(p) is True


def test_mentions_issues():
    assert lifecycle.mentions_issues("there are 3 bugs to fix") is True
    assert (
        lifecycle.mentions_issues("all good, no problems") is True
    )  # "problems" is in text
    assert lifecycle.mentions_issues("great work") is False


# ---------- State machine ----------


def test_linear_progression():
    assert lifecycle.next_phase(lifecycle.PLANNING) == lifecycle.IMPLEMENTATION
    assert lifecycle.next_phase(lifecycle.IMPLEMENTATION) == lifecycle.REVIEW
    assert (
        lifecycle.next_phase(lifecycle.REVIEW, had_issues=False) == lifecycle.COMPLETE
    )
    assert (
        lifecycle.next_phase(lifecycle.REVIEW, had_issues=True) == lifecycle.CORRECTION
    )
    assert lifecycle.next_phase(lifecycle.CORRECTION) == lifecycle.REVIEW


def test_start_and_advance_persists(isolated_paths):
    state = lifecycle.start("sess-1", "build a rate limiter for the API")
    assert state.phase == lifecycle.PLANNING
    assert state.original_prompt.startswith("build a rate limiter")

    reloaded = lifecycle.load("sess-1")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.PLANNING

    lifecycle.advance(state, note="user said go")
    assert state.phase == lifecycle.IMPLEMENTATION
    assert lifecycle.load("sess-1").phase == lifecycle.IMPLEMENTATION


def test_start_strips_no_lifecycle_marker(isolated_paths):
    state = lifecycle.start("sess-1", "[no-lifecycle] build something")
    # start() itself strips the marker but trusts the caller decided to start.
    assert state.original_prompt == "build something"


def test_cycle_cap_forces_complete(isolated_paths):
    state = lifecycle.start("sess-1", "build a feature")
    lifecycle.advance(state)  # implementation
    lifecycle.advance(state)  # review
    # Simulate 3 correction cycles.
    for _ in range(lifecycle.MAX_CORRECTION_CYCLES):
        lifecycle.advance(state, had_issues=True)  # correction
        lifecycle.advance(state)  # review
    assert state.cycles[lifecycle.CORRECTION] == lifecycle.MAX_CORRECTION_CYCLES
    # One more review-with-issues → forced complete.
    lifecycle.advance(state, had_issues=True)
    assert state.phase == lifecycle.COMPLETE


def test_cancel_is_terminal(isolated_paths):
    state = lifecycle.start("sess-1", "build a thing")
    lifecycle.cancel(state)
    assert state.is_active() is False
    assert lifecycle.load("sess-1").phase == lifecycle.CANCELLED


def test_force_complete(isolated_paths):
    state = lifecycle.start("sess-1", "build a thing")
    lifecycle.advance(state)
    lifecycle.advance(state)  # review
    lifecycle.force_complete(state)
    assert state.phase == lifecycle.COMPLETE


def test_delete_and_list(isolated_paths):
    lifecycle.start("sess-1", "build a feature")
    lifecycle.start("sess-2", "implement search")
    listed = lifecycle.list_sessions()
    assert {s.session_id for s in listed} == {"sess-1", "sess-2"}

    assert lifecycle.delete("sess-1") is True
    assert lifecycle.delete("nonexistent") is False
    assert [s.session_id for s in lifecycle.list_sessions()] == ["sess-2"]


# ---------- Phase → picks ----------


def _cat(*entries):
    return list(entries)


def _e(kind: str, name: str) -> CatalogEntry:
    return CatalogEntry(kind=kind, name=name, namespace="user", description="...")


def test_pick_candidates_for_phase_prefers_ranked_names():
    catalog = _cat(
        _e("subagent", "Plan"),
        _e("skill", "brainstorming"),
        _e("skill", "random-skill"),
    )
    picks = lifecycle.pick_candidates_for_phase(lifecycle.PLANNING, catalog, limit=2)
    assert [p.name for p in picks] == ["Plan", "brainstorming"]


def test_pick_candidates_for_phase_returns_empty_when_nothing_matches():
    catalog = _cat(_e("skill", "unrelated-skill"))
    picks = lifecycle.pick_candidates_for_phase(lifecycle.REVIEW, catalog, limit=3)
    assert picks == []


def test_pick_candidates_respects_kind_mismatch():
    # If the user's catalog has a skill named "Plan" (not a subagent), we skip it —
    # the preference explicitly wants a subagent.
    catalog = _cat(_e("skill", "Plan"))
    picks = lifecycle.pick_candidates_for_phase(lifecycle.PLANNING, catalog, limit=2)
    assert picks == []


def test_phase_candidates_skip_disabled_and_fall_through():
    """Live trace 2026-07-30: PLANNING resolved to
       Plan (enabled) · writing-plans (enabled) · plan-writing (OFF)
    so every planning turn emitted a disabled skill as pick #3 — 490 times.
    The ordered list already contains enabled alternatives further down; they
    were simply never reached."""

    def _skill(name, enabled):
        return CatalogEntry(
            kind="skill",
            name=name,
            namespace="user",
            description="d",
            path=f"/s/{name}/SKILL.md",
            enabled=enabled,
        )

    catalog = [
        CatalogEntry(
            kind="subagent", name="Plan", namespace="builtin", description="d"
        ),
        _skill("writing-plans", True),
        _skill("plan-writing", False),
        _skill("brainstorming", True),
    ]
    picks = lifecycle.pick_candidates_for_phase(lifecycle.PLANNING, catalog, limit=3)
    names = [e.name for e in picks]
    assert "plan-writing" not in names
    assert names == ["Plan", "writing-plans", "brainstorming"]


def test_phase_candidates_return_empty_when_all_are_disabled():
    """Callers already fall back to the embedding matcher on an empty list."""
    catalog = [
        CatalogEntry(
            kind="skill",
            name="plan-writing",
            namespace="user",
            description="d",
            path="/s/plan-writing/SKILL.md",
            enabled=False,
        ),
    ]
    assert (
        lifecycle.pick_candidates_for_phase(lifecycle.PLANNING, catalog, limit=3) == []
    )


def test_pick_candidates_for_phase_resolves_bare_and_namespaced_separately():
    """F2: a bare directory name can exist in BOTH the user skills and the
    plugin cache — e.g. a user skill `brainstorming` AND a plugin skill
    whose frontmatter `name:` is ALSO "brainstorming" but whose invocable
    identity is "superpowers:brainstorming". Keying the phase-preference
    lookup on `entry.name` (the frontmatter name) collides the two: since
    plugins/cache is scanned after skills/, last-wins in the dict
    comprehension silently resolves the BARE "brainstorming" preference to
    the PLUGIN entry, not the user's.

    Here the plugin entry is disabled and the user's is enabled, so the old
    bug loses the pick entirely: the bare preference mis-resolves to the
    disabled plugin entry and is filtered out by `pickable_only`, and the
    namespaced preference ("superpowers:brainstorming") never matches
    anything at all under the old code, because no catalog entry's
    frontmatter `name:` is ever namespaced. The user's perfectly enabled
    skill is unreachable either way."""

    def _entry(name, namespace, invoke_name, enabled):
        return CatalogEntry(
            kind="skill",
            name=name,
            namespace=namespace,
            description="d",
            path=f"/s/{name}/SKILL.md",
            enabled=enabled,
            invoke_name=invoke_name,
        )

    # Scan order matters: skills/ is scanned before plugins/cache, so the
    # plugin entry appears LATER in the catalog list — reproducing the exact
    # last-wins collision `catalog.scan()` produces in practice.
    catalog = [
        _entry("brainstorming", "user", "brainstorming", enabled=True),
        _entry(
            "brainstorming",
            "plugin:superpowers",
            "superpowers:brainstorming",
            enabled=False,
        ),
    ]
    picks = lifecycle.pick_candidates_for_phase(lifecycle.PLANNING, catalog, limit=3)
    assert [p.invoke_name for p in picks] == ["brainstorming"]


def test_phase_candidates_can_opt_out_of_filtering():
    """The rotation needs to see what a phase WOULD pick from the whole pool."""
    catalog = [
        CatalogEntry(
            kind="skill",
            name="plan-writing",
            namespace="user",
            description="d",
            path="/s/plan-writing/SKILL.md",
            enabled=False,
        ),
    ]
    picks = lifecycle.pick_candidates_for_phase(
        lifecycle.PLANNING, catalog, limit=3, pickable_only=False
    )
    assert [e.name for e in picks] == ["plan-writing"]


# ---------- advance(source=...) + TurnState ----------


def test_advance_defaults_source_to_user(isolated_paths):
    state = lifecycle.start("s1", "build a rate limiter")
    lifecycle.advance(state)
    latest = lifecycle.load("s1").history[-1]
    assert latest.get("source") == "user"


def test_advance_honours_source_auto(isolated_paths):
    state = lifecycle.start("s1", "build a rate limiter")
    lifecycle.advance(state, source="auto", note="auto from Stop hook")
    latest = lifecycle.load("s1").history[-1]
    assert latest.get("source") == "auto"


def test_turn_state_record_and_load(isolated_paths):
    lifecycle.record_tool("s1", "Edit")
    lifecycle.record_tool("s1", "Task", subagent_type="Plan")
    turn = lifecycle.load_turn("s1")
    assert turn.tool_names == ["Edit", "Task"]
    assert turn.subagents_invoked == ["Plan"]
    assert turn.has_mutating_tool() is True


def test_turn_state_readonly_tools(isolated_paths):
    for t in ("Read", "Grep", "Glob", "LS"):
        lifecycle.record_tool("s1", t)
    assert lifecycle.load_turn("s1").has_mutating_tool() is False


def test_turn_state_delete(isolated_paths):
    lifecycle.record_tool("s1", "Edit")
    assert lifecycle.delete_turn("s1") is True
    assert lifecycle.load_turn("s1") is None
    assert lifecycle.delete_turn("s1") is False  # idempotent


def test_turn_state_roundtrip(isolated_paths):
    original = lifecycle.TurnState(
        session_id="s1", tool_names=["Edit", "Read"], subagents_invoked=["Plan"]
    )
    lifecycle.save_turn(original)
    loaded = lifecycle.load_turn("s1")
    assert loaded.session_id == "s1"
    assert loaded.tool_names == ["Edit", "Read"]
    assert loaded.subagents_invoked == ["Plan"]


def test_turn_state_records_todo_write(isolated_paths):
    from skill_advisor import lifecycle

    turn = lifecycle.record_todo_write(
        session_id="sess-xyz",
        todos=["Write parser", "Wire CLI flag", "Add tests"],
    )
    assert turn.todo_write == {
        "count": 3,
        "titles": ["Write parser", "Wire CLI flag", "Add tests"],
    }
    # Round-trip through disk.
    reloaded = lifecycle.load_turn("sess-xyz")
    assert reloaded is not None
    assert reloaded.todo_write == turn.todo_write


def test_append_todo_title_accumulates_across_calls(isolated_paths):
    """append_todo_title supports the per-call TaskCreate semantics."""
    from skill_advisor import lifecycle

    lifecycle.append_todo_title("sess-app", "Parse X")
    lifecycle.append_todo_title("sess-app", "Wire Y")
    turn = lifecycle.append_todo_title("sess-app", "Test Z")

    assert turn.todo_write == {
        "count": 3,
        "titles": ["Parse X", "Wire Y", "Test Z"],
    }
    reloaded = lifecycle.load_turn("sess-app")
    assert reloaded is not None
    assert reloaded.todo_write == turn.todo_write


def test_append_todo_title_skips_blank(isolated_paths):
    from skill_advisor import lifecycle

    lifecycle.append_todo_title("sess-blank", "real one")
    lifecycle.append_todo_title("sess-blank", "   ")  # whitespace-only → skip
    lifecycle.append_todo_title("sess-blank", "")  # empty → skip
    turn = lifecycle.load_turn("sess-blank")
    assert turn is not None
    assert turn.todo_write == {"count": 1, "titles": ["real one"]}


def test_append_todo_title_caps_at_50(isolated_paths):
    from skill_advisor import lifecycle

    for i in range(60):
        lifecycle.append_todo_title("sess-cap", f"task {i}")
    turn = lifecycle.load_turn("sess-cap")
    assert turn is not None
    assert turn.todo_write["count"] == 50
    assert len(turn.todo_write["titles"]) == 50


def test_append_todo_title_coexists_with_record_todo_write(isolated_paths):
    """A real TodoWrite mid-turn replaces accumulated TaskCreate titles
    (last-write-wins is the documented contract for record_todo_write)."""
    from skill_advisor import lifecycle

    lifecycle.append_todo_title("sess-mix", "tc-1")
    lifecycle.append_todo_title("sess-mix", "tc-2")
    lifecycle.record_todo_write("sess-mix", ["batched-A", "batched-B", "batched-C"])
    turn = lifecycle.load_turn("sess-mix")
    assert turn is not None
    assert turn.todo_write == {
        "count": 3,
        "titles": ["batched-A", "batched-B", "batched-C"],
    }


def test_turn_state_json_roundtrip_without_todo_write():
    # Older turn files on disk don't have the field — must still load.
    from skill_advisor import lifecycle

    turn = lifecycle.TurnState.from_json(
        {
            "session_id": "sess-legacy",
            "turn_started_at": 1234.0,
            "tool_names": ["Edit"],
            "subagents_invoked": [],
        }
    )
    assert turn.todo_write is None


def test_parallelization_check_phase_constants():
    from skill_advisor import lifecycle

    assert lifecycle.PARALLELIZATION_CHECK == "parallelization_check"
    assert lifecycle.PARALLELIZATION_CHECK in lifecycle.ACTIVE_PHASES
    assert lifecycle.PARALLELIZATION_CHECK in lifecycle.PHASE_CANDIDATES
    names = [
        name
        for _kind, name in lifecycle.PHASE_CANDIDATES[lifecycle.PARALLELIZATION_CHECK]
    ]
    # Must reference the canonical superpowers variants first.
    assert "superpowers:dispatching-parallel-agents" in names
    assert "superpowers:using-git-worktrees" in names


def test_enter_parallelization_check_sets_pending_todos():
    from skill_advisor import lifecycle

    state = lifecycle.start("sess-p", "build multi-part feature")
    assert state.phase == lifecycle.PLANNING
    assert state.pending_todos == []

    state = lifecycle.enter_parallelization_check(state, ["T1", "T2", "T3"])
    assert state.phase == lifecycle.PARALLELIZATION_CHECK
    assert state.pending_todos == ["T1", "T2", "T3"]
    # Persisted.
    reloaded = lifecycle.load("sess-p")
    assert reloaded is not None
    assert reloaded.phase == lifecycle.PARALLELIZATION_CHECK
    assert reloaded.pending_todos == ["T1", "T2", "T3"]


def test_next_phase_parallelization_check_to_implementation():
    from skill_advisor import lifecycle

    assert (
        lifecycle.next_phase(lifecycle.PARALLELIZATION_CHECK)
        == lifecycle.IMPLEMENTATION
    )


def test_phase_next_description_parallelization_check():
    from skill_advisor import lifecycle

    desc = lifecycle.phase_next_description(lifecycle.PARALLELIZATION_CHECK)
    assert "implementation" in desc.lower()


def test_record_tool_appends_a_mark_parallel_to_the_name(isolated_paths):
    from skill_advisor import lifecycle

    lifecycle.record_tool("sess-marks", "Read")
    lifecycle.record_tool("sess-marks", "Bash")
    turn = lifecycle.load_turn("sess-marks")

    assert turn.tool_names == ["Read", "Bash"]
    assert len(turn.tool_marks) == 2
    assert turn.tool_marks[1] >= turn.tool_marks[0]
    assert turn.tool_marks[0] >= turn.turn_started_at


def test_from_json_without_tool_marks_yields_empty_list(isolated_paths):
    """A turn file written before this feature must load, not crash."""
    from skill_advisor.lifecycle import TurnState

    turn = TurnState.from_json({
        "session_id": "old",
        "turn_started_at": 1000.0,
        "tool_names": ["Read", "Bash"],
    })

    assert turn.tool_names == ["Read", "Bash"]
    assert turn.tool_marks == []


def test_tool_marks_survive_a_save_load_round_trip(isolated_paths):
    from skill_advisor import lifecycle

    lifecycle.record_tool("sess-rt", "Grep")
    first = lifecycle.load_turn("sess-rt").tool_marks
    lifecycle.record_tool("sess-rt", "Edit")
    second = lifecycle.load_turn("sess-rt").tool_marks

    assert second[0] == first[0]  # the earlier mark was not rewritten
    assert len(second) == 2
