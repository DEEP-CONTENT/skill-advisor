import numpy as np

from skill_advisor import catalog as catalog_mod
from skill_advisor import index as index_mod
from skill_advisor.catalog import CatalogEntry


def _make_catalog() -> list[CatalogEntry]:
    return [
        CatalogEntry(kind="skill", name="alpha", namespace="user", description="first"),
        CatalogEntry(kind="skill", name="beta", namespace="user", description="second"),
        CatalogEntry(kind="skill", name="gamma", namespace="user", description="third"),
    ]


def test_top_k_returns_highest_cosine_scores_first(monkeypatch):
    entries = _make_catalog()
    embeddings = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )

    class FakeModel:
        def embed(self, texts):
            for t in texts:
                if t == "alpha":
                    yield np.array([1.0, 0.0, 0.0], dtype=np.float32)
                elif t == "beta":
                    yield np.array([0.0, 1.0, 0.0], dtype=np.float32)
                else:
                    yield np.array([0.0, 0.0, 1.0], dtype=np.float32)

    monkeypatch.setattr(index_mod, "_embed_model", lambda: FakeModel())

    idx = index_mod.Index(catalog=entries, embeddings=embeddings)
    picks = index_mod.top_k("beta", idx, k=2)
    assert picks[0][0].name == "beta"
    assert picks[0][1] == 1.0
    assert picks[1][0].name in {"alpha", "gamma"}


def test_save_load_round_trip(isolated_paths, monkeypatch):
    entries = _make_catalog()
    embeddings = np.ones((3, 4), dtype=np.float32)

    catalog_mod.save(entries)  # required — index.load reads catalog.json
    index_mod.save(entries, embeddings, "deadbeef")

    idx = index_mod.load()
    assert [e.name for e in idx.catalog] == ["alpha", "beta", "gamma"]
    assert idx.embeddings.shape == (3, 4)
    assert index_mod.current_hash() == "deadbeef"


def test_load_detects_size_mismatch(isolated_paths):
    entries = _make_catalog()
    mismatched = np.ones((2, 4), dtype=np.float32)
    catalog_mod.save(entries)
    index_mod.save(entries, mismatched, "h")

    # Tamper with catalog after save so shapes diverge.
    catalog_mod.save(entries + [CatalogEntry(kind="skill", name="delta", namespace="user", description="d")])

    import pytest
    with pytest.raises(ValueError):
        index_mod.load()


def test_top_k_empty_catalog_returns_empty():
    idx = index_mod.Index(catalog=[], embeddings=np.zeros((0, 4), dtype=np.float32))
    assert index_mod.top_k("anything", idx, k=5) == []
