"""Windows hardware-cut smoke checks for skill-advisor (SMOKE-002, CONTENT-Gate).

Run with `uv run python scripts/windows_smoke.py`. This is the verbindliche Windows
acceptance gate: it does NOT just prove the advisor starts without crashing — it
asserts the advisor works *correctly* (counts, real picks, prompt process exit).
It is a standalone runner (stdlib + project calls, **no pytest**), so it exercises
the real fastembed model on real Windows the way `pytest` mocks never can
(LES-140 / LES-157: build on a hardware cut, not on a hunch).

## The six smokes

1. **build** — `build` MIT `catalog_manifest` (Ben's curated manifest pattern):
   the catalog holds *exactly* 24 skills + 8 custom subagents + 4 custom commands
   + 18 builtins. Counts are asserted **per kind**, not just a sum or exit 0
   (LES-157). A control scan *without* the manifest yields 31 skills — proving the
   manifest is what brings 31→24 (INT-HA-001 Fehlalarm-Schutz): a green build smoke
   that forgot the manifest would silently report 31 and pass on a wrong premise.
2. **cache** — a second `build` after the OS temp dir is wiped does NOT
   re-download the BGE-small model: the persistent fastembed cache
   (`paths.fastembed_cache_dir()`, ARCH-ADR-002) survives temp cleanup. The model
   weights' mtime + size are unchanged across the second build (ARCH-RISK-002-Gate).
3. **hook** — two halves (CODE-RISK-003): a normal prompt over the *real* fastembed
   index yields **non-empty** routing picks in `additionalContext`; a worker that
   cannot finish inside the budget yields **empty** output, exit 0, and a process
   that returns **far below the worker's running time** (HOOK-001 / ADR-001: the
   daemon-thread join-timeout bounds the wait on Windows, where the old
   `signal.SIGALRM` was a no-op). See `_HOOK_TIMEOUT_RUNNER` for why the timeout
   half drives the worker with a controlled sleep rather than real fastembed load.
4. **install** — on Windows with no Unix `$SHELL`, `install --write-alias` writes a
   `claudeskill` *function* into the PowerShell profile; running it twice leaves
   exactly one copy (idempotent).
5. **match** — a typical prompt returns plausible, non-empty top picks.
6. **doctor** — exits 0 on a healthy install.

## FE-VERIFY-001 — fastembed cache_dir pre-flight (folded into the cache smoke)

Before trusting that the model cache is pinned, we ask the *installed* fastembed —
not the docs, not memory — whether `TextEmbedding.__init__` actually accepts a
`cache_dir` kwarg (verified present in fastembed 0.8.0, the `uv.lock` pin). If a
future version drops it, the cache smoke FAILS loudly here instead of silently
re-downloading into the OS temp; the documented fallback is to set `HF_HOME` to the
persistent cache dir before importing fastembed. We never silently swallow a missing
kwarg.
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

EXPECTED_FASTEMBED_VERSION = "0.8.0"  # uv.lock pin

# --- expected content-gate counts (per kind) --------------------------------
# These are reproduced deterministically by the synthetic fixture below so the
# gate is CI-portable and never coupled to whatever the live ~/.claude happens to
# hold (CODE-RISK-002). 31 synthetic skills minus a 7-entry manifest exclusion = 24.
EXPECTED_SKILLS = 24
EXPECTED_SKILLS_NO_MANIFEST = 31
EXPECTED_CUSTOM_SUBAGENTS = 8
EXPECTED_CUSTOM_COMMANDS = 4
EXPECTED_BUILTINS = 18  # 10 builtin subagents + 8 builtin commands (builtins.py)

# Skills 1-8 carry realistic, semantically distinct descriptions so the embedding
# matcher (hook/match smokes) produces meaningful picks. The rest are filler; the
# last 7 (25-31) are what the manifest excludes.
_REAL_SKILLS = [
    ("pdf-extract", "Use when the user wants to extract text, tables, or invoice data from a PDF document."),
    ("git-commit", "Create a semantic git commit following Conventional Commits from the staged changes."),
    ("test-runner", "Run the project's unit and end-to-end tests and report which ones fail."),
    ("excel-analyze", "Read and analyze an Excel spreadsheet: sheets, cell ranges, and formulas."),
    ("deep-research", "Perform deep internet research and fact-checking on a topic with web search."),
    ("frontend-design", "Build production-grade frontend UI components, pages, and web apps with good design."),
    ("pc-monitor", "Check system performance: CPU, RAM, disk usage, and hung or runaway processes."),
    ("email-send", "Read, search, and send Outlook email from the command line."),
]

# A prompt that should land a strong embedding match against one of the real skills.
HOOK_PROMPT = "extract the invoice line items and totals from this PDF file"
MATCH_PROMPT = "write a conventional commit message for my staged changes"

# Timeout half of the hook smoke. We drive the worker with a long sleep instead of
# real fastembed load for two empirically-verified reasons (measured on Windows,
# fastembed 0.8.0):
#   * Real *warm* fastembed work (~0.5-0.65 s) straddles the hook's 0.5 s budget
#     floor (`max(budget, 0.5)`), so it can neither reliably finish nor reliably
#     time out — the branch is a coin-flip, useless as a gate.
#   * Forcing a long *real* load via an empty model cache makes the worker download
#     ~66 MB through huggingface_hub's non-daemon thread pool, which blocks
#     interpreter shutdown anyway — the delay is then huggingface's, not the hook's.
# A sleeping worker is the canonical way to exercise the budget-exceeded branch (it
# is exactly what `tests/test_hook.py::test_hook_silent_on_budget_timeout` does) and
# its GIL-releasing nature matches the real fastembed/numpy work ADR-001 reasons
# about. The smoke's *real-load* coverage lives in the first half (real picks over
# the real index), so this is not "relying only on a mock".
#
# Note on the assertion: the architecture's aspirational "<0.2 s process exit" does
# not survive CPython's daemon-thread finalization — the process teardown caps at a
# few seconds regardless of worker length. What is robust and regression-catching is
# that the process returns *far below* the worker's runtime (a correct daemon-timeout
# exits in a few seconds for a 20 s worker; a joining / no-timeout regression would
# block the full 20 s).
_HOOK_TIMEOUT_WORKER_SLEEP = 20
_HOOK_TIMEOUT_MAX_EXIT_SECONDS = 12.0  # between the ~4 s correct tail and the 20 s worker
_HOOK_TIMEOUT_RUNNER = (
    "import time, skill_advisor.matcher as m, skill_advisor.hook as h\n"
    f"m.pick = lambda *a, **k: (time.sleep({_HOOK_TIMEOUT_WORKER_SLEEP}), None)[1]\n"
    "raise SystemExit(h.run())\n"
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass
class SmokeEnv:
    """A self-contained, gitignored sandbox under the repo's .cache/.

    Everything the smokes touch is redirected here through the framework's own
    env-override seams (CLAUDE_HOME, SKILL_ADVISOR_*_HOME, HOME/USERPROFILE, TMP),
    so a run never reads or writes the real ~/.claude or ~/.cache (ARCH-RISK-006).
    """

    root: Path
    claude_home: Path
    home: Path
    config_home: Path
    cache_home: Path
    fastembed_cache: Path
    tmp: Path
    bin: Path
    manifest_path: Path
    base_env: dict = field(default_factory=dict)

    def child_env(self, **overrides: str) -> dict:
        env = dict(self.base_env)
        env.update({str(k): str(v) for k, v in overrides.items()})
        return env


def _write_skill(skills_root: Path, name: str, description: str) -> None:
    d = skills_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: \"{description}\"\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _write_md(path: Path, *, name: str | None, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = "---\n"
    if name is not None:
        fm += f"name: {name}\n"
    fm += f"description: \"{description}\"\n---\n\n# def\n"
    path.write_text(fm, encoding="utf-8")


def _build_fixture(env: SmokeEnv) -> None:
    """Populate CLAUDE_HOME with a deterministic 31-skill / 8-agent / 4-command tree."""
    skills_root = env.claude_home / "skills"
    # 8 realistic + 17 filler = 25, then 6 more filler = 31 total. The last 7
    # (skill-25..skill-31) are the manifest's exclusion set.
    for slug, desc in _REAL_SKILLS:
        _write_skill(skills_root, slug, desc)
    for i in range(9, 32):  # skill-09 .. skill-31 (23 filler), total 8 + 23 = 31
        _write_skill(skills_root, f"skill-{i:02d}", f"Utility skill number {i} for catalog-count testing.")

    # 8 custom subagents (namespace=user).
    agents_root = env.claude_home / "agents"
    for i in range(1, 9):
        _write_md(agents_root / f"agent-{i}.md", name=f"agent-{i}",
                  description=f"Custom subagent number {i} used for routing tests.")
    # Helper subtree — structurally excluded by catalog._REVIEWERS_SUBDIR, must NOT count.
    for i in range(1, 3):
        _write_md(agents_root / "reviewers" / f"rev-{i}.md", name=f"rev-{i}",
                  description=f"Reviewer helper {i}; should never appear in the catalog.")

    # 4 custom slash-commands (name derived from file stem -> /cmd-N).
    commands_root = env.claude_home / "commands"
    for i in range(1, 5):
        _write_md(commands_root / f"cmd-{i}.md", name=None,
                  description=f"Custom slash command number {i} for routing tests.")

    # The manifest excludes exactly the 7 filler skills 25..31 -> 31 - 7 = 24.
    excluded = [f"skill-{i:02d}" for i in range(25, 32)]
    env.manifest_path.write_text(
        json.dumps({"schema_version": 1, "excluded_subskills": excluded}, indent=2),
        encoding="utf-8",
    )


def _write_config(config_home: Path, *, manifest: str, budget: float, min_score: float = 0.25) -> Path:
    """Write a config.toml under a SKILL_ADVISOR_CONFIG_HOME and return its path."""
    cfg_dir = config_home / "skill-advisor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.toml"
    manifest_line = f'catalog_manifest = "{manifest}"' if manifest else 'catalog_manifest = ""'
    cfg.write_text(
        "[matcher]\n"
        "use_judge = false\n"
        f"budget_seconds = {budget}\n"
        f"min_embedding_score = {min_score}\n"
        "max_picks = 3\n"
        "[catalog]\n"
        f"{manifest_line}\n",
        encoding="utf-8",
    )
    return cfg


def _make_fake_claude(bin_dir: Path) -> None:
    """A no-op `claude` shim so `shutil.which('claude')` resolves (doctor exit 0)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Windows resolves claude.cmd via PATHEXT; the POSIX file is harmless ballast.
    (bin_dir / "claude.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    posix = bin_dir / "claude"
    posix.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    try:
        posix.chmod(0o755)
    except OSError:
        pass


def make_env() -> SmokeEnv:
    # Sandbox lives under the repo's .cache/ (gitignored) so a run never escapes the
    # checkout and `git status` stays clean.
    repo_root = Path(__file__).resolve().parent.parent
    base = repo_root / ".cache"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="smoke-", dir=str(base)))

    env = SmokeEnv(
        root=root,
        claude_home=root / "claude",
        home=root / "home",
        config_home=root / "config",
        cache_home=root / "cache",
        fastembed_cache=root / "fastembed",
        tmp=root / "tmp",
        bin=root / "bin",
        manifest_path=root / "manifest.json",
    )
    for d in (env.claude_home, env.home, env.config_home, env.cache_home,
              env.fastembed_cache, env.tmp, env.bin):
        d.mkdir(parents=True, exist_ok=True)

    _build_fixture(env)
    _make_fake_claude(env.bin)
    _write_config(env.config_home, manifest=str(env.manifest_path), budget=4.0)

    base_env = dict(os.environ)
    # Redirect every path seam into the sandbox.
    base_env.update({
        "CLAUDE_HOME": str(env.claude_home),
        "HOME": str(env.home),
        "USERPROFILE": str(env.home),
        "SKILL_ADVISOR_CONFIG_HOME": str(env.config_home),
        "SKILL_ADVISOR_CACHE_HOME": str(env.cache_home),
        "SKILL_ADVISOR_FASTEMBED_CACHE": str(env.fastembed_cache),
        "TMP": str(env.tmp),
        "TEMP": str(env.tmp),
        "TMPDIR": str(env.tmp),
        "PATH": str(env.bin) + os.pathsep + os.environ.get("PATH", ""),
    })
    # A stray Unix $SHELL (git-bash etc.) would push detect_shell() off the
    # PowerShell branch; the install smoke needs the Windows path.
    base_env.pop("SHELL", None)
    base_env.pop("CLAUDE_CONFIG_DIR", None)
    env.base_env = base_env

    # Mirror onto this process so in-process scan/build calls see the same paths.
    os.environ.update(base_env)
    os.environ.pop("SHELL", None)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    return env


