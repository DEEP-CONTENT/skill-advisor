"""Local embedding index over the catalog.

Uses fastembed's BGE-small (384-dim, ~15 MB, CPU-only). No API keys.
Cold load off disk (mmap'd npz + JSON) is ~20 ms on SSD.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import catalog as catalog_mod
from . import paths
from .catalog import CatalogEntry

_MODEL_NAME = "BAAI/bge-small-en-v1.5"


@dataclass
class Index:
    catalog: list[CatalogEntry]
    embeddings: np.ndarray  # shape (N, D), float32, L2-normalised


def _embed_model():
    # Lazy import — fastembed import is slow (~500 ms) and we don't want it on triage-skip paths.
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=_MODEL_NAME)


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
    return Index(catalog=catalog, embeddings=embeddings)


def current_hash() -> str | None:
    f = paths.catalog_hash_file()
    if not f.is_file():
        return None
    return f.read_text(encoding="utf-8").strip()


def top_k(prompt: str, index: Index, k: int) -> list[tuple[CatalogEntry, float]]:
    if index.embeddings.shape[0] == 0 or k <= 0:
        return []
    model = _embed_model()
    q = np.array(list(model.embed([prompt])), dtype=np.float32)
    q = _normalise(q)[0]
    scores = index.embeddings @ q  # cosine because both sides are unit vectors
    k = min(k, scores.shape[0])
    top_idx = np.argpartition(-scores, k - 1)[:k]
    top_idx = top_idx[np.argsort(-scores[top_idx])]
    return [(index.catalog[i], float(scores[i])) for i in top_idx]
