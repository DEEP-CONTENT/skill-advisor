"""`skill-advisor` CLI dispatcher."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import shutil as _shutil  # for sessions dir cleanup

from . import catalog as catalog_mod
from . import hook as hook_mod
from . import index as index_mod
from . import install as install_mod
from . import lifecycle as lifecycle_mod
from . import matcher, paths, telemetry, triage
from . import sync as sync_mod
from .config import load as load_config


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
        picks = matcher.pick(prompt, cfg)
        elapsed = time.monotonic() - started
        durations.append(elapsed)

        if picks:
            rendered = ", ".join(f"{p.entry.name} ({p.entry.kind})" for p in picks)
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
            report.append(("catalog", f"{len(entries)} entries"))
        except Exception as exc:
            report.append(("catalog", f"broken: {exc}"))
    else:
        report.append(("catalog", "not built (run `skill-advisor build`)"))

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
        f"catalog        : {health['pool']} in pool, {health['pickable']} pickable"
    )
    if health["unparseable"]:
        shown = ", ".join(health["unparseable"][:5])
        more = len(health["unparseable"]) - 5
        suffix = f", +{more} more" if more > 0 else ""
        print(
            f"WARN: {len(health['unparseable'])} SKILL.md files are unparseable "
            f"(no YAML frontmatter) and invisible to the advisor: {shown}{suffix}"
        )
    if health["excluded_but_enabled"]:
        print(
            f"NOTE: {len(health['excluded_but_enabled'])} names in "
            f"catalog.exclude_names are enabled in Claude Code — muted in the "
            f"advisor but invocable. `skill-advisor migrate-excludes` reconciles this."
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
        width_kind = max(len(p.entry.kind) for p in picks)
        width_name = max(len(p.entry.name) for p in picks)
        for rank, pick in enumerate(picks, start=1):
            line = (
                f"  {rank}. {pick.entry.kind:<{width_kind}}  "
                f"{pick.entry.name:<{width_name}}  {pick.reason}"
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
                "name": p.entry.name,
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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