def _run_cli(args: list[str], env: dict, *, stdin: str | None = None,
             timeout: float = 120.0) -> tuple[subprocess.CompletedProcess, float]:
    """Invoke the installed CLI in a fresh process; return (result, wall_seconds)."""
    cmd = [sys.executable, "-m", "skill_advisor.cli", *args]
    started = time.monotonic()
    proc = subprocess.run(
        cmd, input=stdin, env=env, capture_output=True, text=True, timeout=timeout,
    )
    return proc, time.monotonic() - started


# ---------------------------------------------------------------------------
# Pre-flight (FE-VERIFY-001)
# ---------------------------------------------------------------------------


def preflight_fastembed_cache_dir() -> tuple[bool, str]:
    """Verify the installed fastembed exposes `TextEmbedding(cache_dir=...)`."""
    try:
        import fastembed
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - environment problem
        return False, f"fastembed not importable: {exc}"

    version = getattr(fastembed, "__version__", "unknown")
    has_cache_dir = "cache_dir" in inspect.signature(TextEmbedding.__init__).parameters
    notes = [f"fastembed=={version}"]
    if version != EXPECTED_FASTEMBED_VERSION:
        notes.append(f"WARNING: expected {EXPECTED_FASTEMBED_VERSION} (uv.lock pin)")
    if not has_cache_dir:
        notes.append("FAIL: 'cache_dir' kwarg absent — activate HF_HOME fallback before INDEX-001")
        return False, "; ".join(notes)
    notes.append("'cache_dir' present")
    return True, "; ".join(notes)


