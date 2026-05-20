from pathlib import Path

from skill_advisor import sync


def _write_skill(parent: Path, name: str, description: str = "demo") -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )
    return d


def test_sync_copies_new_skills(tmp_path, isolated_paths):
    source = tmp_path / "bundle"
    source.mkdir()
    _write_skill(source, "alpha")
    _write_skill(source, "beta")

    report = sync.sync(source=source)
    assert set(report.copied) == {"alpha", "beta"}
    assert report.skipped == []
    dest = isolated_paths["claude_home"] / "skills"
    assert (dest / "alpha" / "SKILL.md").is_file()
    assert (dest / "beta" / "SKILL.md").is_file()


def test_sync_skips_existing_without_force(tmp_path, isolated_paths):
    source = tmp_path / "bundle"
    source.mkdir()
    _write_skill(source, "alpha", description="bundled")

    dest_root = isolated_paths["claude_home"] / "skills"
    _write_skill(dest_root, "alpha", description="local override")

    report = sync.sync(source=source)
    assert report.copied == []
    assert report.skipped == ["alpha"]
    # Local copy preserved.
    assert "local override" in (dest_root / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_sync_force_overwrites(tmp_path, isolated_paths):
    source = tmp_path / "bundle"
    source.mkdir()
    _write_skill(source, "alpha", description="bundled wins")

    dest_root = isolated_paths["claude_home"] / "skills"
    _write_skill(dest_root, "alpha", description="local version")

    report = sync.sync(source=source, force=True)
    assert report.overwritten == ["alpha"]
    assert "bundled wins" in (dest_root / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_sync_dry_run_changes_nothing(tmp_path, isolated_paths):
    source = tmp_path / "bundle"
    source.mkdir()
    _write_skill(source, "alpha")

    report = sync.sync(source=source, dry_run=True)
    assert report.copied == ["alpha"]
    dest = isolated_paths["claude_home"] / "skills"
    assert not (dest / "alpha").exists()


def test_sync_skips_dirs_without_skill_md(tmp_path, isolated_paths):
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "not-a-skill").mkdir()  # empty dir, no SKILL.md

    report = sync.sync(source=source)
    assert report.copied == []


def test_locate_bundle_prefers_explicit_path(tmp_path, monkeypatch):
    src = tmp_path / "explicit"
    src.mkdir()
    _write_skill(src, "a")
    found = sync.locate_bundle(str(src))
    assert found == src


def test_locate_bundle_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SKILL_ADVISOR_BUNDLE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sync.locate_bundle() is None


def test_locate_bundle_picks_up_env_var(tmp_path, monkeypatch):
    bundle = tmp_path / "repo"
    (bundle / "skills").mkdir(parents=True)
    _write_skill(bundle / "skills", "a")
    monkeypatch.setenv("SKILL_ADVISOR_BUNDLE", str(bundle))
    monkeypatch.chdir(tmp_path)  # away from any stray ./skills
    found = sync.locate_bundle()
    assert found == bundle / "skills"
