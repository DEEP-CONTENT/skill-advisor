from unittest.mock import patch

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


def test_top_k_skips_disabled_entries(isolated_paths):
    """The 58.6% bug, at the index layer. Must fail against today's code."""
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="best", namespace="user", description="d",
                     path="/s/best/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="ok", namespace="user", description="d",
                     path="/s/ok/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, True]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        got = index_mod.top_k("q", idx, 2)

    assert [e.name for e, _ in got] == ["ok"]


def test_top_k_returns_k_pickable_not_k_minus_disabled(isolated_paths):
    """Masking must happen before selection, or a disabled top-scorer eats a slot."""
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="off1", namespace="user", description="d",
                     path="/s/off1/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="on1", namespace="user", description="d",
                     path="/s/on1/SKILL.md", enabled=True),
        CatalogEntry(kind="skill", name="on2", namespace="user", description="d",
                     path="/s/on2/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.9, 0.436], [0.8, 0.6]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, True, True]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        got = index_mod.top_k("q", idx, 2)

    assert [e.name for e, _ in got] == ["on1", "on2"]


def test_top_k_returns_empty_when_everything_is_disabled(isolated_paths):
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="x", namespace="user", description="d",
                     path="/s/x/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="y", namespace="user", description="d",
                     path="/s/y/SKILL.md", enabled=False),
    ]
    emb = np.array([[1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, False]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        assert index_mod.top_k("q", idx, 2) == []


def test_top_k_clamps_when_k_exceeds_the_pickable_count(isolated_paths):
    """max_candidates is 15 but the pickable set may be smaller. The masked
    scores are -inf, and those must never reach the caller as picks."""
    import numpy as np

    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="off", namespace="user", description="d",
                     path="/s/off/SKILL.md", enabled=False),
        CatalogEntry(kind="skill", name="on", namespace="user", description="d",
                     path="/s/on/SKILL.md", enabled=True),
    ]
    emb = np.array([[1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
    idx = index_mod.Index(catalog=entries, embeddings=emb,
                          pickable=np.array([False, True]))

    class _Fixed:
        def embed(self, texts):
            for _ in texts:
                yield np.array([1.0, 0.0], dtype=np.float32)

    with patch.object(index_mod, "_embed_model", return_value=_Fixed()):
        got = index_mod.top_k("q", idx, 15)

    assert [e.name for e, _ in got] == ["on"]
    assert all(np.isfinite(s) for _, s in got)


def test_load_derives_the_pickable_mask(isolated_paths):
    import numpy as np

    from skill_advisor import catalog as catalog_mod
    from skill_advisor import index as index_mod
    from skill_advisor.catalog import CatalogEntry

    entries = [
        CatalogEntry(kind="skill", name="a", namespace="user", description="d",
                     path="/s/a/SKILL.md", enabled=True),
        CatalogEntry(kind="skill", name="b", namespace="user", description="d",
                     path="/s/b/SKILL.md", enabled=False),
    ]
    emb = np.eye(2, dtype=np.float32)
    index_mod.save(entries, emb, "h")
    loaded = index_mod.load()
    assert list(loaded.pickable) == [True, False]