# ---------------------------------------------------------------------------
# Smoke 1 — build + per-kind content gate
# ---------------------------------------------------------------------------


def _count_by(entries, kind: str, *, builtin: bool | None = None) -> int:
    n = 0
    for e in entries:
        if e.kind != kind:
            continue
        if builtin is True and e.namespace != "builtin":
            continue
        if builtin is False and e.namespace == "builtin":
            continue
        n += 1
    return n


def smoke_build(env: SmokeEnv) -> CheckResult:
    """Real build, then assert per-kind counts with AND without the manifest."""
    from skill_advisor import catalog as catalog_mod
    from skill_advisor import index as index_mod
    from skill_advisor.config import Config, CatalogConfig, MatcherConfig

    cfg_with = Config(
        matcher=MatcherConfig(min_embedding_score=0.25),
        catalog=CatalogConfig(catalog_manifest=str(env.manifest_path)),
    )
    cfg_without = Config(catalog=CatalogConfig(catalog_manifest=""))

    entries_with = catalog_mod.scan(cfg_with)
    entries_without = catalog_mod.scan(cfg_without)

    skills = _count_by(entries_with, "skill")
    skills_nomanifest = _count_by(entries_without, "skill")
    custom_subagents = _count_by(entries_with, "subagent", builtin=False)
    custom_commands = _count_by(entries_with, "command", builtin=False)
    builtins = sum(1 for e in entries_with if e.namespace == "builtin")
    builtin_subagents = _count_by(entries_with, "subagent", builtin=True)

    problems: list[str] = []
    if skills != EXPECTED_SKILLS:
        problems.append(f"skills={skills} (want {EXPECTED_SKILLS})")
    if skills_nomanifest != EXPECTED_SKILLS_NO_MANIFEST:
        problems.append(
            f"skills-without-manifest={skills_nomanifest} (want {EXPECTED_SKILLS_NO_MANIFEST}); "
            "manifest gate not proven"
        )
    if custom_subagents != EXPECTED_CUSTOM_SUBAGENTS:
        problems.append(f"custom subagents={custom_subagents} (want {EXPECTED_CUSTOM_SUBAGENTS})")
    if custom_commands != EXPECTED_CUSTOM_COMMANDS:
        problems.append(f"custom commands={custom_commands} (want {EXPECTED_CUSTOM_COMMANDS})")
    if builtins != EXPECTED_BUILTINS:
        problems.append(f"builtins={builtins} (want {EXPECTED_BUILTINS}, of which {builtin_subagents} subagents)")

    # Actually build the index (real fastembed) so dependent smokes have an index.
    source_hash = catalog_mod.compute_hash(entries_with)
    embeddings = index_mod.build(entries_with)
    index_mod.save(entries_with, embeddings, source_hash)
    if embeddings.shape[0] != len(entries_with):
        problems.append(f"embedding rows={embeddings.shape[0]} != entries={len(entries_with)}")

    detail = (
        f"24/8/4/18 gate: skills={skills}, custom_subagents={custom_subagents}, "
        f"custom_commands={custom_commands}, builtins={builtins} "
        f"(no-manifest skills={skills_nomanifest}); built {len(entries_with)} embeddings"
    )
    if problems:
        return CheckResult("build", False, "; ".join(problems))
    return CheckResult("build", True, detail)


