"""`skill-advisor sync-skills` — copy an external library of `SKILL.md` folders
into the user's `~/.claude/skills/` so Claude Code can actually invoke them.

The advisor only *recommends* skills; Claude Code's Skill tool looks for them on
disk under `~/.claude/skills/<name>/SKILL.md`. This module supports the
bring-your-own-library model: pass `--from PATH` (or set `SKILL_ADVISOR_BUNDLE`,
or run from a directory containing a `skills/` folder) to point at the source.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class SyncReport:
    source: Path
    dest: Path
    copied: list[str]
    skipped: list[str]
    overwritten: list[str]

    def summary(self) -> str:
        lines = [
            f"source: {self.source}",
            f"dest:   {self.dest}",
            f"copied:      {len(self.copied)}",
            f"skipped:     {len(self.skipped)} (already present; rerun with --force to overwrite)",
            f"overwritten: {len(self.overwritten)}",
        ]
        return "\n".join(lines)


def _iter_skill_dirs(source: Path):
    for entry in sorted(source.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if not (entry / "SKILL.md").is_file():
            continue
        yield entry


def locate_bundle(explicit: str | None = None) -> Path | None:
    """Find the repo's `skills/` directory. Precedence:
    1. --from <path> argument
    2. $SKILL_ADVISOR_BUNDLE
    3. ./skills/ relative to current working dir
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("SKILL_ADVISOR_BUNDLE")
    if env:
        candidates.append(Path(env).expanduser() / "skills")
    candidates.append(Path.cwd() / "skills")

    for candidate in candidates:
        if candidate.is_dir() and any(_iter_skill_dirs(candidate)):
            return candidate
    return None


def sync(
    source: Path,
    dest: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> SyncReport:
    target = dest or (paths.claude_home() / "skills")
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []
    overwritten: list[str] = []

    for skill_dir in _iter_skill_dirs(source):
        dest_dir = target / skill_dir.name
        if dest_dir.exists():
            if force:
                if not dry_run:
                    shutil.rmtree(dest_dir)
                    shutil.copytree(skill_dir, dest_dir)
                overwritten.append(skill_dir.name)
            else:
                skipped.append(skill_dir.name)
            continue
        if not dry_run:
            shutil.copytree(skill_dir, dest_dir)
        copied.append(skill_dir.name)

    return SyncReport(
        source=source,
        dest=target,
        copied=copied,
        skipped=skipped,
        overwritten=overwritten,
    )
