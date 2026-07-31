import numpy as np

from skill_advisor import centroids, paths


def _unit(*xs):
    v = np.zeros(centroids.DIM, dtype=np.float32)
    for i, x in enumerate(xs):
        v[i] = x
    n = np.linalg.norm(v)
    return v / (n or 1.0)


def test_file_size_is_fixed_across_many_updates(isolated_paths):
    """Privacy claim: fixed-size lossy aggregate, not a record of any prompt."""
    s = centroids.empty()
    for i in range(200):
        centroids.observe(s, _unit(1.0, i % 7))
    centroids.save(s)
    first = paths.centroids_file().stat().st_size

    for i in range(2000):
        centroids.observe(s, _unit(1.0, i % 11))
    centroids.save(s)
    assert paths.centroids_file().stat().st_size == first
    assert s.vectors.shape == (centroids.DEFAULT_K, centroids.DIM)


def test_centroids_stay_unit_length(isolated_paths):
    s = centroids.empty()
    for i in range(100):
        centroids.observe(s, _unit(1.0, i % 5, 0.3))
    claimed = s.counts > 0
    norms = np.linalg.norm(s.vectors[claimed], axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_seeding_claims_distinct_slots_before_nudging(isolated_paths):
    s = centroids.empty(k=3)
    centroids.observe(s, _unit(1.0))
    centroids.observe(s, _unit(0.0, 1.0))
    centroids.observe(s, _unit(0.0, 0.0, 1.0))
    assert int((s.counts > 0).sum()) == 3


def test_identical_prompts_do_not_each_claim_a_slot(isolated_paths):
    """Seeding one slot per prompt burns all K on near-duplicates: a user whose
    first eight prompts are similar gets eight copies of one region and no
    capacity for the rest, which defeats having K>1 at all."""
    s = centroids.empty(k=4)
    for _ in range(10):
        centroids.observe(s, _unit(1.0))
    assert int((s.counts > 0).sum()) == 1
    assert int(s.counts[0]) == 10


def test_a_second_distinct_region_still_claims_its_own_slot(isolated_paths):
    """The dedup guard must not go so far that genuinely new work is absorbed
    into an existing centroid."""
    s = centroids.empty(k=4)
    for _ in range(10):
        centroids.observe(s, _unit(1.0))
    for _ in range(10):
        centroids.observe(s, _unit(0.0, 1.0))
    assert int((s.counts > 0).sum()) == 2
    assert sorted(int(c) for c in s.counts if c) == [10, 10]


def test_nearest_assignment_is_stable(isolated_paths):
    s = centroids.empty(k=2)
    for _ in range(20):
        centroids.observe(s, _unit(1.0))
    for _ in range(20):
        centroids.observe(s, _unit(0.0, 1.0))
    before = s.vectors.copy()
    centroids.observe(s, _unit(0.99, 0.14))
    moved = np.linalg.norm(s.vectors - before, axis=1)
    assert moved[0] > moved[1]  # the near-x prompt moved the x centroid, not the y one


def test_fit_scores_a_close_skill_above_an_unrelated_one(isolated_paths):
    s = centroids.empty(k=2)
    for _ in range(10):
        centroids.observe(s, _unit(1.0))
    embeddings = np.stack([_unit(0.98, 0.2), _unit(0.0, 1.0)])
    scores = centroids.fit(s, embeddings, min_observed=5)
    assert scores is not None
    assert scores[0] > scores[1]


def test_fit_is_suppressed_before_the_cold_start_floor(isolated_paths):
    s = centroids.empty()
    centroids.observe(s, _unit(1.0))
    assert centroids.fit(s, np.stack([_unit(1.0)]), min_observed=200) is None


def test_load_returns_empty_on_a_wrong_shaped_file(isolated_paths):
    paths.ensure_dirs()
    np.savez(
        paths.centroids_file(),
        vectors=np.zeros((3, 5), dtype=np.float32),
        counts=np.zeros(3, dtype=np.int64),
        observed=np.int64(1),
    )
    s = centroids.load()
    assert s.observed == 0
    assert s.vectors.shape == (centroids.DEFAULT_K, centroids.DIM)


def test_load_returns_empty_on_a_truncated_file(isolated_paths):
    """Truncated npz (e.g., from interrupted save) raises BadZipFile, not OSError."""
    paths.ensure_dirs()
    # Write a well-formed npz, then truncate it to simulate interrupted save
    temp = paths.centroids_file()
    np.savez(
        temp,
        vectors=np.zeros((8, 384), dtype=np.float32),
        counts=np.zeros(8, dtype=np.int64),
        observed=np.int64(10),
    )
    # Truncate to 10 bytes (partial zip header)
    temp.write_bytes(temp.read_bytes()[:10])
    s = centroids.load()
    assert s.observed == 0
    assert s.vectors.shape == (centroids.DEFAULT_K, centroids.DIM)


def test_load_returns_empty_on_a_zero_byte_file(isolated_paths):
    """Zero-byte file (e.g., from disk-full write) raises EOFError, not OSError."""
    paths.ensure_dirs()
    paths.centroids_file().write_bytes(b"")
    s = centroids.load()
    assert s.observed == 0
    assert s.vectors.shape == (centroids.DEFAULT_K, centroids.DIM)