# ---------------------------------------------------------------------------
# Smoke 2 — persistent fastembed cache survives temp cleanup
# ---------------------------------------------------------------------------


def _largest_file(root: Path) -> Path | None:
    best: Path | None = None
    best_size = -1
    for p in root.rglob("*"):
        if p.is_file():
            sz = p.stat().st_size
            if sz > best_size:
                best, best_size = p, sz
    return best


def smoke_cache(env: SmokeEnv) -> CheckResult:
    """Second build after wiping %TEMP% must NOT re-download the model (ARCH-RISK-002)."""
    ok, note = preflight_fastembed_cache_dir()
    if not ok:
        return CheckResult("cache", False, f"FE-VERIFY-001 pre-flight failed: {note}")

    from skill_advisor import catalog as catalog_mod
    from skill_advisor import index as index_mod
    from skill_advisor.config import Config, CatalogConfig, MatcherConfig

    cfg = Config(
        matcher=MatcherConfig(min_embedding_score=0.25),
        catalog=CatalogConfig(catalog_manifest=str(env.manifest_path)),
    )
    entries = catalog_mod.scan(cfg)

    # First build already happened in smoke_build, but be self-contained: ensure
    # the model is cached now.
    index_mod.build(entries[:1] or entries)

    model_file = _largest_file(env.fastembed_cache)
    if model_file is None:
        return CheckResult("cache", False,
                           f"no model file found under {env.fastembed_cache} after build")
    before = model_file.stat()
    before_mtime, before_size = before.st_mtime_ns, before.st_size

    # Simulate OS temp cleanup: wipe the controlled temp dir (never the real %TEMP%).
    for child in env.tmp.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass

    # Second build: must reuse the cached weights, not re-fetch them.
    index_mod.build(entries)

    if not model_file.is_file():
        return CheckResult("cache", False,
                           f"model file {model_file.name} vanished after temp wipe + rebuild")
    after = model_file.stat()
    if after.st_mtime_ns != before_mtime or after.st_size != before_size:
        return CheckResult(
            "cache", False,
            f"model re-downloaded: {model_file.name} mtime/size changed "
            f"({before_mtime}/{before_size} -> {after.st_mtime_ns}/{after.st_size})",
        )
    return CheckResult(
        "cache", True,
        f"{note}; 2nd build after temp wipe reused {model_file.name} "
        f"({before_size} bytes, mtime stable)",
    )


