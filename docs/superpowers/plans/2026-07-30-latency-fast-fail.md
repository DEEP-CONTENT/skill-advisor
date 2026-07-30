# Latency: fail fast, never empty-handed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the `claude -p` judge times out or fails, emit the embedding picks that were already computed instead of returning nothing — and cut the judge budget from 25 s to 8 s so the failure happens fast.

**Architecture:** This is change 2 of `docs/superpowers/specs/2026-07-29-latency-design.md`, extracted as its own plan because it is independent of the embedding index and therefore ships *before* the catalog refresh. Three moving parts: `judge.rank()` gains a `failure` reason so its caller can tell a timeout from a genuine decline; `matcher.pick_stateless()` falls back to the already-ranked embedding list on failure but preserves the judge's 1,251 real declines; and the two independent timeout layers (`judge`'s subprocess timeout and the hook's `SIGALRM` watchdog) get retuned together with an invariant test so nobody inverts them.

**Tech Stack:** Python 3.12, `uv`, pytest, stdlib `subprocess`/`signal`. No new dependencies.

## Global Constraints

- Hook paths are **silent on error**. Nothing added here may raise out of `hook.run()`. CLI verbs (`doctor`) *should* fail loudly.
- `budget_seconds` is a **whole-hook** budget enforced by `signal.alarm()` at `hook.py:133`, not just the judge's timeout. The judge's own subprocess timeout is `budget_seconds - 0.5` (`judge.py:78`). **The judge timeout must always be strictly less than the alarm**, or the alarm kills the process before the fallback can run.
- Target after this plan: `matcher.budget_seconds = 8.0`, `parallelization.judge_timeout_seconds = 5.0`.
- Do not change `use_judge`'s shipped default (`False`). The judge stays opt-in.
- Test suite baseline is **411 passing in 0.66 s**. Keep it fast: every test here stubs the subprocess. No test may actually invoke `claude`.
- Existing behaviour that must NOT change: a judge that *ran and declined* (`{"picks": [], "skip": true}`) still produces zero picks. Those 1,251 declines are the judge's entire value and this plan does not remove them.

---

## Audit corrections to the spec

The spec was written against measurements that no longer match the code. These are settled; the tasks below already account for them. Do not re-litigate them mid-implementation.

| Spec says | Reality | Task |
|---|---|---|
| "Cut `budget_seconds` from 25 to ~8" | The **shipped default is 4.0** (`config.py:16`) and `use_judge` defaults to **False** (`config.py:20`). 25/true are only in the user's live `~/.config/skill-advisor/config.toml`. Three artifacts need editing, not one. | 4 |
| "Fall back to embedding picks **on timeout**" | `judge.rank()` returns `None` for six different reasons and the caller cannot distinguish them. The return contract has to change first. | 1, 2 |
| Implicitly, that the judge's timeout is the only timeout | There are **two** layers. `hook.py:133` wraps all of `matcher.pick()` in `signal.alarm(int(budget_seconds + 0.5))`, and `_BudgetExceeded` returns silent — bypassing any matcher-level fallback. Only a 0.5 s margin separates them. | 4 |
| `judge_used` telemetry measures judge usage | `hook.py:243` records `judge_used=cfg.matcher.use_judge` — the **config flag**, not whether the judge ran. Every event since 2026-05-19 says `true`, including 830 triage-skipped events where it never ran. | 3 |
| The 261 ms embedding baseline is comparable | All 838 `judge_used=False` events date from 2026-04-28..05-19, against a since-changed catalog. It is a cross-era comparison, not an A/B. | 4 (re-measure) |

