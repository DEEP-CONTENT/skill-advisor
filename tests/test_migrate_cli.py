import argparse
import json
import tomllib

from skill_advisor import cli, paths


def _write_config(config_home, names):
    body = "[catalog]\nexclude_names = [" + ", ".join(f'"{n}"' for n in names) + "]\n"
    (config_home / "config.toml").write_text(body, encoding="utf-8")


def _ns(**kw):
    base = {"revert": False, "verbose": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_migrate_moves_excludes_into_skill_overrides(isolated_paths, capsys):
    _write_config(isolated_paths["config_home"], ["alpha", "beta"])

    assert cli._cmd_migrate_excludes(_ns()) == 0

    settings = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert settings["skillOverrides"] == {"alpha": "off", "beta": "off"}

    cfg = tomllib.loads((isolated_paths["config_home"] / "config.toml").read_text())
    assert cfg["catalog"]["exclude_names"] == []


def test_migrate_backs_up_both_files_and_prints_a_revert_command(
    isolated_paths, capsys
):
    _write_config(isolated_paths["config_home"], ["alpha"])
    assert cli._cmd_migrate_excludes(_ns()) == 0
    out = capsys.readouterr().out

    assert (isolated_paths["config_home"] / "config.toml.pre-migrate.bak").is_file()
    assert "migrate-excludes --revert" in out


def test_revert_restores_both_files_exactly(isolated_paths):
    _write_config(isolated_paths["config_home"], ["alpha", "beta"])
    original = (isolated_paths["config_home"] / "config.toml").read_bytes()

    assert cli._cmd_migrate_excludes(_ns()) == 0
    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 0

    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == original


def test_revert_with_no_backups_fails_loudly(isolated_paths, capsys):
    """`rotate` and `migrate-excludes` are CLI verbs, not hooks — they must not
    be silent on error the way the hook paths are."""
    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 1
    assert "nothing to revert" in capsys.readouterr().out