# ---------------------------------------------------------------------------
# Smoke 3 — hook: real picks + prompt silent exit at near-zero budget
# ---------------------------------------------------------------------------


def smoke_hook(env: SmokeEnv) -> CheckResult:
    # (a) Normal prompt + full budget -> non-empty additionalContext.
    full_env = env.child_env()
    payload = json.dumps({"prompt": HOOK_PROMPT, "session_id": "smoke-hook"})
    proc_full, t_full = _run_cli(["hook"], full_env, stdin=payload, timeout=60.0)
    if proc_full.returncode != 0:
        return CheckResult("hook", False,
                           f"normal-prompt hook exit {proc_full.returncode}: {proc_full.stderr[:200]}")
    out = (proc_full.stdout or "").strip()
    if not out:
        return CheckResult("hook", False, "normal-prompt hook emitted no additionalContext (expected picks)")
    try:
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return CheckResult("hook", False, f"normal-prompt hook output not a valid envelope: {exc}: {out[:200]}")
    if not ctx.strip():
        return CheckResult("hook", False, "normal-prompt hook additionalContext was empty")

    # (b) Worker exceeds budget -> empty output, exit 0, process returns far below
    # the worker's runtime (HOOK-001 daemon-thread timeout). budget≈0 -> 0.5 s floor.
    low_cfg_home = env.root / "config-lowbudget"
    _write_config(low_cfg_home, manifest=str(env.manifest_path), budget=0.01)
    low_env = env.child_env(SKILL_ADVISOR_CONFIG_HOME=str(low_cfg_home))

    started = time.monotonic()
    proc_low = subprocess.run(
        [sys.executable, "-c", _HOOK_TIMEOUT_RUNNER],
        input=json.dumps({"prompt": HOOK_PROMPT, "session_id": "smoke-budget"}),
        env=low_env, capture_output=True, text=True,
        timeout=_HOOK_TIMEOUT_WORKER_SLEEP + 15,
    )
    t_low = time.monotonic() - started

    if proc_low.returncode != 0:
        return CheckResult("hook", False, f"budget≈0 hook exit {proc_low.returncode} (want 0)")
    if (proc_low.stdout or "").strip():
        return CheckResult("hook", False,
                           f"budget≈0 hook should emit nothing, got: {proc_low.stdout[:200]}")
    if t_low >= _HOOK_TIMEOUT_MAX_EXIT_SECONDS:
        return CheckResult(
            "hook", False,
            f"budget≈0 hook waited {t_low:.2f}s for a {_HOOK_TIMEOUT_WORKER_SLEEP}s worker "
            f"(>= {_HOOK_TIMEOUT_MAX_EXIT_SECONDS}s) — the budget timeout did not fire; "
            f"the worker is likely joined instead of abandoned",
        )
    return CheckResult(
        "hook", True,
        f"normal prompt picked '{ctx.strip().splitlines()[0][:60]}...' (t={t_full:.2f}s); "
        f"budget≈0 -> empty + exit0, returned in {t_low:.2f}s vs {_HOOK_TIMEOUT_WORKER_SLEEP}s worker",
    )