**Accepted consequence of Task 4, flagged deliberately:** dropping `parallelization.judge_timeout_seconds` to 5.0 means the parallelization detector — which makes its own ~12 s `claude -p` call — will usually time out and return `None`. That degrades gracefully (`_parallelization_picks` returns `[]`, the lifecycle still auto-advances, implementation picks are still shown), but the feature is effectively dormant under the short budget. The spec explicitly sanctions this ("or accepting that the parallelization check does not run under the shorter budget"). A future option, out of scope here, is to give the `PARALLELIZATION_CHECK` phase its own longer alarm since it fires at most once per lifecycle.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/skill_advisor/judge.py` | Owns the `claude -p` subprocess and its failure taxonomy. | Modify: `JudgeResult` gains `failure`; `rank()` always returns a `JudgeResult`. |
| `src/skill_advisor/matcher.py` | Orchestration. Decides what a judge failure means. | Modify: extract `_embedding_picks()`, add the fallback branch, thread `JudgeTrace`. |
| `src/skill_advisor/hook.py` | Hot path + telemetry emission. | Modify: create a `JudgeTrace`, pass it to `matcher.pick()`, record honest `judge_used`. |
| `src/skill_advisor/telemetry.py` | Event schema. | Modify: `record()` gains `judge_failure`. |
| `src/skill_advisor/config.py` | Defaults. | Modify: `budget_seconds` 4.0 → 8.0, `judge_timeout_seconds` 20.0 → 5.0, comments. |
| `src/skill_advisor/install.py` | Generated `config.toml` template. | Modify: lines 219, 297-301. |
| `tests/test_judge.py` | | Modify: two `is None` assertions become `.failure` assertions; add failure-taxonomy tests. |
| `tests/test_matcher.py` | | Modify: existing `JudgeResult(...)` stubs still construct fine (new field defaults). Add fallback tests. |
| `tests/test_hook.py` | | Add: honest-`judge_used` tests. |
| `tests/test_config.py` | | Add: the timeout-ordering invariant test. |
| `tests/test_doctor_cli.py` | | Add: doctor still warns below the threshold at the new defaults. |

---

### Task 1: Make a judge failure distinguishable from a judge decline

`judge.rank()` currently returns `None` for six unrelated conditions. The caller needs to know which, because a decline must stay silent and a failure must fall back.

**Files:**
- Modify: `src/skill_advisor/judge.py:28-31` (`JudgeResult`), `65-100` (`rank`)
- Test: `tests/test_judge.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `judge.JudgeResult(picks: list[Pick], effort: str | None = None, failure: str | None = None)` — frozen dataclass. `failure is None` means the judge **ran and answered**, even if `picks == []` (a decline). A non-`None` `failure` is one of the string constants below.
  - `judge.FAILURE_NO_CANDIDATES = "no_candidates"`, `FAILURE_CLI_MISSING = "cli_missing"`, `FAILURE_TIMEOUT = "timeout"`, `FAILURE_SUBPROCESS = "subprocess_error"`, `FAILURE_EXIT = "exit_nonzero"`, `FAILURE_UNPARSEABLE = "unparseable"`.
  - `judge.rank(prompt, candidates, config, timeout=None) -> JudgeResult` — **never returns `None` any more**.
  - `judge._parse_judge_reply(stdout, candidates) -> JudgeResult | None` — unchanged, still internal, still may return `None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_judge.py`:

```python
def test_rank_reports_cli_missing_not_none():
    with patch("skill_advisor.judge.shutil.which", return_value=None):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_CLI_MISSING
    assert result.picks == []


def test_rank_reports_timeout():
    import subprocess
    with patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"), patch(
        "skill_advisor.judge.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0),
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_TIMEOUT


def test_rank_reports_nonzero_exit():
    completed = type("R", (), {"returncode": 3, "stdout": "", "stderr": "boom"})()
    with patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"), patch(
        "skill_advisor.judge.subprocess.run", return_value=completed
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_EXIT


def test_rank_reports_unparseable_reply():
    completed = type("R", (), {"returncode": 0, "stdout": "not json", "stderr": ""})()
    with patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"), patch(
        "skill_advisor.judge.subprocess.run", return_value=completed
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.failure == judge.FAILURE_UNPARSEABLE


def test_rank_reports_no_candidates():
    result = judge.rank("anything", [], Config())
    assert result.failure == judge.FAILURE_NO_CANDIDATES


def test_a_genuine_decline_is_not_a_failure():
    """The judge ran and said 'nothing fits'. That is a verdict, not an error."""
    inner = json.dumps({"picks": [], "skip": True})
    completed = type("R", (), {"returncode": 0, "stdout": json.dumps({"result": inner}), "stderr": ""})()
    with patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"), patch(
        "skill_advisor.judge.subprocess.run", return_value=completed
    ):
        result = judge.rank("anything", _candidates(), Config())
    assert result.picks == []
    assert result.failure is None
```

Also **replace** the two existing tests that assert `is None`:

```python
# tests/test_judge.py — was test_rank_returns_none_when_claude_not_on_path
def test_rank_when_claude_not_on_path_yields_no_picks():
    with patch("skill_advisor.judge.shutil.which", return_value=None):
        result = judge.rank("anything", _candidates(), Config())
        assert result.picks == []


# tests/test_judge.py — was test_rank_returns_none_on_timeout
def test_rank_on_timeout_yields_no_picks():
    import subprocess
    with patch("skill_advisor.judge.shutil.which", return_value="/usr/bin/claude"), patch(
        "skill_advisor.judge.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0),
    ):
        assert judge.rank("anything", _candidates(), Config()).picks == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_judge.py -q
```

