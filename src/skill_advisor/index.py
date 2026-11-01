"""Local embedding index over the catalog.

Uses fastembed's BGE-small (384-dim, ~15 MB, CPU-only). No API keys.
Cold load off disk (mmap'd npz + JSON) is ~20 ms on SSD.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from . import catalog as catalog_mod
from . import paths
from .catalog import CatalogEntry

_MODEL_NAME = "BAAI/bge-small-en-v1.5"


@dataclass
class Index:
    catalog: list[CatalogEntry]
    embeddings: np.ndarray  # shape (N, D), float32, L2-normalised
    # Bool mask of shape (N,) — True where the entry is invocable in Claude Code.
    # The index deliberately holds disabled entries too: the rotation pool needs
    # their embeddings to score a skill that has never been used.
    pickable: np.ndarray | None = None


@lru_cache(maxsize=1)
def _embed_model():
    # Lazy import — fastembed import is slow (~500 ms) and we don't want it on triage-skip paths.
    # Cached at process scope (lru_cache, not a warm-model claim): a single hook
    # invocation is one short-lived process that can call this more than once —
    # e.g. top_k() during matching, then embed_one() again for the centroid
    # sketch — and constructing TextEmbedding, not running inference, is the
    # expensive part. Caching removes that duplicate construction; it does not
    # make any one call fast, and does not remove the need for the SIGALRM
    # budget around callers of embed_one()/top_k().
    from fastembed import TextEmbedding

    # Pin the model weights to our XDG cache (FE-VERIFY-001 confirmed the kwarg on
    # fastembed 0.8.0) so the ~15 MB BGE-small ONNX survives temp cleanup instead
    # of re-downloading on every cold start. fastembed creates the dir on demand.
    cache_dir = paths.fastembed_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(model_name=_MODEL_NAME, cache_dir=str(cache_dir))


def _normalise(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype(np.float32)


def build(catalog: list[CatalogEntry]) -> np.ndarray:
    if not catalog:
        return np.zeros((0, 384), dtype=np.float32)
    model = _embed_model()
    texts = [e.embed_text() for e in catalog]
    vectors = np.array(list(model.embed(texts)), dtype=np.float32)
    return _normalise(vectors)


def save(catalog: list[CatalogEntry], embeddings: np.ndarray, source_hash: str) -> None:
    paths.ensure_dirs()
    catalog_mod.save(catalog)
    np.savez(paths.embeddings_file(), embeddings=embeddings)
    paths.catalog_hash_file().write_text(source_hash, encoding="utf-8")


def load() -> Index:
    catalog = catalog_mod.load()
    if not paths.embeddings_file().is_file():
        raise FileNotFoundError(
            f"embeddings not built yet; expected {paths.embeddings_file()}. "
            "Run `skill-advisor build`."
        )
    with np.load(paths.embeddings_file()) as data:
        embeddings = data["embeddings"].astype(np.float32, copy=False)
    if embeddings.shape[0] != len(catalog):
        raise ValueError(
            f"catalog/embedding size mismatch ({len(catalog)} vs {embeddings.shape[0]}); "
            "rerun `skill-advisor build`."
        )
    mask = np.array([e.enabled for e in catalog], dtype=bool)
    return Index(catalog=catalog, embeddings=embeddings, pickable=mask)


def current_hash() -> str | None:
    f = paths.catalog_hash_file()
    if not f.is_file():
        return None
    return f.read_text(encoding="utf-8").strip()


def embed_one(text: str) -> np.ndarray:
    """A single L2-normalised query vector."""
    model = _embed_model()
    q = np.array(list(model.embed([text])), dtype=np.float32)
    return _normalise(q)[0]


def _rank_prescored(
    q: np.ndarray, index: Index, k: int, pickable_only: bool
) -> list[tuple[CatalogEntry, float]]:
    """Cosine-rank `index` against an already-embedded query `q`.

    Split out of `top_k` so `embed_and_rank` can hand back the query vector
    it built without embedding the same prompt a second time.
    """
    scores = index.embeddings @ q  # cosine because both sides are unit vectors

    if pickable_only and index.pickable is not None:
        # Mask BEFORE selection. Masking after would let a disabled top-scorer
        # consume one of the K slots and silently shorten the shortlist.
        if not index.pickable.any():
            return []
        scores = np.where(index.pickable, scores, -np.inf)

    k = min(k, int(np.isfinite(scores).sum()) if pickable_only else scores.shape[0])
    if k <= 0:
        return []
    top_idx = np.argpartition(-scores, k - 1)[:k]
    top_idx = top_idx[np.argsort(-scores[top_idx])]
    return [(index.catalog[i], float(scores[i])) for i in top_idx]


def top_k(
    prompt: str, index: Index, k: int, *, pickable_only: bool = True
) -> list[tuple[CatalogEntry, float]]:
    if index.embeddings.shape[0] == 0 or k <= 0:
        return []
    q = embed_one(prompt)
    return _rank_prescored(q, index, k, pickable_only)


def embed_and_rank(
    prompt: str, index: Index, k: int, *, pickable_only: bool = True
) -> tuple[list[tuple[CatalogEntry, float]], np.ndarray | None]:
    """Like `top_k`, but also returns the query embedding.

    A caller that needs the same prompt's vector again afterward (matcher.py
    folding it into the centroid sketch — see `matcher.JudgeTrace.query_embedding`)
    can reuse it instead of paying for a second ~100ms fastembed call on a
    path the README promises is fast. Returns `(ranked, None)` in the same
    short-circuit case `top_k` takes — empty catalog or `k<=0` — so no
    embedding is computed there either.
    """
    if index.embeddings.shape[0] == 0 or k <= 0:
        return [], None
    q = embed_one(prompt)
    return _rank_prescored(q, index, k, pickable_only), q