# ---------------------------------------------------------------------------
# Smoke 4 — install writes an idempotent PowerShell claudeskill function
# ---------------------------------------------------------------------------


def smoke_install(env: SmokeEnv) -> CheckResult:
    if os.name != "nt":
        return CheckResult("install", True, "skipped: not Windows (PowerShell-profile path is Windows-only)")

    install_home = env.root / "install-home"
    install_cfg = env.root / "install-config"
    install_home.mkdir(parents=True, exist_ok=True)
    install_cfg.mkdir(parents=True, exist_ok=True)
    inst_env = env.child_env(
        USERPROFILE=str(install_home),
        HOME=str(install_home),
        SKILL_ADVISOR_CONFIG_HOME=str(install_cfg),
        CLAUDE_HOME=str(install_home / ".claude"),
    )
    inst_env.pop("SHELL", None)  # force the Windows PowerShell branch in detect_shell()

    profile = install_home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"

    for run_no in (1, 2):
        proc, _ = _run_cli(["install", "--no-build", "--write-alias"], inst_env, timeout=60.0)
        if proc.returncode != 0:
            return CheckResult("install", False,
                               f"install run {run_no} exit {proc.returncode}: {proc.stderr[:200]}")

    if not profile.is_file():
        return CheckResult("install", False, f"PowerShell profile not written at {profile}")
    text = profile.read_text(encoding="utf-8")
    count = text.count("function claudeskill")
    if count != 1:
        return CheckResult("install", False,
                           f"expected exactly one `function claudeskill` after 2 installs, found {count}")
    return CheckResult("install", True,
                       f"`function claudeskill` present once in {profile.name} after 2 idempotent installs")