Expected: FAIL — `AttributeError: module 'skill_advisor.judge' has no attribute 'FAILURE_CLI_MISSING'`, and `AttributeError: 'NoneType' object has no attribute 'picks'` on the rewritten pair.

- [ ] **Step 3: Add the failure taxonomy to `JudgeResult`**

In `src/skill_advisor/judge.py`, after the `log = logging.getLogger(__name__)` line:

```python
# Why the judge produced no verdict. `None` means it ran and answered —
# including answering "nothing fits", which is a verdict, not a failure.
FAILURE_NO_CANDIDATES = "no_candidates"
FAILURE_CLI_MISSING = "cli_missing"
FAILURE_TIMEOUT = "timeout"
FAILURE_SUBPROCESS = "subprocess_error"
FAILURE_EXIT = "exit_nonzero"
FAILURE_UNPARSEABLE = "unparseable"
```

Replace the `JudgeResult` dataclass:

```python
@dataclass(frozen=True)
class JudgeResult:
    picks: list[Pick]
    effort: str | None = None
    # None ⟺ the judge ran and returned a usable verdict. Callers use this to
    # tell a 24-second timeout apart from a deliberate "nothing fits" — the
    # first should fall back to the embedding ranking, the second must not.
    failure: str | None = None

    @property
    def ran(self) -> bool:
        return self.failure is None
```

- [ ] **Step 4: Make `rank()` always return a `JudgeResult`**

Replace `src/skill_advisor/judge.py:65-100` with:

```python
def rank(
    prompt: str, candidates: list[CatalogEntry], config: Config, timeout: float | None = None
) -> JudgeResult:
    """Rank `candidates` with `claude -p`.

    Never returns None. A result with `failure is None` means the judge ran and
    answered; `picks == []` in that case is a deliberate decline and callers
    must respect it. A non-None `failure` means no verdict was obtained and the
    caller should fall back to whatever it already has.
    """
    if not candidates:
        return JudgeResult(picks=[], failure=FAILURE_NO_CANDIDATES)
    if shutil.which("claude") is None:
        log.warning("claude CLI not on PATH; judge skipped")
        return JudgeResult(picks=[], failure=FAILURE_CLI_MISSING)

    judge_prompt = _JUDGE_TEMPLATE.format(
        prompt=prompt.strip(),
        candidates=_render_candidates(candidates),
    )
    budget = timeout if timeout is not None else max(config.matcher.budget_seconds - 0.5, 0.5)

    try:
        completed = subprocess.run(
            ["claude", "-p", "--model", config.matcher.model, "--output-format", "json"],
            input=judge_prompt,
            capture_output=True,
            text=True,
            timeout=budget,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.info("judge timed out after %.2fs", budget)
        return JudgeResult(picks=[], failure=FAILURE_TIMEOUT)
    except (OSError, ValueError) as exc:
        log.warning("judge subprocess failed: %s", exc)
        return JudgeResult(picks=[], failure=FAILURE_SUBPROCESS)

    if completed.returncode != 0:
        log.warning("judge exit %s: %s", completed.returncode, completed.stderr[:200])
        return JudgeResult(picks=[], failure=FAILURE_EXIT)

    parsed = _parse_judge_reply(completed.stdout, candidates)
    if parsed is None:
        return JudgeResult(picks=[], failure=FAILURE_UNPARSEABLE)
    return parsed
```

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest -q
```

Expected: PASS, 411 + 7 = 418 or more. If `tests/test_matcher.py` fails here, stop — it means a call site still branches on `is None` and Task 2 has to land first; note it and continue to Task 2 rather than patching around it.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/skill_advisor/judge.py tests/test_judge.py && uv run ruff format --check src/skill_advisor/judge.py tests/test_judge.py
git add src/skill_advisor/judge.py tests/test_judge.py
git commit -m "refactor(judge): distinguish a failed judge from a declining one"
```

---

### Task 2: Fall back to the embedding ranking when the judge fails

The ranking is already computed at `matcher.py:63` before the judge is ever called. Today it is discarded on failure. This is the change that converts 432 empty 25-second waits into 432 answers.

**Files:**
- Modify: `src/skill_advisor/matcher.py:23-29` (`StatelessResult`), `63-85` (`pick_stateless` body)
- Test: `tests/test_matcher.py`

