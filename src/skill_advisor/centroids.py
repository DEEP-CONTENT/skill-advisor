"""Fixed-size online sketch of the user's prompt distribution.

Rotation needs to score a skill that has never been used, which usage data
cannot do. The escape is that relevance does not require usage: every skill
already has an embedding, so its fit can be measured against a sketch of the
work the user actually does.

Eight centroids rather than one because the work is multi-modal — Kubernetes,
frontend, Python services and documentation occupy different regions of
embedding space, and a single mean would sit between all of them describing
none.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np

from . import paths

log = logging.getLogger(__name__)

DIM = 384
DEFAULT_K = 8

# A prompt within this cosine of an existing centroid is already represented, so
# it nudges that centroid instead of burning one of only K slots on a near
# duplicate. Without this guard, seeding claims one slot per prompt until all K
# are gone — two IDENTICAL prompts take two slots — and a user whose first eight
# prompts are similar ends up with eight copies of the same region and no
# capacity left for the others. That is the exact failure the K>1 design exists
# to avoid. Verified by `test_identical_prompts_do_not_each_claim_a_slot`.
SEED_SIMILARITY = 0.9


@dataclass
class Sketch:
    vectors: np.ndarray  # (K, DIM) float32, L2-normalised; all-zero row = unclaimed
    counts: np.ndarray  # (K,) int64
    observed: int  # total prompts folded in


def empty(k: int = DEFAULT_K) -> Sketch:
    return Sketch(
        vectors=np.zeros((k, DIM), dtype=np.float32),
        counts=np.zeros(k, dtype=np.int64),
        observed=0,
    )


def load(k: int = DEFAULT_K) -> Sketch:
    """Never raises. A missing or wrong-shaped file degrades to an empty sketch
    of `k` centroids, which suppresses semantic_fit and makes rotation refuse
    to run.

    `k` only matters for a COLD START (no file yet, or an unreadable one) —
    once a valid sketch exists on disk, its own shape wins regardless of `k`;
    the centroid count is fixed at creation time, same as `min_observed_prompts`
    and every other cold-start-only setting. Callers pass
    `cfg.rotation.centroid_count` so a configured value actually takes effect
    instead of always seeding `DEFAULT_K`.
    """
    f = paths.centroids_file()
    if not f.is_file():
        return empty(k)
    try:
        with np.load(f) as data:
            vectors = data["vectors"].astype(np.float32, copy=False)
            counts = data["counts"].astype(np.int64, copy=False)
            observed = int(data["observed"])
    except Exception as exc:
        log.warning("centroids unreadable (%s); starting empty", exc)
        return empty(k)
    if (
        vectors.ndim != 2
        or vectors.shape[1] != DIM
        or counts.shape != (vectors.shape[0],)
    ):
        log.warning("centroids wrong shape %s; starting empty", vectors.shape)
        return empty(k)
    return Sketch(vectors=vectors, counts=counts, observed=observed)


def save(sketch: Sketch) -> bool:
    """Write `sketch` to disk atomically: temp file, then `Path.replace()`.

    `np.savez()` writes straight to the file it's given, with no atomicity of
    its own — a crash mid-write (killed process, disk full) leaves a
    truncated, corrupt `.npz` sitting at the real path. Every other writer in
    this codebase (`overrides.write`/`remove_keys`, `_write_config_toml`)
    already goes through a `.tmp.<pid>` file + atomic replace; this matches
    that. The temp file is opened ourselves and handed to `np.savez` as a
    file object rather than a path, because `np.savez` silently appends
    `.npz` to any path argument that doesn't already end in `.npz` — passing
    it `centroids.npz.tmp.<pid>` as a *path* would actually write to
    `centroids.npz.tmp.<pid>.npz`, breaking the rename below.
    """
    target = paths.centroids_file()
    tmp = target.with_suffix(f".npz.tmp.{os.getpid()}")
    try:
        paths.ensure_dirs()
        with tmp.open("wb") as f:
            np.savez(
                f,
                vectors=sketch.vectors,
                counts=sketch.counts,
                observed=np.int64(sketch.observed),
            )
        tmp.replace(target)
        return True
    except OSError as exc:
        log.debug("centroids save failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def observe(sketch: Sketch, vec: np.ndarray) -> None:
    """Fold one prompt embedding in, mutating `sketch`.

    A prompt that no existing centroid covers claims a free slot, so the K
    regions spread out instead of collapsing into one blob. A prompt that IS
    covered nudges its nearest centroid rather than consuming a slot — see
    SEED_SIMILARITY for why that guard is load-bearing.
    """
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return
    v = v / n

    sketch.observed += 1
    claimed = sketch.counts > 0
    sims = sketch.vectors @ v
    best_sim = float(sims[claimed].max()) if claimed.any() else -1.0

    unclaimed = np.flatnonzero(~claimed)
    if unclaimed.size and best_sim < SEED_SIMILARITY:
        i = int(unclaimed[0])
        sketch.vectors[i] = v
        sketch.counts[i] = 1
        return

    # Restrict argmax to claimed slots: an unclaimed row is all zeros and scores
    # cosine 0, which would beat a genuinely-nearest centroid on a prompt that
    # happens to sit at a negative cosine to it. This only matters if SEED_SIMILARITY
    # is retuned downward; it is forward-defense for future maintainers.
    masked = np.where(claimed, sims, -np.inf)
    i = int(np.argmax(masked))
    sketch.counts[i] += 1
    sketch.vectors[i] += (v - sketch.vectors[i]) / float(sketch.counts[i])
    norm = float(np.linalg.norm(sketch.vectors[i]))
    if norm:
        sketch.vectors[i] /= norm


def fit(
    sketch: Sketch, embeddings: np.ndarray, *, min_observed: int
) -> np.ndarray | None:
    """max_i cosine(embedding, centroid_i) per row, or None before cold start."""
    if sketch.observed < min_observed:
        return None
    claimed = sketch.counts > 0
    if not claimed.any() or embeddings.size == 0:
        return None
    sims = np.asarray(embeddings, dtype=np.float32) @ sketch.vectors[claimed].T
    return sims.max(axis=1)
