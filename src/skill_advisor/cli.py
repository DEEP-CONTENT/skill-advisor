"""`skill-advisor` CLI dispatcher."""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path

import shutil as _shutil  # for sessions dir cleanup

from . import catalog as catalog_mod
from . import centroids
from . import hook as hook_mod
from . import index as index_mod
from . import install as install_mod
from . import lifecycle as lifecycle_mod
from . import matcher, overrides, paths, rotate, telemetry, triage
from . import sync as sync_mod
from .config import load as load_config

log = logging.getLogger(__name__)


def _cmd_install(args: argparse.Namespace) -> int:
    if shutil.which("claude") is None:
        print(
            "warning: `claude` is not on PATH. Install it before activating the advisor.",
            file=sys.stderr,
        )

    try:
        settings_path = install_mod.render_settings()
    except install_mod.RenderSettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config_path = install_mod.write_default_config(force=False)

    print(f"settings: {settings_path}")
    print(f"config:   {config_path}")

    alias_changed = False
    shell = install_mod.detect_shell()
    if args.write_alias:
        if shell is None:
            print(
                "error: --write-alias given but shell could not be detected "
                "from $SHELL (supported: bash, zsh, fish).",
                file=sys.stderr,
            )
            return 2
        alias_changed = install_mod.install_alias(shell)
        print(
            f"alias:    {'added to' if alias_changed else 'already present in'} "
            f"{shell.rc_file} ({shell.name})"
        )

    if args.no_build:
        print("skipped first catalog build (--no-build). Run `skill-advisor build` next.")
    else:
        print("building catalog + embeddings (first run downloads the BGE-small model, ~15 MB)...")
        count = _build_catalog()
        print(f"indexed {count} catalog entries")

    _print_activation_hint(settings_path, alias_written=args.write_alias and alias_changed, shell=shell)
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    shell = install_mod.detect_shell()
    if shell is not None:
        changed = install_mod.uninstall_alias(shell)
        print(f"alias: {'removed from' if changed else 'not present in'} {shell.rc_file}")

    for path in (
        paths.settings_file(),
        paths.catalog_file(),
        paths.embeddings_file(),
        paths.catalog_hash_file(),
        paths.log_file(),
        paths.events_file(),
        paths.telemetry_salt_file(),
        paths.effort_file(),
        paths.observed_effort_file(),
        paths.baseline_file(),
        paths.statusline_script(),
        paths.centroids_file(),
    ):
        if path.is_file():
            path.unlink()
            print(f"removed: {path}")

    if args.purge_config and paths.config_file().is_file():
        paths.config_file().unlink()
        print(f"removed: {paths.config_file()}")
    elif paths.config_file().is_file():
        print(f"kept: {paths.config_file()} (use --purge-config to remove)")

    return 0


def _build_catalog() -> int:
    cfg = load_config()
    entries = catalog_mod.scan(cfg)
    source_hash = catalog_mod.compute_hash(entries)
    embeddings = index_mod.build(entries)
    index_mod.save(entries, embeddings, source_hash)
    return len(entries)