**Interfaces:**
- Consumes: `judge.JudgeResult.failure`, `judge.FAILURE_TIMEOUT` from Task 1.
- Produces:
  - `matcher.StatelessResult(picks: list[ResolvedPick], judge_effort: str | None = None, judge_ran: bool = False, judge_failure: str | None = None)`
  - `matcher._embedding_picks(ranked: list[tuple[CatalogEntry, float]], k_picks: int, min_score: float, *, fallback: bool = False) -> list[ResolvedPick]` — shared by the confident path and the fallback path. `fallback=True` changes only the reason string.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matcher.py`:

```python
def test_judge_timeout_falls_back_to_embedding_picks(isolated_paths):
    """The 432-empty-timeouts bug. Must fail against today's code."""
    from skill_advisor.config import Config
    from skill_advisor.judge import FAILURE_TIMEOUT, JudgeResult

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        mock_rank.return_value = JudgeResult(picks=[], failure=FAILURE_TIMEOUT)
        result = matcher.pick_stateless(
            "q", Config(), force_judge=True, top_k=3, candidates=3, threshold=0.0,
        )

    assert [p.entry.name for p in result.picks] == ["alpha", "beta", "gamma"]
    assert result.judge_failure == FAILURE_TIMEOUT
    assert result.judge_ran is False
    assert "fallback" in result.picks[0].reason


def test_judge_decline_still_returns_nothing(isolated_paths):
    """The judge's 1,251 real declines are its value. Do not convert them to picks."""
    from skill_advisor.config import Config
    from skill_advisor.judge import JudgeResult

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        mock_rank.return_value = JudgeResult(picks=[], failure=None)
        result = matcher.pick_stateless(
            "q", Config(), force_judge=True, top_k=3, candidates=3, threshold=0.0,
        )

    assert result.picks == []
    assert result.judge_ran is True
    assert result.judge_failure is None


def test_fallback_respects_min_embedding_score(isolated_paths):
    """A fallback must not surface junk the confident path would have suppressed."""
    from skill_advisor.config import Config
    from skill_advisor.judge import FAILURE_TIMEOUT, JudgeResult

    stub = _prime_stateless_index([0.9, 0.1, 0.05])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        mock_rank.return_value = JudgeResult(picks=[], failure=FAILURE_TIMEOUT)
        result = matcher.pick_stateless(
            "q", Config(), force_judge=True, top_k=3, candidates=3, threshold=0.35,
        )

    assert [p.entry.name for p in result.picks] == ["alpha"]


