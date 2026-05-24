"""Windows hardware-cut smoke checks for skill-advisor.

Run with `uv run python scripts/windows_smoke.py`. Each check is a self-contained
function returning a `CheckResult`; `main()` runs them and exits non-zero if any
fail. Later tasks (SMOKE-001/002) extend this file with the full 6-check harness
(build, query, manifest count, …). This file currently carries the FE-VERIFY-001
pre-flight only.

## FE-VERIFY-001 — fastembed cache_dir pre-flight (LES-140: no building on a hunch)

Before any code wires the embedding cache to a fixed path (PATHS-001/INDEX-001),
we ask the *installed* library — not the docs, not memory — whether
`fastembed.TextEmbedding.__init__` actually accepts a `cache_dir` parameter.

Resolution of the cache path:
  - **Direct `cache_dir=` (default).** Verified present in fastembed 0.8.0
    (`uv.lock` pin). `index._embed_model()` passes `cache_dir=str(path)`.
  - **`HF_HOME` fallback.** If a future fastembed version drops the `cache_dir`
    kwarg, this pre-flight FAILS loudly here (before INDEX-001 runs). The
    documented fallback is to set the `HF_HOME` env var to the persistent cache
    dir before importing fastembed, which redirects the huggingface_hub download
    cache the model weights live in. We do not silently try/except the kwarg —
    a missing parameter must surface, not be swallowed.
"""
from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass

EXPECTED_FASTEMBED_VERSION = "0.8.0"  # uv.lock pin


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def preflight_fastembed_cache_dir() -> CheckResult:
    """Verify the installed fastembed exposes `TextEmbedding(cache_dir=...)`.

    Hard failure when the kwarg is absent — dependent code (INDEX-001) must not
    be written against an unverified signature. A version mismatch against the
    `uv.lock` pin is a warning, not a failure (the kwarg is what matters).
    """
    try:
        import fastembed
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        return CheckResult(
            name="FE-VERIFY-001",
            passed=False,
            detail=f"fastembed not importable: {exc}",
        )

    version = getattr(fastembed, "__version__", "unknown")
    params = inspect.signature(TextEmbedding.__init__).parameters
    has_cache_dir = "cache_dir" in params

    notes = [f"fastembed.__version__={version}"]
    if version != EXPECTED_FASTEMBED_VERSION:
        notes.append(
            f"WARNING: expected {EXPECTED_FASTEMBED_VERSION} (uv.lock pin), "
            f"got {version} — verify the kwarg still holds for this version"
        )

    if not has_cache_dir:
        notes.append(
            "FAIL: 'cache_dir' absent from TextEmbedding.__init__ — activate the "
            "HF_HOME fallback (set HF_HOME to the persistent cache dir before "
            "import) and do NOT wire cache_dir= in index._embed_model()"
        )
        return CheckResult(name="FE-VERIFY-001", passed=False, detail="; ".join(notes))

    notes.append("'cache_dir' present → pass cache_dir=str(path) directly")
    return CheckResult(name="FE-VERIFY-001", passed=True, detail="; ".join(notes))


def _all_checks() -> list[CheckResult]:
    # SMOKE-001/002 append their build/query/manifest checks to this list.
    return [preflight_fastembed_cache_dir()]


def main() -> int:
    results = _all_checks()
    failures = 0
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}: {r.detail}")
        if not r.passed:
            failures += 1
    print(f"\n{len(results) - failures}/{len(results)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