def _cmd_build(args: argparse.Namespace) -> int:
    cfg = load_config()
    entries = catalog_mod.scan(cfg)
    source_hash = catalog_mod.compute_hash(entries)
    existing = index_mod.current_hash()
    if existing == source_hash and not args.force and paths.embeddings_file().is_file():
        print(f"catalog unchanged ({len(entries)} entries); pass --force to rebuild anyway.")
        return 0
    print(f"embedding {len(entries)} entries...")
    embeddings = index_mod.build(entries)
    index_mod.save(entries, embeddings, source_hash)
    print(f"wrote {paths.catalog_file()}")
    print(f"wrote {paths.embeddings_file()}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    cfg = load_config()
    path = Path(args.prompts)
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        return 2

    durations: list[float] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
            prompt = record.get("prompt", raw)
        except json.JSONDecodeError:
            prompt = raw

        started = time.monotonic()
        result = matcher.pick(prompt, cfg)
        elapsed = time.monotonic() - started
        durations.append(elapsed)

        # matcher.pick() returns a PickResult (or None), not a list — the
        # prior `picks = matcher.pick(...)` / `for p in picks` iterated the
        # PickResult object itself, which has no __iter__ and raises
        # `TypeError: 'PickResult' object is not iterable` for any prompt
        # that actually produces picks. `replay` exists specifically so a
        # user can see what the pipeline would recommend across a batch of
        # prompts; it was non-functional on the one case that matters.
        if result and result.picks:
            # invoke_name (falling back to name) — the string the Skill
            # tool actually accepts. Same fix as inject.py / the `match`
            # renderer.
            rendered = ", ".join(
                f"{p.entry.invoke_name or p.entry.name} ({p.entry.kind})"
                for p in result.picks
            )
        else:
            rendered = "<skip>"
        print(f"[{elapsed:6.2f}s] {prompt[:70]!r:72s} -> {rendered}")

    if durations:
        durations.sort()
        p50 = durations[len(durations) // 2]
        p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
        print(f"\nn={len(durations)} p50={p50:.2f}s p95={p95:.2f}s")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    report: list[tuple[str, str]] = []

    claude_bin = shutil.which("claude")
    report.append(("claude on PATH", claude_bin or "MISSING"))

    settings = paths.settings_file()
    report.append(("settings file", str(settings) if settings.is_file() else "MISSING"))

    cat = paths.catalog_file()
    emb = paths.embeddings_file()
    if cat.is_file() and emb.is_file():
        try:
            entries = catalog_mod.load()
            report.append(("catalog (cached)", f"{len(entries)} entries"))
        except Exception as exc:
            report.append(("catalog (cached)", f"broken: {exc}"))
    else:
        report.append(("catalog (cached)", "not built (run `skill-advisor build`)"))

    source_hash = None
    try:
        source_hash = catalog_mod.compute_hash(catalog_mod.scan())
    except Exception as exc:
        report.append(("scan", f"error: {exc}"))
    if source_hash and index_mod.current_hash() and index_mod.current_hash() != source_hash:
        report.append(("freshness", "STALE — rerun `skill-advisor build`"))
    elif source_hash:
        report.append(("freshness", "up-to-date"))

    log = paths.log_file()
    if log.is_file():
        size = log.stat().st_size
        report.append(("log", f"{log} ({size} bytes)"))
    else:
        report.append(("log", "not yet written"))

    width = max(len(label) for label, _ in report)
    for label, value in report:
        print(f"{label:<{width}} : {value}")

    # Parallelization-specific: judge needs budget headroom.
    cfg = load_config()

    # Pool health: how much of the disk the scanner can see.
    health = catalog_mod.pool_health(cfg)
    print(
        f"skill files    : {health['skill_md_files']} on disk, "
        f"{health['parseable']} parseable"
    )
    print(
        f"catalog (live) : {health['pool']} in pool, {health['pickable']} pickable"
    )
    if health["unparseable_count"] > 0:
        shown = ", ".join(health["unparseable_dirs"][:5])
        more = len(health["unparseable_dirs"]) - 5
        suffix = f", +{more} more" if more > 0 else ""
        print(
            f"WARN: {health['unparseable_count']} SKILL.md files are unparseable "
            f"(no YAML frontmatter) and invisible to the advisor. "
            f"Examples: {shown}{suffix}"
        )
    if health["excluded_but_enabled"]:
        print(
            f"NOTE: {len(health['excluded_but_enabled'])} names are configured as "
            f"excluded in catalog.exclude_names but remain enabled in Claude Code. "
            f"To reconcile: edit config.toml and remove them from catalog.exclude_names."
        )

    cfg_effort = cfg.effort
    if cfg_effort.enabled:
        jq = shutil.which("jq")
        print(f"jq             : {jq or 'MISSING (status line will render nothing)'}")
        script = paths.statusline_script()
        print(f"statusline     : {script if script.is_file() else 'not written (run install)'}")
        rec = paths.effort_file()
        print(f"effort state   : {'present' if rec.is_file() else 'none yet'}")

    if cfg.parallelization.enabled:
        min_budget = cfg.parallelization.judge_timeout_seconds + 3.0
        if cfg.matcher.budget_seconds < min_budget:
            print(
                f"WARN: parallelization.enabled=true but matcher.budget_seconds "
                f"({cfg.matcher.budget_seconds:.1f}) < judge_timeout_seconds+3 "
                f"({min_budget:.1f}). Raise matcher.budget_seconds or the detector "
                f"will frequently time out."
            )

    return 0 if claude_bin and cat.is_file() and emb.is_file() else 1


def _cmd_hook(args: argparse.Namespace) -> int:
    return hook_mod.run()


def _cmd_posttooluse(args: argparse.Namespace) -> int:
    return hook_mod.run_posttooluse()


def _cmd_stop(args: argparse.Namespace) -> int:
    return hook_mod.run_stop()


_PHASES = ("planning", "implementation", "review", "correction", "complete")


def _resolve_match_prompt(args: argparse.Namespace) -> str | None:
    """Positional arg wins; otherwise read stdin iff piped. TTY with no arg → None."""
    if args.prompt:
        return args.prompt.strip()
    if sys.stdin.isatty():
        return None
    return sys.stdin.read().strip()


def _render_match_text(
    prompt: str,
    picks: list[matcher.ResolvedPick],
    triage_skip: bool | None,
    verbose: bool,
) -> None:
    short = prompt if len(prompt) <= 80 else prompt[:77] + "..."
    print(f'prompt: "{short}"')
    if not picks:
        print("  (no picks)")
    else:
        # invoke_name (falling back to name) — the string the Skill tool
        # actually accepts, not the frontmatter `name:`. This is the command
        # a user runs to check what will be recommended; printing the wrong
        # name here would mask exactly the bug this renders to catch.
        names = [p.entry.invoke_name or p.entry.name for p in picks]
        width_kind = max(len(p.entry.kind) for p in picks)
        width_name = max(len(n) for n in names)
        for rank, (pick, name) in enumerate(zip(picks, names), start=1):
            line = (
                f"  {rank}. {pick.entry.kind:<{width_kind}}  "
                f"{name:<{width_name}}  {pick.reason}"
            )
            print(line)
            if verbose:
                if pick.entry.namespace:
                    print(f"      namespace: {pick.entry.namespace}")
                if pick.entry.description:
                    desc = pick.entry.description.strip().splitlines()[0]
                    print(f"      {desc}")
                if pick.entry.path:
                    print(f"      {pick.entry.path}")
    if triage_skip is not None:
        print(f"triage: {'would skip' if triage_skip else 'would match'}")


def _render_match_json(
    prompt: str,
    picks: list[matcher.ResolvedPick],
    triage_skip: bool | None,
    verbose: bool,
) -> None:
    out: dict = {
        "prompt": prompt,
        "picks": [
            {
                "rank": rank,
                "kind": p.entry.kind,
                # invoke_name (falling back to name) — the string the Skill
                # tool actually accepts. Same fix as the text renderer above.
                "name": p.entry.invoke_name or p.entry.name,
                "namespace": p.entry.namespace,
                "reason": p.reason,
                "description": p.entry.description,
                "path": p.entry.path,
            }
            for rank, p in enumerate(picks, start=1)
        ],
    }
    if triage_skip is not None:
        out["triage_skip"] = triage_skip
    print(json.dumps(out, indent=2 if verbose else None, ensure_ascii=False))


def _ingestion_rate(events: list[dict]) -> tuple[int, int]:
    """Pair each prompt-with-picks event with the next stop event in the same
    session, then count how many of those stops invoked the `Skill` tool.

    Returns (prompts_with_picks, skill_invoked).
    """
    # Sort once by timestamp so "next stop in session" is just a forward scan.
    by_session: dict[str, list[dict]] = {}
    for event in events:
        sess = event.get("session_sha256")
        if not sess:
            continue
        by_session.setdefault(sess, []).append(event)
    # Stable sort by ts string — ISO-8601 with fixed format sorts lexically.
    for items in by_session.values():
        items.sort(key=lambda e: e.get("ts") or "")

    prompts_with_picks = 0
    skill_invoked = 0
    for items in by_session.values():
        for i, event in enumerate(items):
            if event.get("kind", "prompt") != "prompt":
                continue
            if event.get("triage_skipped"):
                continue
            if not (event.get("picks") or []):
                continue
            # Walk forward for the next stop event in this session.
            for next_event in items[i + 1:]:
                if next_event.get("kind") == "stop":
                    prompts_with_picks += 1
                    tools = next_event.get("tools") or []
                    if "Skill" in tools:
                        skill_invoked += 1
                    break
    return prompts_with_picks, skill_invoked


def _report_stats(events: list[dict], catalog_names: set[str]) -> dict:
    """Compute aggregate stats from an event list. Deterministic ordering."""
    from collections import Counter

    pick_counts: "Counter[str]" = Counter()
    kind_counts: "Counter[str]" = Counter()
    phase_counts: "Counter[str]" = Counter()
    sessions: set[str] = set()
    durations: list[int] = []
    triage_skipped = 0
    prompt_events = 0

    for event in events:
        # Aggregations below describe *prompt* events. Stop events contribute
        # to sessions_total + ingestion only (see _ingestion_rate).
        if event.get("kind", "prompt") != "prompt":
            sessions.add(event.get("session_sha256") or "")
            continue
        prompt_events += 1
        sessions.add(event.get("session_sha256") or "")
        if event.get("triage_skipped"):
            triage_skipped += 1
        durations.append(int(event.get("duration_ms") or 0))
        phase_counts[event.get("phase") or "none"] += 1
        for pick in event.get("picks") or []:
            name = pick.get("name")
            if not name:
                continue
            pick_counts[name] += 1
            kind_counts[pick.get("kind") or "unknown"] += 1

    picked_names = set(pick_counts)
    dead = sorted(catalog_names - picked_names)

    durations.sort()
    if durations:
        p50 = durations[len(durations) // 2]
        p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
    else:
        p50 = p95 = 0

    prompts_with_picks, skill_invoked = _ingestion_rate(events)

    return {
        "events_total": prompt_events,
        "sessions_total": len({s for s in sessions if s}),
        "triage_skipped": triage_skipped,
        "top_picks": pick_counts.most_common(),
        "kind_counts": dict(kind_counts.most_common()),
        "phase_counts": dict(phase_counts.most_common()),
        "coverage_picked": len(picked_names & catalog_names),
        "coverage_catalog": len(catalog_names),
        "dead": dead,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "ingestion_prompts_with_picks": prompts_with_picks,
        "ingestion_skill_invoked": skill_invoked,
    }


def _render_report_text(stats: dict, args: argparse.Namespace) -> None:
    print(f"Events: {stats['events_total']} across {stats['sessions_total']} sessions "
          f"(triage-skipped: {stats['triage_skipped']})")
    print()
    total = sum(count for _, count in stats["top_picks"]) or 1
    print(f"Top picks (top {args.top}):")
    for rank, (name, count) in enumerate(stats["top_picks"][:args.top], start=1):
        pct = 100.0 * count / total
        print(f"  {rank:>2}. {name:<42} {count:>5}  {pct:>5.1f}%")
    print()
    if stats["coverage_catalog"]:
        pct = 100.0 * stats["coverage_picked"] / stats["coverage_catalog"]
        dead_count = stats["coverage_catalog"] - stats["coverage_picked"]
        print(f"Catalog coverage: {stats['coverage_picked']} / "
              f"{stats['coverage_catalog']} entries picked at least once ({pct:.1f}%)")
        if not args.dead and dead_count:
            print(f"  Run with --dead to list the {dead_count} unused.")
    print()
    print(f"Latency: p50 {stats['latency_p50_ms']} ms, p95 {stats['latency_p95_ms']} ms")

    denom = stats["ingestion_prompts_with_picks"]
    num = stats["ingestion_skill_invoked"]
    if denom:
        pct = 100.0 * num / denom
        print(f"Ingestion: {num} / {denom} prompts followed by a Skill invocation ({pct:.1f}%)")
    else:
        print("Ingestion: no prompt+stop pairs yet (record_stop telemetry is new).")

    if args.by_phase and stats["phase_counts"]:
        print()
        print("By phase:")
        for phase, count in stats["phase_counts"].items():
            print(f"  {phase:<16} {count}")
    if args.by_kind and stats["kind_counts"]:
        print()
        print("By kind:")
        for kind, count in stats["kind_counts"].items():
            print(f"  {kind:<16} {count}")
    if args.dead and stats["dead"]:
        print()
        print(f"Dead entries ({len(stats['dead'])}):")
        for name in stats["dead"]:
            print(f"  {name}")


def _render_report_csv(stats: dict, args: argparse.Namespace) -> None:
    print("rank,name,count,percent")
    total = sum(count for _, count in stats["top_picks"]) or 1
    for rank, (name, count) in enumerate(stats["top_picks"][:args.top], start=1):
        pct = 100.0 * count / total
        # Quote names with commas.
        safe = name if "," not in name else f'"{name}"'
        print(f"{rank},{safe},{count},{pct:.2f}")


def _render_report_json(stats: dict, args: argparse.Namespace) -> None:
    payload = {
        "events_total": stats["events_total"],
        "sessions_total": stats["sessions_total"],
        "triage_skipped": stats["triage_skipped"],
        "top": [
            {"rank": i, "name": name, "count": count}
            for i, (name, count) in enumerate(stats["top_picks"][:args.top], start=1)
        ],
        "coverage": {
            "picked": stats["coverage_picked"],
            "catalog": stats["coverage_catalog"],
        },
        "latency": {
            "p50_ms": stats["latency_p50_ms"],
            "p95_ms": stats["latency_p95_ms"],
        },
        "by_phase": stats["phase_counts"],
        "by_kind": stats["kind_counts"],
        "ingestion": {
            "prompts_with_picks": stats["ingestion_prompts_with_picks"],
            "skill_invoked": stats["ingestion_skill_invoked"],
        },
    }
    if args.dead:
        payload["dead"] = stats["dead"]
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _cmd_report(args: argparse.Namespace) -> int:
    if args.purge_older_than:
        try:
            duration = telemetry.parse_duration(args.purge_older_than)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        removed = telemetry.purge_older_than(duration)
        print(f"purged {removed} event(s) older than {args.purge_older_than}.")
        return 0

    try:
        since = telemetry.parse_duration(args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc) - since

    if not paths.events_file().is_file():
        print("no telemetry events recorded.")
        print("enable with `events_enabled = true` under [telemetry] in config.toml,")
        print("then run a few prompts through `claudeskill` before reporting again.")
        return 0

    events = list(telemetry.iter_events(cutoff=cutoff))
    if not events:
        print(f"no events in the last {args.since}.")
        return 0

    try:
        catalog_names = {e.name for e in catalog_mod.load()}
    except FileNotFoundError:
        catalog_names = set()

    stats = _report_stats(events, catalog_names)

    if args.format == "json":
        _render_report_json(stats, args)
    elif args.format == "csv":
        _render_report_csv(stats, args)
    else:
        _render_report_text(stats, args)
    return 0


def _rotation_stats(
    events: list[dict], *, catalog: list | None = None
) -> dict[str, dict]:
    """Fold the event log into per-skill picks / invocations / recency.

    Keys are `CatalogEntry.name` (the frontmatter `name:`) — that's what
    `rotate.score_pool()` looks up via `stats.get(entry.name, {})`, NOT
    `invoke_name` (the directory name, or `plugin:dirname`, that Claude
    Code's Skill tool actually accepts). The two event kinds don't agree on
    which one they log natively:

      * `kind=prompt` events' `picks` are already `entry.name`-keyed —
        `telemetry.record()` stores `pick["name"] = entry.name` directly.
      * `kind=stop` events' `skills` list is populated from
        `tool_input.skill` (see `lifecycle.record_tool`), which is
        `invoke_name` — the exact string the Skill tool was invoked with.

    `invoke_name` differs from `name` for any skill whose frontmatter
    `name:` diverges from its directory name (documented in overrides.py's
    module docstring: 7 skills do this on the author's machine, e.g. the
    `xlsx` dir declaring `name: xlsx-official`). Left untranslated, such a
    skill's invocation count and `last_invoked_days` would silently land
    under a key `score_pool()` never looks up — invisible to
    `invocation_rate` (currently zero-weighted, so low stakes today) AND to
    `rotate._shielded()`'s recency check (which reads the same per-entry
    `last_invoked_days` slot picks populate), defeating the "never demote a
    recently-invoked skill" guarantee for exactly the entries most likely to
    need it.

    `catalog`, if given, supplies the `invoke_name -> name` translation for
    `kind=stop` events. Without it (the default), stop-event names are used
    as-is — correct only when every entry's `invoke_name` equals its `name`,
    which is the common case but not a guarantee, so callers that have a
    catalog on hand should pass it.
    """
    from datetime import datetime, timezone

    invoke_to_name: dict[str, str] = {}
    for e in catalog or ():
        invoke_to_name[e.invoke_name or e.name] = e.name

    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for ev in events:
        if ev.get("kind") == "stop":
            for raw_name in ev.get("skills") or []:
                name = invoke_to_name.get(raw_name, raw_name)
                slot = out.setdefault(
                    name, {"picks": 0, "invocations": 0, "last_invoked_days": None}
                )
                slot["invocations"] += 1
                try:
                    ts = datetime.fromisoformat(str(ev["ts"]).replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                days = (now - ts).total_seconds() / 86400.0
                prev = slot["last_invoked_days"]
                slot["last_invoked_days"] = days if prev is None else min(prev, days)
            continue
        for pick in ev.get("picks") or []:
            name = pick.get("name")
            if not name:
                continue
            slot = out.setdefault(
                name, {"picks": 0, "invocations": 0, "last_invoked_days": None}
            )
            slot["picks"] += 1
    return out


_DEFAULT_ROTATE_PRINT_LIMIT = 20


def _rotatable_pool(catalog: list, embeddings):
    """Filter the full catalog down to entries `overrides.override_key()`
    can actually address, in lockstep with their embedding rows.

    Subagents, slash commands, and plugin-namespaced skills are permanently
    enabled — `override_key()` returns `None` for all three (verified live:
    docs/superpowers/notes/2026-07-30-skilloverrides-verification.md, cases C
    and D). `config.catalog.extra_roots` skills are excluded for a different
    reason but the same effect: they're scanned in place from a directory
    Claude Code's own skill discovery never looks at, so `skillOverrides`
    (keyed off that discovery) can't address them either — proposing one
    would be a silent no-op, same class of bug as the plugin-skill exclusion.
    Either way, neither promoting nor demoting one can ever change anything.
    Excluding them HERE, before scoring, rather than only suppressing them at
    print time, matters: a scored-but-unprintable entry would still distort
    `pick_rate`'s max-picks normalisation and the target/active arithmetic in
    `rotate.propose()`, even if the printed proposal hid it. Filtering first
    is also what guarantees `proposal.promote`/`.demote` can never contain an
    entry whose `override_key()` is `None` — the property that makes the
    printed dry run honest about what `--apply` can actually do.

    Returns `(pool_catalog, pool_embeddings, excluded_count, excluded_labels)`.
    `excluded_labels` is the set of human-readable categories actually seen
    among the excluded entries (a subset of {"subagents", "slash commands",
    "plugin skills", "extra-root skills"}) — callers use it to describe the
    exclusion accurately instead of always naming all four regardless of
    what was really excluded.
    """
    keep: list[int] = []
    excluded_labels: set[str] = set()
    for i, e in enumerate(catalog):
        key = overrides.override_key(
            kind=e.kind, namespace=e.namespace, path=e.path, name=e.name
        )
        if key is not None:
            keep.append(i)
            continue
        if e.kind == "subagent":
            excluded_labels.add("subagents")
        elif e.kind == "command":
            excluded_labels.add("slash commands")
        elif e.kind == "skill" and e.namespace.startswith("plugin:"):
            excluded_labels.add("plugin skills")
        elif e.kind == "skill" and e.namespace == "extra":
            excluded_labels.add("extra-root skills")
        else:  # pragma: no cover - defensive; no known catalog shape hits this
            excluded_labels.add("other unrotatable entries")
    pool_catalog = [catalog[i] for i in keep]
    pool_embeddings = embeddings[keep] if keep else embeddings[:0]
    return pool_catalog, pool_embeddings, len(catalog) - len(keep), excluded_labels


def _print_proposal_lines(
    verb: str, noun: str, items: list, reason_by_name: dict, limit: int
) -> None:
    """Print up to `limit` lines for one direction of the proposal.

    `limit <= 0` means show everything. A list longer than what fits is
    still announced explicitly ("… +N more") rather than silently cut — a
    silent cut reads as "that's everything" to the human deciding whether to
    apply this, which is worse than not printing the tail at all.
    """
    shown = items if limit <= 0 else items[:limit]
    for s in shown:
        name = s.entry.invoke_name or s.entry.name
        print(f"  {verb} {name:45} {reason_by_name[s.entry.name]}")
    remaining = len(items) - len(shown)
    if remaining > 0:
        plural = noun if remaining == 1 else f"{noun}s"
        print(f"  … +{remaining} more {plural}")


def _cmd_rotate(args: argparse.Namespace) -> int:
    """Propose (or, with --apply, write) active-skill-set rotation.

    A CLI verb, not a hook path: it fails loudly. `RotationRefused` (cold
    start, or a proposal that would breach `rotation.min_active`) and a
    failed settings write both print a clear message and return a non-zero
    exit code instead of a bare traceback.

    Without --apply this MUST NOT touch the settings file — that is the
    single most important property of this command, since the printed
    proposal is read by a human before they decide whether to change their
    active skill set. Every early-return path above the --apply branch below
    happens before any write is attempted.

    The pool/active/target/sketch line prints unconditionally, before the
    scoring attempt, specifically so that a refusal below is self-explanatory
    without a second run: the reader already has the sketch's observed-prompt
    count et al. in view when the refusal message appears.
    """
    cfg = load_config()
    rot = cfg.rotation
    if args.target is not None:
        rot = dataclasses.replace(rot, target_active=int(args.target))
    if args.limit is not None and args.limit < 0:
        print("error: --limit must be >= 0 (0 shows the full list)", file=sys.stderr)
        return 2
    limit = args.limit if args.limit is not None else _DEFAULT_ROTATE_PRINT_LIMIT

    try:
        idx = index_mod.load()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    # F5: `doctor` re-scans the disk and compares against the hash recorded
    # at the last `build` to report staleness, but `rotate` never did — it
    # loaded catalog.json as-is and scored/wrote against it regardless of
    # whether skills or skillOverrides had changed since. The sharp case is
    # an upgrade: an old catalog.json deserializes with `enabled=True`
    # defaulted for every entry (CatalogEntry.from_json tolerates missing
    # fields), so `rotate --apply` run before `build` sees a fabricated
    # "everything is active" starting point and can propose hundreds of
    # demotions from data that was never real. Unlike `migrate-excludes`,
    # `--apply` here takes no backup, and this is a CLI verb — it fails
    # loudly rather than silently trusting stale state.
    try:
        fresh_hash = catalog_mod.compute_hash(catalog_mod.scan(cfg))
    except Exception as exc:
        print(f"ERROR: catalog scan failed: {exc}")
        return 1
    stored_hash = index_mod.current_hash()
    if stored_hash and stored_hash != fresh_hash:
        print(
            "ERROR: catalog is stale — skills or skillOverrides changed "
            "since the last `skill-advisor build`; refusing to rotate "
            "against outdated data.\n"
            "Run `skill-advisor build`, then re-run `skill-advisor rotate`."
        )
        return 1

    pool_catalog, pool_embeddings, excluded, excluded_labels = _rotatable_pool(
        idx.catalog, idx.embeddings
    )
    pool_names = {e.name for e in pool_catalog}

    sketch = centroids.load(k=rot.centroid_count)
    events = list(telemetry.iter_events())  # cutoff=None: the whole log
    # _rotation_stats() normalises stop-event names to entry.name space (see
    # its docstring), but its output still spans the WHOLE catalog — a
    # subagent, slash command, or plugin skill genuinely accrues picks (they
    # are always `enabled`, so the matcher recommends them like anything
    # else) even though `_rotatable_pool()` above has already excluded them
    # from scoring. Scoping to `pool_names` here, before score_pool() ever
    # sees `stats`, is what stops those picks from inflating max_picks and
    # silently deflating every real skill's pick_rate/total.
    stats = {
        name: s
        for name, s in _rotation_stats(events, catalog=idx.catalog).items()
        if name in pool_names
    }

    active = sum(1 for e in pool_catalog if e.enabled)
    if excluded:
        exclusion_note = (
            f"excluded {excluded} unrotatable ({', '.join(sorted(excluded_labels))} "
            "— not addressable via skillOverrides)"
        )
    else:
        exclusion_note = "excluded 0 unrotatable"
    print(
        f"catalog {len(idx.catalog)} entries · {exclusion_note} · "
        f"pool {len(pool_catalog)} · active {active} · "
        f"target {rot.target_active} · sketch {sketch.observed} prompts"
    )
    if not cfg.telemetry.events_enabled:
        # F3: `events` above is read unconditionally — turning telemetry off
        # stops the hook from RECORDING new prompts (the privacy toggle),
        # it does not erase or blind rotate to an event log collected while
        # telemetry was previously on. Scoring on that historical log is
        # intentional: the data doesn't stop being real just because future
        # collection was disabled, and rotate.score_pool()/_rotation_stats()
        # are that log's only consumer of stale reads. The claim that used
        # to print here — "pick_rate and invocation_rate are unavailable;
        # scoring on semantic_fit alone" — was simply FALSE whenever a
        # pre-existing event log was present: a user reading it would
        # believe usage data played no part, while an incumbent could in
        # fact still be surviving purely on pick_rate=1.0. Reworded to a
        # claim that is actually true, rather than skipping the log, so
        # `rotate`'s scoring behavior is unchanged by this fix.
        print(
            "NOTE: telemetry is off — no new usage data is being recorded; "
            "existing events (if any) are still scored."
        )

    try:
        scored = rotate.score_pool(pool_catalog, pool_embeddings, sketch, stats, rot)
        proposal = rotate.propose(scored, rot)
    except rotate.RotationRefused as exc:
        print(f"rotation refused: {exc}")
        return 1

    if not proposal.promote and not proposal.demote:
        print("no changes proposed")
        return 0

    # Read the pre-proposal active count back from `scored` — the exact list
    # `rotate.propose()` computed `incumbents` from — rather than reusing the
    # `active` variable computed above from `pool_catalog` before scoring.
    # Both count the same thing (enabled entries in the pool) and agree by
    # construction, but this ties the arithmetic to what actually produced
    # the proposal instead of two independently-maintained call sites.
    active_scored = sum(1 for s in scored if s.entry.enabled)
    resulting_active = active_scored + len(proposal.promote) - len(proposal.demote)
    print(
        f"proposal: {len(proposal.promote)} promotion(s), "
        f"{len(proposal.demote)} demotion(s) → {resulting_active} active"
    )
    _print_proposal_lines(
        "PROMOTE", "promotion", proposal.promote, proposal.reason_by_name, limit
    )
    _print_proposal_lines(
        "DEMOTE ", "demotion", proposal.demote, proposal.reason_by_name, limit
    )

    if not args.apply:
        print("\n(dry run — nothing written; rerun with --apply)")
        return 0

    # `overrides.override_key()` returns None for entries skillOverrides
    # cannot address (subagents, slash commands, plugin-namespaced skills —
    # verified live, see
    # docs/superpowers/notes/2026-07-30-skilloverrides-verification.md).
    # `_rotatable_pool()` above already excludes every such entry from
    # scoring, so this can't actually fire today — kept as defense in depth
    # rather than trusting that invariant to hold forever. Skip rather than
    # invent a key: writing a namespaced key would be silently ignored by
    # Claude Code, so `rotate --apply` would report success while changing
    # nothing for that entry.
    updates: dict[str, str] = {}
    for s in proposal.promote:
        key = overrides.override_key(
            kind=s.entry.kind,
            namespace=s.entry.namespace,
            path=s.entry.path,
            name=s.entry.name,
        )
        if key:
            updates[key] = "on"
    for s in proposal.demote:
        key = overrides.override_key(
            kind=s.entry.kind,
            namespace=s.entry.namespace,
            path=s.entry.path,
            name=s.entry.name,
        )
        if key:
            updates[key] = overrides.OFF

    try:
        wrote = overrides.write(updates)
    except ValueError as exc:
        # Defense in depth, not the expected path: override_key() already
        # filters out namespaced keys above, so overrides.write() should
        # never see one to raise on. A CLI verb still fails loudly rather
        # than leaking that ValueError as a bare traceback.
        print(f"ERROR: refused to write skillOverrides: {exc}")
        return 1
    if not wrote:
        print("ERROR: settings write failed; nothing changed")
        return 1
    print(f"\napplied {len(updates)} change(s) to {paths.settings_file()}")
    print("run `skill-advisor build` to rebuild the catalog")
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    cfg = load_config()

    prompt = _resolve_match_prompt(args)
    if prompt is None:
        print(
            "error: provide a prompt as an argument or via stdin (pipe).",
            file=sys.stderr,
        )
        return 2
    if not prompt:
        print("error: empty prompt.", file=sys.stderr)
        return 2

    try:
        idx = index_mod.load()
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "skill-advisor build" not in str(exc):
            print("       run `skill-advisor build` to create the catalog.", file=sys.stderr)
        return 2

    if args.phase:
        limit = args.top_k if args.top_k is not None else cfg.matcher.max_picks
        entries = lifecycle_mod.pick_candidates_for_phase(
            args.phase, idx.catalog, limit=limit, config=cfg.lifecycle
        )
        picks = [
            matcher.ResolvedPick(entry=e, reason=f"{args.phase} phase preference")
            for e in entries
        ]
    else:
        picks = matcher.pick_stateless(
            prompt,
            cfg,
            force_judge=args.judge,
            threshold=args.threshold,
            top_k=args.top_k,
            candidates=args.candidates,
            index=idx,
        ).picks

    triage_skip = triage.should_skip(prompt, cfg) if args.show_triage else None

    if args.json:
        _render_match_json(prompt, picks, triage_skip, verbose=args.match_verbose)
    else:
        _render_match_text(prompt, picks, triage_skip, verbose=args.match_verbose)
    return 0


def _cmd_lifecycle(args: argparse.Namespace) -> int:
    action = args.action
    if action == "status":
        if args.session:
            state = lifecycle_mod.load(args.session)
            if state is None:
                print(f"no lifecycle state for session {args.session!r}")
                return 1
            _print_lifecycle(state)
            return 0
        sessions = lifecycle_mod.list_sessions()
        if not sessions:
            print("no active lifecycle sessions.")
            return 0
        for state in sessions:
            _print_lifecycle(state)
            print()
        return 0

    if action == "reset":
        if args.all:
            d = paths.sessions_dir()
            removed = 0
            if d.is_dir():
                for f in d.glob("*.json"):
                    f.unlink()
                    removed += 1
            print(f"removed {removed} session state file(s).")
            return 0
        if args.session:
            deleted = lifecycle_mod.delete(args.session)
            print(f"{'removed' if deleted else 'no state for'} session {args.session}")
            return 0 if deleted else 1
        print("error: pass --session <id> or --all", file=sys.stderr)
        return 2

    print(f"unknown lifecycle action: {action}", file=sys.stderr)
    return 2


def _print_lifecycle(state: lifecycle_mod.LifecycleState) -> None:
    print(f"session:    {state.session_id}")
    print(f"phase:      {state.phase}")
    print(f"task:       {state.original_prompt[:120]}")
    if state.cycles:
        cycles = ", ".join(f"{k}={v}" for k, v in state.cycles.items())
        print(f"cycles:     {cycles}")
    if state.history:
        last = state.history[-1]
        print(f"last event: {last.get('phase')} — {last.get('note')}")


def _cmd_sync_skills(args: argparse.Namespace) -> int:
    source = sync_mod.locate_bundle(args.from_)
    if source is None:
        print(
            "error: no skills/ source directory found. "
            "Pass --from <path>, set $SKILL_ADVISOR_BUNDLE, "
            "or run from a directory containing a skills/ folder.",
            file=sys.stderr,
        )
        return 2

    report = sync_mod.sync(source=source, force=args.force, dry_run=args.dry_run)
    print(report.summary())

    if args.dry_run:
        if report.copied:
            print("\nWould copy:")
            for name in report.copied[:20]:
                print(f"  + {name}")
            if len(report.copied) > 20:
                print(f"  ... and {len(report.copied) - 20} more")
        if not args.force and report.skipped:
            print(f"\n{len(report.skipped)} existing dir(s) would be skipped (use --force to overwrite).")

    if not args.dry_run and (report.copied or report.overwritten):
        print(
            "\nHint: rebuild the catalog now that skills have moved:\n"
            "  skill-advisor build"
        )
    return 0


_MIGRATE_BACKUP_SUFFIX = ".pre-migrate.bak"
# Marker written next to the settings backup when the settings file did NOT
# exist before this migration wrote it — a small JSON document recording
# exactly the skillOverrides keys THIS migration added, e.g.
# `{"keys": ["alpha", "beta"]}`. Lets --revert tell "restore from backup
# bytes" apart from "no bytes to restore, so undo exactly what I added".
#
# It is NOT a blanket "delete the file" sentinel: the settings file is not
# exclusively ours to delete just because we created it. The ordinary Stop
# hook write-back (`baseline._write_settings_effort`, entirely automatic, no
# user action) and `install.render_settings()` both do their own independent
# read-modify-write of this same file, and can add `effortLevel`,
# `statusLine`, etc. to it after we create it but before a revert. Deleting
# the whole file on revert would destroy that too. So revert instead removes
# exactly the recorded keys from skillOverrides via `overrides.remove_keys()`
# and deletes the file only if what's left is PROVABLY still just our own
# artifact — `skillOverrides` empty and no other top-level key at all.
_MIGRATE_ABSENT_SUFFIX = ".pre-migrate.absent"


def _classify_exclude_names(
    names: list[str], cfg
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Split `exclude_names` into what `skillOverrides` can address and what it can't.

    `skillOverrides` covers only entries whose `overrides.override_key()`
    resolves to a real key — user (and extra-root) *skills*. It has no
    concept of subagents or slash commands at all, and plugin skills resolve
    to None (verified live: a namespaced key is silently ignored). By
    contrast `catalog._accept()` filters `exclude_names` on the bare
    invocable name regardless of kind, so one name can simultaneously mute a
    skill and a same-named subagent. Migrating a name is only safe when
    EVERY catalog entry that name resolves to is skillOverrides-addressable —
    otherwise the migration would silently stop muting whichever isn't.

    Scans with exclude_names cleared: scanning with the live list would
    filter out the very entries this function needs to classify.
    """
    scan_cfg = dataclasses.replace(
        cfg, catalog=dataclasses.replace(cfg.catalog, exclude_names=())
    )
    entries = catalog_mod.scan(scan_cfg)
    by_invocable: dict[str, list] = {}
    for entry in entries:
        invocable = entry.invoke_name or entry.name
        by_invocable.setdefault(invocable, []).append(entry)

    migratable: dict[str, str] = {}
    retained: list[str] = []
    reasons: dict[str, str] = {}
    for name in names:
        matches = by_invocable.get(name, [])
        if not matches:
            retained.append(name)
            reasons[name] = "no catalog entry on disk"
            continue
        blockers = [
            e
            for e in matches
            if overrides.override_key(
                kind=e.kind, namespace=e.namespace, path=e.path, name=e.name
            )
            is None
        ]
        if blockers:
            shapes = sorted({f"{e.kind}/{e.namespace}" for e in blockers})
            retained.append(name)
            reasons[name] = (
                f"not addressable via skillOverrides ({', '.join(shapes)})"
            )
            continue
        migratable[name] = overrides.OFF
    return migratable, retained, reasons


def _rewrite_exclude_names_text(text: str, retained: list[str]) -> str:
    """Rewrite `exclude_names` inside the live `[catalog]` table only.

    Deliberately NOT a first-match-anywhere-in-the-file substitution — that
    would also rewrite a comment, or a table other than `[catalog]`, that
    happens to contain the same text, silently leaving the real list
    unmigrated while the command reports success. `retained` (not `[]`) is
    written back: entries skillOverrides cannot address stay in
    exclude_names rather than being dropped.
    """
    array_text = "[" + ", ".join(json.dumps(n) for n in retained) + "]"
    replacement_line = (
        f"exclude_names = {array_text}"
        "  # curated by `skill-advisor migrate-excludes`;"
        " these are not addressable via skillOverrides"
    )

    table_re = re.compile(r"^\[catalog\]\s*(#.*)?$", re.MULTILINE)
    match = table_re.search(text)
    if match is None:
        prefix = text if (not text or text.endswith("\n")) else text + "\n"
        return prefix + f"\n[catalog]\nexclude_names = {array_text}\n"

    table_start = match.end()
    next_table = re.search(r"^\[", text[table_start:], re.MULTILINE)
    table_end = table_start + next_table.start() if next_table else len(text)

    table_body = text[table_start:table_end]
    # `[^\]]` matches newlines with or without DOTALL — DOTALL only changes
    # what `.` matches, and this pattern has no `.` in it.
    key_re = re.compile(r"^exclude_names\s*=\s*\[[^\]]*\](\s*#.*)?$", re.MULTILINE)
    if key_re.search(table_body):
        new_body = key_re.sub(lambda _m: replacement_line, table_body, count=1)
    else:
        new_body = table_body.rstrip("\n") + f"\n{replacement_line}\n"

    return text[:table_start] + new_body + text[table_end:]


def _write_config_toml(cfg_path: Path, text: str) -> bool:
    """Atomic, syntax-validated config.toml write.

    Same contract as `overrides.write()` / `baseline._write_settings_effort`:
    temp file, validated by re-parsing with `tomllib` before it touches the
    real path, atomic `Path.replace()`, temp unlinked on any failure. Returns
    `False` on any failure, leaving the file byte-identical — a bare
    `write_text()` here would let an interrupted write truncate config.toml,
    and every later `skill-advisor` invocation reads it via `tomllib.loads()`
    with no exception handling, so a truncated file bricks the whole tool,
    not just this command.
    """
    tmp = cfg_path.with_suffix(f".toml.tmp.{os.getpid()}")
    try:
        tomllib.loads(text)  # syntax-validate before it touches the real path
        paths.ensure_dirs()
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(cfg_path)
        return True
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("config.toml write failed: %s", exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _cmd_migrate_excludes(args: argparse.Namespace) -> int:
    """Move catalog.exclude_names into skillOverrides, once — partially.

    Only names that resolve to a skillOverrides-addressable catalog entry (a
    user/extra-root skill) are migrated; see `_classify_exclude_names`.
    Everything skillOverrides cannot reach — subagent names, slash-command
    names, namespaced/plugin entries, and names matching nothing on disk —
    is left in catalog.exclude_names rather than silently losing its mute.

    Deliberate, accepted consequence for what IS migrated: those names become
    rotation-eligible. Some were muted for irrelevance and may come back.
    Hence the backups and the printed revert command — read the first
    `rotate --dry-run` after migrating rather than applying it blind.

    Two safety rails, both because `paths.settings_file()` can silently
    resolve to a file Claude Code never reads (wrong/unset
    `SKILL_ADVISOR_SETTINGS_FILE`):
      * if the resolved settings file doesn't exist yet, `--create-settings`
        is required to proceed — see `_do_migrate_excludes`.
      * if THIS run is the one that creates it, a `.pre-migrate.absent`
        marker records exactly which keys it added, so `--revert` can undo
        precisely that — see the module-level comment on
        `_MIGRATE_ABSENT_SUFFIX` for why it's a surgical key-removal and not
        a "delete the file" sentinel.

    A CLI verb, so it fails loudly: the whole verb is wrapped so any
    unexpected failure (including a `ValueError` from `overrides.write()`,
    which should be unreachable now that only addressable names are ever
    forwarded to it, but is defended against regardless) is reported instead
    of a bare traceback, and the message reminds the user `--revert` exists.
    """
    cfg_path = paths.config_file()
    settings_path = paths.settings_file()
    cfg_bak = cfg_path.with_name(cfg_path.name + _MIGRATE_BACKUP_SUFFIX)
    settings_bak = settings_path.with_name(settings_path.name + _MIGRATE_BACKUP_SUFFIX)
    settings_absent = settings_path.with_name(
        settings_path.name + _MIGRATE_ABSENT_SUFFIX
    )

    if args.revert:
        restored = []
        if cfg_bak.is_file():
            cfg_path.write_bytes(cfg_bak.read_bytes())
            cfg_bak.unlink()
            restored.append(str(cfg_path))
        if settings_bak.is_file():
            settings_path.write_bytes(settings_bak.read_bytes())
            settings_bak.unlink()
            restored.append(str(settings_path))
        elif settings_absent.is_file():
            outcome = _revert_absent_settings(settings_path, settings_absent)
            if outcome is None:
                return 1
            if outcome:
                restored.append(outcome)
        if not restored:
            print("nothing to revert: no .pre-migrate.bak files found")
            return 1
        print("restored:\n  " + "\n  ".join(restored))
        return 0

    try:
        return _do_migrate_excludes(
            cfg_path, settings_path, cfg_bak, settings_bak, settings_absent, args
        )
    except Exception as exc:
        print(f"ERROR: migrate-excludes failed: {exc}")
        present = [p for p in (cfg_bak, settings_bak, settings_absent) if p.is_file()]
        if present:
            print("Backups from this run may still be present:")
            for p in present:
                print(f"  {p}")
            print("Revert with: skill-advisor migrate-excludes --revert")
        return 1


def _revert_absent_settings(settings_path: Path, settings_absent: Path) -> str | None:
    """Undo exactly what a migration added when it created `settings_path`
    from nothing, using the `.pre-migrate.absent` marker's recorded keys.

    Returns a human-readable description of what happened, for `restored`;
    `""`/falsy if there was nothing to do (the file is already gone); `None`
    on a hard failure (already reported), signalling the caller to exit 1
    without unlinking the marker, so a retry remains possible.

    Deletes `settings_path` only when what remains after removing exactly
    the recorded keys is provably still nothing but this migration's own
    artifact — an empty `skillOverrides` and no other top-level key. If
    anything else is present (added by the Stop hook's effortLevel
    write-back, `install.render_settings()`, a hand-added key, ...) the file
    is kept and the message says so plainly.

    A corrupt or wrong-shaped marker is a hard failure, not a silent
    "nothing to remove": this function used to fall back to an empty key
    list in that case and let the caller print "restored:" with exit 0 — a
    revert that touched nothing while reporting success. A caller checking
    only the exit code would believe the migrated `off` entries were gone
    when they were not. Better to fail loudly and leave the marker in place
    for a retry than to silently under-deliver.
    """
    try:
        raw = settings_absent.read_text(encoding="utf-8")
        marker = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"ERROR: {settings_absent} is corrupt ({exc}); cannot tell which "
            "skillOverrides keys this migration added, so nothing was "
            f"changed. Inspect {settings_absent} by hand (or remove it, "
            "accepting the loss) and retry --revert."
        )
        return None
    if not isinstance(marker, dict) or not isinstance(marker.get("keys"), list):
        print(
            f"ERROR: {settings_absent} does not have the expected "
            '{"keys": [...]} shape; cannot tell which skillOverrides keys '
            f"this migration added, so nothing was changed. Inspect "
            f"{settings_absent} by hand and retry --revert."
        )
        return None
    migrated_keys = list(marker["keys"])

    if not settings_path.is_file():
        # Already gone (deleted independently of this tool) — nothing to
        # revert here beyond clearing the now-moot marker.
        settings_absent.unlink()
        return ""

    if not overrides.remove_keys(migrated_keys, settings_path=settings_path):
        print(
            f"ERROR: could not update {settings_path} while reverting its "
            "migrated skillOverrides keys; left unchanged. The marker is "
            "kept so --revert can be retried."
        )
        return None

    try:
        remaining = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        remaining = None
    only_ours = (
        isinstance(remaining, dict)
        and not remaining.get("skillOverrides")
        and set(remaining.keys()) <= {"skillOverrides"}
    )

    settings_absent.unlink()
    if only_ours:
        settings_path.unlink()
        return f"{settings_path} (removed — nothing else had been written to it)"

    removed_desc = ", ".join(migrated_keys) if migrated_keys else "(none recorded)"
    return (
        f"{settings_path} (kept — another writer had added to it since "
        f"migration; removed migrated keys: {removed_desc})"
    )


def _do_migrate_excludes(
    cfg_path: Path,
    settings_path: Path,
    cfg_bak: Path,
    settings_bak: Path,
    settings_absent: Path,
    args: argparse.Namespace,
) -> int:
    cfg = load_config()
    names = list(cfg.catalog.exclude_names)
    if not names:
        print("catalog.exclude_names is already empty; nothing to migrate")
        return 0

    migratable, retained, reasons = _classify_exclude_names(names, cfg)

    if not migratable:
        print(
            f"nothing migratable: none of the {len(names)} exclude_names "
            "entries are addressable via skillOverrides (subagents, slash "
            "commands, namespaced/plugin entries, and names matching "
            "nothing on disk can't be). config.toml is unchanged."
        )
        for name in names:
            print(f"  retained: {name} — {reasons[name]}")
        return 0

    if not settings_path.is_file() and not args.create_settings:
        print(f"ERROR: settings file does not exist: {settings_path}")
        print(
            "A missing target usually means SKILL_ADVISOR_SETTINGS_FILE isn't "
            "pointed at the file your `claude --settings ...` invocation "
            "actually reads — writing skillOverrides here would be silently "
            "inert (see paths.settings_file()'s own docstring on this trap)."
        )
        print(
            "If a fresh settings file at this exact path is intentional (e.g. "
            "a brand-new install), re-run with --create-settings."
        )
        return 1

    paths.ensure_dirs()
    if cfg_path.is_file():
        cfg_bak.write_bytes(cfg_path.read_bytes())

    if settings_path.is_file():
        settings_bak.write_bytes(settings_path.read_bytes())
        if settings_absent.is_file():
            settings_absent.unlink()
    else:
        # Record exactly which keys THIS migration is about to add, so
        # --revert can remove precisely those later rather than assuming the
        # whole file is safe to delete (see the module-level comment on
        # _MIGRATE_ABSENT_SUFFIX).
        settings_absent.write_text(
            json.dumps({"keys": sorted(migratable)}) + "\n", encoding="utf-8"
        )
        if settings_bak.is_file():
            settings_bak.unlink()

    try:
        wrote = overrides.write(migratable)
    except ValueError as exc:
        print(f"ERROR: refused to write skillOverrides: {exc}")
        print("config.toml left unchanged.")
        return 1
    if not wrote:
        print("ERROR: could not write skillOverrides; config.toml left unchanged")
        return 1

    text = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
    new_text = _rewrite_exclude_names_text(text, retained)
    if not _write_config_toml(cfg_path, new_text):
        print(
            "ERROR: could not write config.toml; skillOverrides was already "
            "updated with the following entries, but config.toml is unchanged:"
        )
        for name in migratable:
            print(f"  {name}")
        print(f"config.toml backup: {cfg_bak}")
        print("Revert with: skill-advisor migrate-excludes --revert")
        return 1

    print(f"migrated {len(migratable)} of {len(names)} names into skillOverrides:")
    for name in migratable:
        print(f"  {name}")
    if retained:
        print(
            f"retained {len(retained)} name(s) in catalog.exclude_names "
            "(skillOverrides cannot address them):"
        )
        for name in retained:
            print(f"  {name} — {reasons[name]}")
    settings_backup_note = (
        str(settings_bak)
        if settings_bak.is_file()
        else (
            f"{settings_absent} (marker — settings file didn't exist; --revert "
            "removes just these keys, deleting the file only if nothing else "
            "was written to it since)"
        )
    )
    print(f"backups: {cfg_bak}\n         {settings_backup_note}")
    print("revert with: skill-advisor migrate-excludes --revert")
    print(
        "NEXT: run `skill-advisor build`, then read `skill-advisor rotate` "
        "carefully before applying — the migrated names are now "
        "rotation-eligible. Retained names are unaffected and still muted "
        "via exclude_names."
    )
    return 0


def _detect_existing_claudeskill() -> str | None:
    """Return a short description of any existing `claudeskill` wrapper, or None."""
    import os
    # 1. Any binary on PATH?
    on_path = shutil.which("claudeskill")
    if on_path:
        return f"binary at {on_path}"
    # 2. Ask the user's login shell whether it defines `claudeskill`.
    user_shell = os.environ.get("SHELL", "")
    if user_shell:
        try:
            import subprocess
            out = subprocess.run(
                [user_shell, "-i", "-c", "type -a claudeskill 2>&1 || true"],
                capture_output=True, text=True, timeout=3,
            )
            text = (out.stdout or "") + (out.stderr or "")
            if "not found" not in text and text.strip():
                first = next((ln for ln in text.splitlines() if ln.strip()), "")
                if first:
                    return first.strip()
        except Exception:
            pass
    return None


def _print_activation_hint(
    settings_path: "Path",
    *,
    alias_written: bool,
    shell: "install_mod.ShellTarget | None",
) -> None:
    import os
    existing = _detect_existing_claudeskill()
    claude_config = os.environ.get("CLAUDE_CONFIG_DIR")
    print()
    print("To activate the advisor, pass --settings to `claude`:")
    print(f'  claude --settings "{settings_path}"')

    if alias_written and shell is not None:
        print()
        print(f"An alias was written to {shell.rc_file}.")
        print(f"Reload it:   source {shell.rc_file}")
        print("Then run:    claudeskill")

    if existing:
        print()
        print(f"Detected an existing `claudeskill`: {existing}")
        print("To preserve its behavior (e.g. a custom CLAUDE_CONFIG_DIR),")
        print("add the --settings flag to that wrapper rather than letting this")
        print("command overwrite it. Example patch for a shell function or script:")
        print()
        print(f'    claude --settings "{settings_path}" "$@"')

    if claude_config:
        print()
        print(
            f"Heads-up: CLAUDE_CONFIG_DIR is set to {claude_config}. The advisor's "
            "catalog will scan that directory's `skills/` first, falling back to "
            "$HOME/.claude/skills if the primary is empty."
        )

    print()
    print(f"Customize: $EDITOR {paths.config_file()}")
    print("Rebuild after adding skills: skill-advisor build")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-advisor", description="Silent skill/agent router for Claude Code.")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser(
        "install",
        help="render settings file + build catalog; prints the --settings flag to add to your claude launcher",
    )
    p_install.add_argument("--no-build", action="store_true", help="skip the first catalog build")
    p_install.add_argument(
        "--write-alias",
        action="store_true",
        help=(
            "also append a `claudeskill` alias to your shell rc. Off by default so "
            "this command is safe for users who already have a custom claudeskill "
            "wrapper (e.g. one that sets CLAUDE_CONFIG_DIR for work/private splits)."
        ),
    )
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="remove alias and per-user state")
    p_uninstall.add_argument("--purge-config", action="store_true", help="also delete config.toml")
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_build = sub.add_parser("build", help="rebuild catalog + embeddings")
    p_build.add_argument("--force", action="store_true", help="rebuild even if the catalog hash is unchanged")
    p_build.set_defaults(func=_cmd_build)

    p_replay = sub.add_parser("replay", help="replay prompts through the full pipeline")
    p_replay.add_argument("prompts", help="path to prompts file (one prompt per line, or JSONL with a 'prompt' field)")
    p_replay.set_defaults(func=_cmd_replay)

    p_doctor = sub.add_parser("doctor", help="diagnose the install")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_hook = sub.add_parser("hook", help="UserPromptSubmit hook entry point (invoked by Claude Code)")
    p_hook.set_defaults(func=_cmd_hook)

    p_posttool = sub.add_parser(
        "posttooluse",
        help="PostToolUse hook entry point — records tool usage for lifecycle auto-advance",
    )
    p_posttool.set_defaults(func=_cmd_posttooluse)

    p_stop = sub.add_parser(
        "stop",
        help="Stop hook entry point — applies lifecycle auto-advance rules",
    )
    p_stop.set_defaults(func=_cmd_stop)

    p_match = sub.add_parser(
        "match",
        help="evaluate the matcher against a single prompt and print ranked picks",
    )
    p_match.add_argument(
        "prompt",
        nargs="?",
        help="prompt text; if omitted, read from stdin (only when stdin is piped)",
    )
    p_match.add_argument(
        "-n", "--top-k", dest="top_k", type=int, default=None,
        help="picks to print (default: matcher.max_picks from config)",
    )
    p_match.add_argument(
        "-k", "--candidates", type=int, default=None,
        help="candidate shortlist size (default: matcher.max_candidates from config)",
    )
    p_match.add_argument(
        "--threshold", type=float, default=None,
        help="override min_embedding_score for this call",
    )
    p_match.add_argument(
        "--judge", action=argparse.BooleanOptionalAction, default=None,
        help="force judge on/off (default: follow config.matcher.use_judge)",
    )
    p_match.add_argument(
        "--phase", choices=_PHASES,
        help="evaluate lifecycle phase preferences instead of matching against the prompt",
    )
    p_match.add_argument(
        "--show-triage", action="store_true",
        help="also print whether the hook's triage layer would skip this prompt",
    )
    p_match.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p_match.add_argument(
        "--verbose", dest="match_verbose", action="store_true",
        help="per-pick: also show namespace, description, and catalog path",
    )
    p_match.set_defaults(func=_cmd_match)

    p_lc = sub.add_parser("lifecycle", help="inspect or reset per-session lifecycle state")
    lc_sub = p_lc.add_subparsers(dest="action", required=True)

    p_lc_status = lc_sub.add_parser("status", help="show active lifecycle sessions")
    p_lc_status.add_argument("--session", help="only show this session id")
    p_lc_status.set_defaults(func=_cmd_lifecycle)

    p_lc_reset = lc_sub.add_parser("reset", help="delete lifecycle state for one session or all")
    p_lc_reset.add_argument("--session", help="session id to delete")
    p_lc_reset.add_argument("--all", action="store_true", help="delete every session state file")
    p_lc_reset.set_defaults(func=_cmd_lifecycle)

    p_report = sub.add_parser(
        "report",
        help="summarize pick frequency, dead skills, and latency from advisor.events.jsonl",
    )
    p_report.add_argument("--since", default="30d",
                          help="time window (e.g. 7d, 24h, 2w; default: 30d)")
    p_report.add_argument("--top", type=int, default=20,
                          help="show the top N most-picked entries (default: 20)")
    p_report.add_argument("--dead", action="store_true",
                          help="also list catalog entries with zero picks in the window")
    p_report.add_argument("--by-phase", dest="by_phase", action="store_true",
                          help="group a pick count per lifecycle phase")
    p_report.add_argument("--by-kind", dest="by_kind", action="store_true",
                          help="group a pick count per entry kind (skill/subagent/command)")
    p_report.add_argument("--format", choices=("table", "csv", "json"), default="table",
                          help="output format (default: table)")
    p_report.add_argument("--purge-older-than", dest="purge_older_than", default=None,
                          help="delete events older than this duration and exit (e.g. 90d)")
    p_report.set_defaults(func=_cmd_report)

    p_sync = sub.add_parser(
        "sync-skills",
        help="copy an external library of SKILL.md folders into ~/.claude/skills/ so Claude Code can invoke them",
    )
    p_sync.add_argument("--from", dest="from_", help="path to a directory containing SKILL.md folders (overrides auto-detection)")
    p_sync.add_argument("--force", action="store_true", help="overwrite existing skill directories in ~/.claude/skills")
    p_sync.add_argument("--dry-run", action="store_true", help="print what would happen without copying")
    p_sync.set_defaults(func=_cmd_sync_skills)

    p_migrate = sub.add_parser(
        "migrate-excludes",
        help="move catalog.exclude_names into skillOverrides (one-time, reversible)",
    )
    p_migrate.add_argument("--revert", action="store_true", help="restore the pre-migration backups")
    p_migrate.add_argument(
        "--create-settings",
        action="store_true",
        help=(
            "allow creating the settings file at the resolved "
            "SKILL_ADVISOR_SETTINGS_FILE path if it doesn't exist yet. "
            "Required so a wrong/unset env var can't silently write "
            "skillOverrides to a file Claude Code never reads."
        ),
    )
    p_migrate.set_defaults(func=_cmd_migrate_excludes)

    p_rotate = sub.add_parser(
        "rotate",
        help="propose (or apply) active-skill-set rotation based on semantic fit and usage",
    )
    p_rotate.add_argument(
        "--apply", action="store_true", help="write the proposal to the settings file"
    )
    p_rotate.add_argument(
        "--target",
        type=int,
        default=None,
        help="override rotation.target_active for this run",
    )
    p_rotate.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            f"max PROMOTE/DEMOTE lines to print per direction "
            f"(default {_DEFAULT_ROTATE_PRINT_LIMIT}; 0 = show all)"
        ),
    )
    p_rotate.set_defaults(func=_cmd_rotate)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