def test_judge_success_marks_judge_ran(isolated_paths):
    from skill_advisor.config import Config
    from skill_advisor.judge import JudgeResult
    from skill_advisor.judge import Pick as JudgePick

    stub = _prime_stateless_index([0.9, 0.7, 0.5])
    with patch.object(index_mod, "_embed_model", return_value=stub), \
         patch("skill_advisor.matcher.judge.rank") as mock_rank:
        mock_rank.return_value = JudgeResult(picks=[JudgePick(name="beta", reason="fits")])
        result = matcher.pick_stateless(
            "q", Config(), force_judge=True, top_k=3, candidates=3, threshold=0.0,
        )

    assert result.judge_ran is True
    assert result.judge_failure is None
    assert [p.entry.name for p in result.picks] == ["beta"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_matcher.py -q -k "fallback or decline or judge_ran"
```

Expected: FAIL — `AttributeError: 'StatelessResult' object has no attribute 'judge_failure'`, and the timeout test returns `picks == []`.

- [ ] **Step 3: Extend `StatelessResult`**

Replace `src/skill_advisor/matcher.py:31-34`:

```python
@dataclass(frozen=True)
class StatelessResult:
    picks: list[ResolvedPick]
    judge_effort: str | None = None
    # True ⟺ the judge subprocess ran and returned a verdict this call.
    judge_ran: bool = False
    # Non-None ⟺ the judge was asked but produced nothing usable; see judge.FAILURE_*.
    judge_failure: str | None = None
```

- [ ] **Step 4: Extract the shared embedding-picks builder**

Add above `pick_stateless` in `src/skill_advisor/matcher.py`:

```python
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
```

- [ ] **Step 5: Rewrite the judge branch to fall back**

Replace `src/skill_advisor/matcher.py:65-85` (the `if use_judge:` block through the end of the function) with:

```python
    if use_judge:
        cand_entries = [e for e, _ in ranked]
        if not cand_entries:
            return StatelessResult(picks=[])
        raw = judge.rank(prompt, cand_entries, cfg)
        if raw.failure is not None:
            # The judge produced no verdict. The embedding ranking is already in
            # hand and cost nothing extra — emitting it beats going silent.
            log.info("judge unavailable (%s); using embedding fallback", raw.failure)
            return StatelessResult(
                picks=_embedding_picks(ranked, k_picks, min_score, fallback=True),
                judge_ran=False,
                judge_failure=raw.failure,
            )
        by_name = {e.name: e for e in cand_entries}
        out: list[ResolvedPick] = []
        for p in raw.picks[:k_picks]:
            entry = by_name.get(p.name)
            if entry is not None:
                out.append(ResolvedPick(entry=entry, reason=p.reason))
        # An empty `out` here is the judge declining. Respect it — do not fall back.
        return StatelessResult(picks=out, judge_effort=raw.effort, judge_ran=True)

    return StatelessResult(picks=_embedding_picks(ranked, k_picks, min_score))
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/test_matcher.py -q && uv run pytest -q
```

Expected: PASS. `test_pick_stateless_returns_top_k_in_score_order` (`tests/test_matcher.py:177`) asserts `"0.90" in picks[0].reason` — the reason contains the score, not the literal `"embedding match"` label — so it still passes.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/skill_advisor/matcher.py tests/test_matcher.py && uv run ruff format --check src/skill_advisor/matcher.py tests/test_matcher.py
git add src/skill_advisor/matcher.py tests/test_matcher.py
git commit -m "fix(matcher): emit embedding picks when the judge fails instead of nothing"
```

---

### Task 3: Make `judge_used` telemetry mean "the judge ran"

`hook.py:243` records the config flag. Every event since 2026-05-19 claims `judge_used: true`, including 830 triage-skipped prompts where no subprocess was ever spawned. Without this fix the fallback's effect cannot be measured, and the escalation rate in the follow-on plan cannot be measured at all.

**Files:**
- Modify: `src/skill_advisor/matcher.py` (add `JudgeTrace`, thread it through `pick`/`_pick_inner`/`_default_picks`/`pick_stateless`)
- Modify: `src/skill_advisor/hook.py:128` (create the trace), `243` (record it)
- Modify: `src/skill_advisor/telemetry.py:71-128` (`record` gains `judge_failure`)
- Test: `tests/test_hook.py`, `tests/test_telemetry.py`

**Interfaces:**
- Consumes: `StatelessResult.judge_ran` / `.judge_failure` from Task 2.
- Produces:
  - `matcher.JudgeTrace` — a **mutable** (non-frozen) dataclass: `ran: bool = False`, `failure: str | None = None`. A collector, because `_pick_inner` legitimately returns `None` when there are no picks and would otherwise throw the judge's outcome away — which is exactly the decline case we most need to count.
  - `matcher.pick(prompt, config=None, session_id=None, *, trace: JudgeTrace | None = None) -> PickResult | None`
  - `matcher.pick_stateless(..., trace: JudgeTrace | None = None) -> StatelessResult`
  - `telemetry.record(..., judge_failure: str | None = None)` — new keyword, appended to the event as `"judge_failure"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_hook.py` already has the idiom for this — `_run_with_stdin(payload)` at line 10, `_enable_telemetry_in_config(isolated_paths)` at line 63, and `patch("skill_advisor.hook.matcher.pick", ...)`. Use them rather than monkeypatching `_read_input` or `load_config`.

First add a config helper next to `_enable_telemetry_in_config`, because these tests need `use_judge = true` to be meaningful — the whole point is that the flag is on while the judge did not run:

```python
def _enable_telemetry_and_judge(isolated_paths):
    """Telemetry on AND use_judge on — so `judge_used` echoing the config is
    distinguishable from `judge_used` reporting what actually happened."""
    cfg = isolated_paths["config_home"] / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "[telemetry]\nevents_enabled = true\nprompt_hash_salt = \"fixed\"\n"
        "\n[matcher]\nuse_judge = true\n"
    )
```

Then append:

```python
def test_judge_used_is_false_when_the_judge_never_ran(isolated_paths):
    """Today `judge_used` records cfg.matcher.use_judge, so 830 triage-skipped
    events on the live log claim the judge ran. It must report what happened."""
    _enable_telemetry_and_judge(isolated_paths)
    with patch("skill_advisor.hook.matcher.pick", return_value=None):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert lines[-1]["judge_used"] is False


def test_judge_failure_is_recorded(isolated_paths):
    _enable_telemetry_and_judge(isolated_paths)

    def _fake_pick(prompt, cfg, session_id=None, *, trace=None):
        if trace is not None:
            trace.ran = False
            trace.failure = "timeout"
        return None

    with patch("skill_advisor.hook.matcher.pick", _fake_pick):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert lines[-1]["judge_failure"] == "timeout"
    assert lines[-1]["judge_used"] is False


def test_judge_used_is_true_when_the_judge_actually_ran(isolated_paths):
    _enable_telemetry_and_judge(isolated_paths)
    entry = CatalogEntry(kind="skill", name="brainstorming", namespace="user", description="...")
    result = PickResult(picks=[ResolvedPick(entry=entry, reason="judge said so")], state=None)

    def _fake_pick(prompt, cfg, session_id=None, *, trace=None):
        if trace is not None:
            trace.ran = True
        return result

    with patch("skill_advisor.hook.matcher.pick", _fake_pick):
        _run_with_stdin({"prompt": "a substantive prompt that should match", "session_id": "s1"})

    events_path = isolated_paths["cache_home"] / "advisor.events.jsonl"
    lines = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    assert lines[-1]["judge_used"] is True
    assert lines[-1]["judge_failure"] is None
```

No new imports needed — `json`, `patch`, `CatalogEntry`, `PickResult` and `ResolvedPick` are already at the top of the file.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_hook.py -q -k "judge_used or judge_failure"
```

Expected: FAIL — `assert True is False` on the first, `KeyError: 'judge_failure'` on the second, `TypeError: _fake_pick() got an unexpected keyword argument 'trace'` once the hook starts passing one.

- [ ] **Step 3: Add the `JudgeTrace` collector**

In `src/skill_advisor/matcher.py`, after the `PickResult` dataclass:

```python
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
```

- [ ] **Step 4: Thread it through the four call sites**

In `pick_stateless`, add `trace: JudgeTrace | None = None` as the last keyword parameter, and immediately before each of the three `return StatelessResult(...)` statements in the `use_judge` branch (and the final embedding-only return), record into it. The tidiest form — replace the whole `if use_judge:` block's returns with a single exit:

```python
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
    ...
    result = ...  # build StatelessResult exactly as Task 2 left it
    if trace is not None:
        trace.ran = result.judge_ran
        trace.failure = result.judge_failure
    return result
```

Concretely: keep the Task 2 body but assign each `StatelessResult(...)` to `result` and fall through to one shared trailer. **Both** early guards — `if idx is None:` and `if not cand_entries:` — return before the judge is reached, so they may return directly without touching the trace; a `JudgeTrace` starts at `ran=False, failure=None`, which is already the truth in those cases. Leaving them as bare early returns is correct, not an oversight.

In `_default_picks`, `_pick_inner`, and `pick`, add `trace: JudgeTrace | None = None` and pass it down:

```python
def _default_picks(prompt: str, cfg: Config, idx: index_mod.Index, trace: "JudgeTrace | None" = None) -> "StatelessResult":
    return pick_stateless(prompt, cfg, index=idx, trace=trace)
```

Every `_default_picks(x, cfg, idx)` call inside `_pick_inner` becomes `_default_picks(x, cfg, idx, trace)`. `_pick_inner(prompt, cfg, session_id)` becomes `_pick_inner(prompt, cfg, session_id, trace)`. And:

```python
def pick(
    prompt: str,
    config: Config | None = None,
    session_id: str | None = None,
    *,
    trace: JudgeTrace | None = None,
) -> PickResult | None:
    cfg = config or load_config()
    result = _pick_inner(prompt, cfg, session_id, trace)
    ...
```

- [ ] **Step 5: Add `judge_failure` to the telemetry event**

In `src/skill_advisor/telemetry.py`, add `judge_failure: str | None = None,` to `record()`'s keyword-only parameters (after `judge_used`), and next to the `"judge_used": bool(judge_used),` line in the event dict:

```python
        "judge_failure": str(judge_failure) if judge_failure else None,
```

- [ ] **Step 6: Wire the hook**

In `src/skill_advisor/hook.py`, replace line 128:

```python
        trace = matcher.JudgeTrace()
        result = matcher.pick(prompt, cfg, session_id=session_id, trace=trace)
```

Move `trace = matcher.JudgeTrace()` to *before* `signal.alarm(budget)` so it still exists if the alarm fires. Then replace line 243:

```python
                judge_used=trace.ran,
                judge_failure=trace.failure,
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src/skill_advisor/matcher.py src/skill_advisor/hook.py src/skill_advisor/telemetry.py tests/test_hook.py && uv run ruff format --check src/skill_advisor/matcher.py src/skill_advisor/hook.py src/skill_advisor/telemetry.py tests/test_hook.py
git add src/skill_advisor/matcher.py src/skill_advisor/hook.py src/skill_advisor/telemetry.py tests/test_hook.py
git commit -m "fix(telemetry): judge_used means the judge ran, not that it is configured"
```

---

### Task 4: Cut the budget and lock the two timeout layers in order

**Files:**
- Modify: `src/skill_advisor/config.py:16` (`budget_seconds`), `82` (comment), `88` (`judge_timeout_seconds`)
- Modify: `src/skill_advisor/install.py:219`, `297-301`
- Modify: `~/.config/skill-advisor/config.toml` (the live file — not in the repo)
- Test: `tests/test_config.py`, `tests/test_doctor_cli.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `config.MatcherConfig.budget_seconds == 8.0`, `config.ParallelizationConfig.judge_timeout_seconds == 5.0`. No new symbols.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_judge_subprocess_timeout_is_strictly_under_the_hook_alarm():
    """Two independent timeout layers guard the hot path:

      * judge.py:78    subprocess timeout = max(budget_seconds - 0.5, 0.5)
      * hook.py:133    SIGALRM            = int(budget_seconds + 0.5)

    The alarm aborts the whole hook and returns silent, bypassing the
    embedding fallback entirely. If it ever fires first, the fallback added in
    this plan is dead code. Pin the ordering.
    """
    from skill_advisor.config import Config

    budget = Config().matcher.budget_seconds
    judge_timeout = max(budget - 0.5, 0.5)
    hook_alarm = max(int(budget + 0.5), 1)
    assert judge_timeout < hook_alarm


def test_shipped_defaults_keep_doctor_quiet():
    """budget_seconds must cover judge_timeout_seconds + 3 at the shipped values,
    or a fresh install warns on its first `doctor` run."""
    from skill_advisor.config import Config

    cfg = Config()
    assert cfg.matcher.budget_seconds >= cfg.parallelization.judge_timeout_seconds + 3.0


def test_budget_default_is_eight_seconds():
    from skill_advisor.config import Config

    assert Config().matcher.budget_seconds == 8.0
    assert Config().parallelization.judge_timeout_seconds == 5.0
```

Append to `tests/test_doctor_cli.py`:

```python
def test_doctor_still_warns_when_budget_is_below_the_detector_timeout(isolated_paths, capsys):
    """The check must stay honest after the defaults move."""
    from skill_advisor import cli

    (isolated_paths["config_home"] / "config.toml").write_text(
        "[matcher]\nbudget_seconds = 4.0\n\n"
        "[parallelization]\nenabled = true\njudge_timeout_seconds = 5.0\n",
        encoding="utf-8",
    )
    try:
        cli.main(["doctor"])   # ends in sys.exit; tests/test_doctor_cli.py:23 idiom
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "budget_seconds" in out
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_config.py tests/test_doctor_cli.py -q
```

Expected: FAIL — `assert 4.0 == 8.0`, and `assert 4.0 >= 23.0` for the doctor-quiet test.

- [ ] **Step 3: Move the defaults**

In `src/skill_advisor/config.py`, `MatcherConfig`:

```python
    # Whole-hook budget. hook.py arms SIGALRM at int(budget_seconds + 0.5) around
    # the entire matcher; judge.py gives its subprocess budget_seconds - 0.5, so
    # the judge always loses the race and matcher.py's embedding fallback is
    # reachable. Measured 2026-07-29: the judge answers usefully under 15 s or
    # not at all (the >=24 s band produced 14 picks against 432 nothings), so a
    # tight budget forfeits almost nothing.
    budget_seconds: float = 8.0
```

`ParallelizationConfig` (replace both line 82's comment and line 88):

```python
    # Must stay <= matcher.budget_seconds - 3. The detector makes its own
    # `claude -p` call from inside the hook's SIGALRM window, so a timeout
    # longer than the budget means the alarm kills the whole hook — silent, no
    # picks at all — instead of the detector merely giving up. At 5.0 under an
    # 8 s budget the detector will often time out and return None; that
    # degrades gracefully (no parallel picks, lifecycle still advances) and is
    # the accepted trade for the budget cut.
    judge_timeout_seconds: float = 5.0
```

- [ ] **Step 4: Update the generated config template**

`src/skill_advisor/install.py:219`: `budget_seconds = 4.0` → `budget_seconds = 8.0`.

`src/skill_advisor/install.py:297-301`, replace the stale `>= 23` guidance:

```python
# default. Enabling requires matcher.budget_seconds >= judge_timeout_seconds + 3.
# judge_timeout_seconds = 5.0
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 6: Update the live config and re-measure the baseline**

The repo defaults do not affect this machine — `~/.config/skill-advisor/config.toml` overrides them with `budget_seconds = 25.0` and `judge_timeout_seconds = 20.0`. Edit it to:

```toml
[matcher]
budget_seconds = 8.0
use_judge = true

[parallelization]
enabled = true
min_tasks = 3
judge_timeout_seconds = 5.0
```

Then capture a clean before/after, because the spec's 261 ms embedding baseline is from 2026-04-28..05-19 against a since-changed catalog and is not a same-build comparison:

```bash
skill-advisor doctor
skill-advisor report --json > /tmp/latency-before.json
```

Record in the commit message: current p50, p95, and the count of `judge_used=true` events with zero picks. Re-run the same command after a week of use and compare.

- [ ] **Step 7: Commit**

```bash
uv run ruff check src/skill_advisor/config.py src/skill_advisor/install.py tests/test_config.py tests/test_doctor_cli.py && uv run ruff format --check src/skill_advisor/config.py src/skill_advisor/install.py tests/test_config.py tests/test_doctor_cli.py
git add src/skill_advisor/config.py src/skill_advisor/install.py tests/test_config.py tests/test_doctor_cli.py
git commit -m "perf: cut the hook budget to 8s and pin the timeout ordering"
```

---

### Task 5: Document the change

**Files:**
- Modify: `README.md` (the matcher/judge configuration section)
- Modify: `docs/superpowers/specs/2026-07-29-latency-design.md`

- [ ] **Step 1: Correct the spec's stale claims in place**

The spec is the record of the decision and currently contains three statements that were never true of the code. Edit it rather than leaving it to mislead the next reader:

1. Under "Fail fast, and never return empty-handed", change "Cut `budget_seconds` from 25 to ~8" to note that 25 was the author's local config and the shipped default was 4.0; both now read 8.0.
2. In the same section, add that `judge.rank()` collapsed six failure modes into `None` and that the fallback required a return-contract change first.
3. Add a line to "Failure modes": `hook.py`'s `SIGALRM` is a second, independent timeout layer that returns silent, and the judge's subprocess timeout must stay strictly below it.

Add at the top of the Design section:

```markdown
> **Status 2026-07-30:** change 2 is implemented — see
> `docs/superpowers/plans/2026-07-30-latency-fast-fail.md`. Change 1 (judge
> escalation) is deferred behind the catalog refresh, because that work shrinks
> the embedding index from 338 to ~93 entries and invalidates any confidence
> threshold calibrated before it. See
> `docs/superpowers/plans/2026-07-30-latency-judge-escalation.md`.
```

- [ ] **Step 2: Update the README**

In the matcher configuration section, state that `budget_seconds` is a whole-hook budget, that it must be at least `parallelization.judge_timeout_seconds + 3`, and that a judge timeout now yields embedding picks rather than silence.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/superpowers/specs/2026-07-29-latency-design.md
git commit -m "docs: record the fast-fail change and correct the latency spec's stale claims"
```

---

## Self-Review

**Spec coverage (change 2 only):** "Cut `budget_seconds` from 25 to ~8" → Task 4. "Fall back to embedding picks on timeout" → Tasks 1+2. "`doctor` check must stay honest" → Task 4 Step 1. "The fallback, driven by a real timeout" test → Task 2 Step 1. "The budget interaction" test → Task 4. "Latency has a regression test" → **partially deferred**: the wall-clock ceiling test belongs with the escalation work in the follow-on plan, since only there does an embedding-only path become the common case. Change 1 (escalation, calibration) is out of scope by the sequencing decision and lives in `2026-07-30-latency-judge-escalation.md`.

**Placeholder scan:** clean. Every code step carries real code; every test step carries a real assertion.

**Type consistency:** `JudgeResult.failure` (Task 1) is consumed as `raw.failure` (Task 2) and surfaced as `StatelessResult.judge_failure` (Task 2), collected by `JudgeTrace.failure` (Task 3), emitted as the event key `judge_failure` (Task 3). `judge_ran` / `JudgeTrace.ran` / `judge_used` are the same boolean under three names — deliberate, because the event-log key `judge_used` is already written on 8,536 historical rows and renaming it would split the field across two eras. (`cli._report_stats` does not read it, so nothing else constrains the name.)

**API references verified against the tree on 2026-07-30:** `_run_with_stdin` (`tests/test_hook.py:10`), `_enable_telemetry_in_config` (`tests/test_hook.py:63`), the `SIGALRM` watchdog (`hook.py:133`), the judge's subprocess timeout (`judge.py:78`), the doctor warning block (`cli.py:215-222`), and the generated config template (`install.py:219,297-301`).
