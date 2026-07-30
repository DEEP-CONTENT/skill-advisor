import argparse
import json
import tomllib

from skill_advisor import cli, paths


def _write_config(config_home, names):
    body = "[catalog]\nexclude_names = [" + ", ".join(f'"{n}"' for n in names) + "]\n"
    (config_home / "config.toml").write_text(body, encoding="utf-8")


def _write_user_skill(claude_home, name):
    """A real, addressable user skill — `overrides.override_key()` resolves
    it to a non-None key, so migrate-excludes should treat it as migratable."""
    skill_dir = claude_home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f'description: "Use when the user asks about {name}."\n'
        "---\n\n"
        f"# {name}\n",
        encoding="utf-8",
    )


def _ns(**kw):
    base = {"revert": False, "verbose": False, "create_settings": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_migrate_moves_excludes_into_skill_overrides(isolated_paths, capsys):
    _write_user_skill(isolated_paths["claude_home"], "alpha")
    _write_user_skill(isolated_paths["claude_home"], "beta")
    _write_config(isolated_paths["config_home"], ["alpha", "beta"])

    assert cli._cmd_migrate_excludes(_ns(create_settings=True)) == 0

    settings = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert settings["skillOverrides"] == {"alpha": "off", "beta": "off"}

    cfg = tomllib.loads((isolated_paths["config_home"] / "config.toml").read_text())
    assert cfg["catalog"]["exclude_names"] == []


def test_migrate_backs_up_both_files_and_prints_a_revert_command(
    isolated_paths, capsys
):
    _write_user_skill(isolated_paths["claude_home"], "alpha")
    _write_config(isolated_paths["config_home"], ["alpha"])
    assert cli._cmd_migrate_excludes(_ns(create_settings=True)) == 0
    out = capsys.readouterr().out

    assert (isolated_paths["config_home"] / "config.toml.pre-migrate.bak").is_file()
    assert "migrate-excludes --revert" in out


def test_revert_restores_both_files_exactly(isolated_paths):
    _write_user_skill(isolated_paths["claude_home"], "alpha")
    _write_user_skill(isolated_paths["claude_home"], "beta")
    _write_config(isolated_paths["config_home"], ["alpha", "beta"])
    original = (isolated_paths["config_home"] / "config.toml").read_bytes()

    assert cli._cmd_migrate_excludes(_ns(create_settings=True)) == 0
    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 0

    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == original


def test_revert_with_no_backups_fails_loudly(isolated_paths, capsys):
    """`rotate` and `migrate-excludes` are CLI verbs, not hooks — they must not
    be silent on error the way the hook paths are."""
    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 1
    assert "nothing to revert" in capsys.readouterr().out


def test_migrate_retains_subagents_and_namespaced_entries(isolated_paths, capsys):
    """Real-world shape (measured against the live config): exclude_names
    mixes an addressable user skill with a builtin subagent name (`Plan`)
    and a namespaced builtin subagent (`feature-dev:code-architect`, which
    IS a real catalog entry — a plugin subagent, not a skill) plus a name
    matching nothing on disk. Only the skill can be migrated; the rest must
    stay in exclude_names or their mute is silently lost."""
    _write_user_skill(isolated_paths["claude_home"], "gamma")
    _write_config(
        isolated_paths["config_home"],
        ["gamma", "Plan", "feature-dev:code-architect", "ghost-skill-not-on-disk"],
    )

    assert cli._cmd_migrate_excludes(_ns(create_settings=True)) == 0
    out = capsys.readouterr().out

    settings = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert settings["skillOverrides"] == {"gamma": "off"}

    cfg = tomllib.loads((isolated_paths["config_home"] / "config.toml").read_text())
    assert set(cfg["catalog"]["exclude_names"]) == {
        "Plan",
        "feature-dev:code-architect",
        "ghost-skill-not-on-disk",
    }
    assert "gamma" not in cfg["catalog"]["exclude_names"]

    assert "migrated 1 of 4" in out
    assert "gamma" in out
    assert "Plan" in out
    assert "feature-dev:code-architect" in out
    assert "ghost-skill-not-on-disk" in out


def test_migrate_when_nothing_is_migratable_leaves_config_untouched(
    isolated_paths, capsys
):
    """All entries unresolvable/non-addressable -> no writes at all, exit 0,
    a clear explanation, and no backups (nothing was actually migrated).
    Nothing tries to touch the settings file either, so no --create-settings
    is needed here."""
    _write_config(isolated_paths["config_home"], ["Plan", "not-on-disk-either"])
    before = (isolated_paths["config_home"] / "config.toml").read_bytes()

    assert cli._cmd_migrate_excludes(_ns()) == 0
    out = capsys.readouterr().out

    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == before
    assert not (isolated_paths["config_home"] / "config.toml.pre-migrate.bak").is_file()
    assert "nothing migratable" in out
    assert "Plan" in out
    assert "not-on-disk-either" in out
    assert not paths.settings_file().is_file()


def test_migrate_rewrites_the_live_catalog_table_not_a_comment(isolated_paths, capsys):
    """A decoy `exclude_names = [...]` sits in a comment before the real
    `[catalog]` table. The rewrite must be scoped to the live table, not the
    first textual occurrence of `exclude_names` anywhere in the file."""
    _write_user_skill(isolated_paths["claude_home"], "delta")
    body = (
        '# old note: exclude_names = ["decoy"]\n'
        "[matcher]\n"
        "max_picks = 3\n"
        "\n"
        "[catalog]\n"
        'exclude_names = ["delta"]\n'
    )
    (isolated_paths["config_home"] / "config.toml").write_text(body, encoding="utf-8")

    assert cli._cmd_migrate_excludes(_ns(create_settings=True)) == 0

    text = (isolated_paths["config_home"] / "config.toml").read_text()
    assert '# old note: exclude_names = ["decoy"]' in text  # untouched
    cfg = tomllib.loads(text)
    assert cfg["catalog"]["exclude_names"] == []
    assert cfg["matcher"]["max_picks"] == 3


def test_migrate_leaves_config_untouched_if_the_toml_write_fails(
    isolated_paths, capsys, monkeypatch
):
    """If the config.toml write fails after skillOverrides already succeeded,
    config.toml must stay byte-identical, the failure must be reported (not
    a bare traceback), the command must exit non-zero, and the message must
    point at --revert AND list what already landed in skillOverrides."""
    _write_user_skill(isolated_paths["claude_home"], "epsilon")
    _write_config(isolated_paths["config_home"], ["epsilon"])
    before = (isolated_paths["config_home"] / "config.toml").read_bytes()

    monkeypatch.setattr(cli, "_write_config_toml", lambda *a, **kw: False)

    assert cli._cmd_migrate_excludes(_ns(create_settings=True)) == 1
    out = capsys.readouterr().out

    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == before
    assert "ERROR" in out
    assert "revert" in out.lower()
    assert "epsilon" in out  # the entries that DID land, not just a bare claim

    # skillOverrides DID get written before the config.toml write failed.
    settings = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert settings["skillOverrides"] == {"epsilon": "off"}


def test_migrate_noop_when_exclude_names_absent(isolated_paths, capsys):
    (isolated_paths["config_home"] / "config.toml").write_text(
        "[matcher]\nmax_picks = 3\n", encoding="utf-8"
    )

    assert cli._cmd_migrate_excludes(_ns()) == 0
    out = capsys.readouterr().out
    assert "already empty" in out

    text = (isolated_paths["config_home"] / "config.toml").read_text()
    assert "exclude_names" not in text
    assert not paths.settings_file().is_file()


# --- round 2: --create-settings guard + revert-deletes-what-it-created ---


def test_migrate_refuses_when_settings_file_absent_without_the_flag(
    isolated_paths, capsys
):
    """`paths.settings_file()` can silently resolve to a file Claude Code
    never reads (SKILL_ADVISOR_SETTINGS_FILE unset/wrong). Writing 400+
    entries there and reporting success would be a silent no-op in
    production. Refuse unless the user explicitly opts in."""
    _write_user_skill(isolated_paths["claude_home"], "zeta")
    _write_config(isolated_paths["config_home"], ["zeta"])
    cfg_before = (isolated_paths["config_home"] / "config.toml").read_bytes()

    assert cli._cmd_migrate_excludes(_ns()) == 1
    out = capsys.readouterr().out

    assert str(paths.settings_file()) in out
    assert "SKILL_ADVISOR_SETTINGS_FILE" in out
    assert "--create-settings" in out
    assert not paths.settings_file().is_file()
    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == cfg_before
    assert not (isolated_paths["config_home"] / "config.toml.pre-migrate.bak").is_file()


def test_revert_deletes_a_settings_file_this_migration_created(isolated_paths):
    """First-ever migrate: no settings file exists beforehand, so there's no
    backup bytes to restore. --revert must delete the file this run created
    rather than silently leaving its migrated `off` entries in place."""
    _write_user_skill(isolated_paths["claude_home"], "eta")
    _write_config(isolated_paths["config_home"], ["eta"])
    cfg_original = (isolated_paths["config_home"] / "config.toml").read_bytes()
    assert not paths.settings_file().is_file()

    assert cli._cmd_migrate_excludes(_ns(create_settings=True)) == 0
    assert paths.settings_file().is_file()

    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 0

    assert not paths.settings_file().is_file()
    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == cfg_original


def test_revert_restores_pre_existing_settings_file_byte_identical_unchanged_behavior(
    isolated_paths,
):
    """Do not regress the already-proven case: when the settings file
    pre-exists, migrate doesn't need --create-settings, and --revert restores
    its original bytes rather than deleting it."""
    _write_user_skill(isolated_paths["claude_home"], "theta")
    _write_config(isolated_paths["config_home"], ["theta"])
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(
        json.dumps({"effortLevel": "high", "skillOverrides": {"pre-existing": "off"}}),
        encoding="utf-8",
    )
    settings_original = paths.settings_file().read_bytes()
    cfg_original = (isolated_paths["config_home"] / "config.toml").read_bytes()

    assert cli._cmd_migrate_excludes(_ns()) == 0  # no --create-settings needed
    assert cli._cmd_migrate_excludes(_ns(revert=True)) == 0

    assert paths.settings_file().is_file()
    assert paths.settings_file().read_bytes() == settings_original
    assert (isolated_paths["config_home"] / "config.toml").read_bytes() == cfg_original


# --- round 2 minor: direct coverage of _write_config_toml's own internals ---


def test_write_config_toml_rejects_invalid_toml(isolated_paths):
    cfg_path = isolated_paths["config_home"] / "config.toml"
    cfg_path.write_text("[catalog]\nexclude_names = []\n", encoding="utf-8")
    before = cfg_path.read_bytes()

    assert cli._write_config_toml(cfg_path, "[catalog\nexclude_names = [") is False

    assert cfg_path.read_bytes() == before
    assert list(cfg_path.parent.glob("*.toml.tmp.*")) == []


def test_write_config_toml_returns_false_when_replace_raises(
    isolated_paths, monkeypatch
):
    from pathlib import Path

    cfg_path = isolated_paths["config_home"] / "config.toml"
    cfg_path.write_text("[catalog]\nexclude_names = []\n", encoding="utf-8")
    before = cfg_path.read_bytes()

    def _boom(self, target):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(Path, "replace", _boom)

    assert cli._write_config_toml(cfg_path, "[catalog]\nexclude_names = []\n") is False

    assert cfg_path.read_bytes() == before
    assert list(cfg_path.parent.glob("*.toml.tmp.*")) == []
