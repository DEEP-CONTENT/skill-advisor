import ast
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from skill_advisor import manifest
from skill_advisor.manifest import ManifestFilter, load_manifest


def _write(path, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def test_manifest_filter_is_frozen():
    filter_ = ManifestFilter()

    with pytest.raises(FrozenInstanceError):
        filter_.schema_version = 1


def test_empty_returns_neutral_filter():
    filter_ = ManifestFilter.empty()

    assert filter_.schema_version == 0
    assert filter_.excluded_subskills == frozenset()
    assert filter_.excluded_subagents == frozenset()
    assert filter_.plugin_whitelist is None


def test_excluded_names_is_union_of_subskills_and_subagents():
    filter_ = ManifestFilter(
        excluded_subskills=frozenset({"skill-a", "shared"}),
        excluded_subagents=frozenset({"agent-a", "shared"}),
    )

    assert filter_.excluded_names == frozenset({"skill-a", "agent-a", "shared"})


def test_load_manifest_reads_valid_object_fields(tmp_path, isolated_paths):
    path = _write(
        tmp_path / "manifest.json",
        {
            "schema_version": 2,
            "excluded_subskills": ["skill-a", "skill-b"],
            "excluded_subagents": ["agent-a"],
        },
    )

    filter_ = load_manifest(path, isolated_paths["claude_home"])

    assert filter_.schema_version == 2
    assert filter_.excluded_subskills == frozenset({"skill-a", "skill-b"})
    assert filter_.excluded_subagents == frozenset({"agent-a"})
    assert filter_.plugin_whitelist is None


def test_load_manifest_trims_strings_and_discards_invalid_list_members(tmp_path, isolated_paths):
    path = _write(
        tmp_path / "manifest.json",
        {
            "excluded_subskills": [" skill-a ", "", "   ", 123, None, True],
            "excluded_subagents": [" agent-a ", [], {"name": "agent-b"}],
        },
    )

    filter_ = load_manifest(path, isolated_paths["claude_home"])

    assert filter_.excluded_subskills == frozenset({"skill-a"})
    assert filter_.excluded_subagents == frozenset({"agent-a"})


@pytest.mark.parametrize("schema_version", ["v1", True])
def test_load_manifest_resets_non_integer_schema_version_to_zero(
    tmp_path,
    isolated_paths,
    schema_version,
):
    path = _write(tmp_path / "manifest.json", {"schema_version": schema_version})

    filter_ = load_manifest(path, isolated_paths["claude_home"])

    assert filter_.schema_version == 0


def test_load_manifest_fail_soft_for_empty_path(isolated_paths):
    assert load_manifest("", isolated_paths["claude_home"]) == ManifestFilter.empty()


def test_load_manifest_fail_soft_for_missing_file(tmp_path, isolated_paths):
    path = tmp_path / "missing.json"

    assert load_manifest(path, isolated_paths["claude_home"]) == ManifestFilter.empty()


def test_load_manifest_fail_soft_for_broken_json(tmp_path, isolated_paths):
    path = _write(tmp_path / "manifest.json", "{not valid json")

    assert load_manifest(path, isolated_paths["claude_home"]) == ManifestFilter.empty()


def test_load_manifest_fail_soft_for_non_object_top_level(tmp_path, isolated_paths):
    path = _write(tmp_path / "manifest.json", ["not", "an", "object"])

    assert load_manifest(path, isolated_paths["claude_home"]) == ManifestFilter.empty()


def test_load_manifest_fail_soft_for_oversized_file(tmp_path, isolated_paths):
    path = _write(
        tmp_path / "manifest.json",
        {"padding": "x" * manifest.MAX_MANIFEST_BYTES},
    )

    assert path.stat().st_size > manifest.MAX_MANIFEST_BYTES
    assert load_manifest(path, isolated_paths["claude_home"]) == ManifestFilter.empty()


def test_plugin_whitelist_missing_keeps_none(tmp_path, isolated_paths):
    path = _write(tmp_path / "manifest.json", {})

    filter_ = load_manifest(path, isolated_paths["claude_home"])

    assert filter_.plugin_whitelist is None


def test_plugin_whitelist_present_empty_list_becomes_empty_tuple(tmp_path, isolated_paths):
    path = _write(tmp_path / "manifest.json", {"plugin_whitelist": []})

    filter_ = load_manifest(path, isolated_paths["claude_home"])

    assert filter_.plugin_whitelist == ()


def test_plugin_whitelist_keeps_order_and_deduplicates_names(tmp_path, isolated_paths):
    path = _write(
        tmp_path / "manifest.json",
        {
            "plugin_whitelist": [
                "alpha",
                {"name": "beta"},
                "alpha",
                {"name": " gamma "},
                {"name": "beta"},
            ]
        },
    )

    filter_ = load_manifest(path, isolated_paths["claude_home"])

    assert filter_.plugin_whitelist == ("alpha", "beta", "gamma")


def test_plugin_whitelist_discards_entries_without_name_and_non_list_is_empty_tuple(
    tmp_path,
    isolated_paths,
):
    no_name_path = _write(
        tmp_path / "manifest-no-name.json",
        {"plugin_whitelist": [{"path": "plugins/x"}, {"name": ""}, 123]},
    )
    non_list_path = _write(
        tmp_path / "manifest-non-list.json",
        {"plugin_whitelist": "alpha"},
    )

    assert load_manifest(no_name_path, isolated_paths["claude_home"]).plugin_whitelist == ()
    assert load_manifest(non_list_path, isolated_paths["claude_home"]).plugin_whitelist == ()


def test_plugin_whitelist_confines_paths_to_claude_home(tmp_path, isolated_paths):
    claude_home = isolated_paths["claude_home"]
    inside_dir = claude_home / "plugins" / "inside"
    outside_dir = tmp_path / "outside"
    inside_dir.mkdir(parents=True)
    outside_dir.mkdir(parents=True)
    path = _write(
        tmp_path / "manifest.json",
        {
            "plugin_whitelist": [
                {"name": "inside-relative", "path": "plugins/inside"},
                {"name": "escapes-relative", "path": "../outside"},
                {"name": "escapes-absolute", "path": str(outside_dir)},
            ]
        },
    )

    filter_ = load_manifest(path, claude_home)

    assert filter_.plugin_whitelist == ("inside-relative",)


def test_manifest_module_uses_only_stdlib_imports():
    source = manifest.__file__
    assert source is not None
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    allowed = {"json", "logging", "dataclasses", "pathlib", "__future__"}
    imported_modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module.split(".", 1)[0])

    assert imported_modules <= allowed