# ---------------------------------------------------------------------------
# Smoke 5 — match returns non-empty top picks
# ---------------------------------------------------------------------------


def smoke_match(env: SmokeEnv) -> CheckResult:
    proc, _ = _run_cli(["match", "--json", MATCH_PROMPT], env.child_env(), timeout=60.0)
    if proc.returncode != 0:
        return CheckResult("match", False, f"match exit {proc.returncode}: {proc.stderr[:200]}")
    try:
        picks = json.loads(proc.stdout)["picks"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return CheckResult("match", False, f"match JSON unparseable: {exc}: {proc.stdout[:200]}")
    if not picks:
        return CheckResult("match", False, "match returned zero picks for a typical prompt")
    top = picks[0]
    return CheckResult("match", True,
                       f"top pick {top.get('kind')}:{top.get('name')} (+{len(picks) - 1} more)")


# ---------------------------------------------------------------------------
# Smoke 6 — doctor exits 0 on a healthy install
# ---------------------------------------------------------------------------


def smoke_doctor(env: SmokeEnv) -> CheckResult:
    proc, _ = _run_cli(["doctor"], env.child_env(), timeout=60.0)
    if proc.returncode != 0:
        return CheckResult("doctor", False,
                           f"doctor exit {proc.returncode} (want 0):\n{proc.stdout[-300:]}\n{proc.stderr[:200]}")
    return CheckResult("doctor", True, "exit 0 on healthy install (claude shim + built catalog/embeddings)")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_SMOKES = (
    ("build", smoke_build),
    ("cache", smoke_cache),
    ("hook", smoke_hook),
    ("install", smoke_install),
    ("match", smoke_match),
    ("doctor", smoke_doctor),
)


def run_all() -> list[CheckResult]:
    env = make_env()
    results: list[CheckResult] = []
    try:
        for name, fn in _SMOKES:
            try:
                results.append(fn(env))
            except Exception as exc:  # a smoke crash is a failure, not a runner crash
                import traceback
                results.append(CheckResult(name, False, f"crashed: {exc}\n{traceback.format_exc()}"))
    finally:
        shutil.rmtree(env.root, ignore_errors=True)
    return results


def main() -> int:
    results = run_all()
    failures = 0
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}: {r.detail}")
        if not r.passed:
            failures += 1
    print(f"\n{len(results) - failures}/{len(results)} smokes passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
