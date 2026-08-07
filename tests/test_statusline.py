import json
import os
import shutil
import subprocess

import pytest

from skill_advisor import paths, statusline

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _run(payload: dict) -> str:
    script = statusline.write_script()
    env = dict(os.environ)
    env["SKILL_ADVISOR_CACHE_HOME"] = str(paths.cache_dir())
    out = subprocess.run(
        ["sh", str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, out.stderr
    assert out.stderr == "", out.stderr
    return out.stdout.strip()


def _payload(level="high", **kw):
    base = {
        "session_id": "sess-1",
        "effort": {"level": level},
        "model": {"display_name": "Opus"},
        "context_window": {"used_percentage": 34.2},
    }
    base.update(kw)
    return base


def test_renders_observed_level_and_model():
    out = _run(_payload())
    assert "high" in out
    assert "Opus" in out


def test_renders_arrow_on_disagreement():
    paths.ensure_dirs()
    paths.effort_file().write_text(json.dumps({"level": "xhigh"}), encoding="utf-8")
    out = _run(_payload(level="medium"))
    assert "medium" in out and "xhigh" in out
    assert "→" in out


def test_no_arrow_on_agreement():
    paths.ensure_dirs()
    paths.effort_file().write_text(json.dumps({"level": "high"}), encoding="utf-8")
    out = _run(_payload(level="high"))
    assert "→" not in out


def test_writes_sensor_file():
    _run(_payload(level="xhigh"))
    data = json.loads(paths.observed_effort_file().read_text(encoding="utf-8"))
    assert data["level"] == "xhigh"
    assert data["session_id"] == "sess-1"


def test_absent_effort_key_is_survivable():
    """Models without reasoning-effort support omit the key entirely."""
    payload = _payload()
    del payload["effort"]
    out = _run(payload)
    assert "Opus" in out
    assert "→" not in out


def test_malformed_stdin_exits_zero_silently():
    script = statusline.write_script()
    env = dict(os.environ)
    env["SKILL_ADVISOR_CACHE_HOME"] = str(paths.cache_dir())
    out = subprocess.run(
        ["sh", str(script)], input="not json{", capture_output=True, text=True, env=env
    )
    assert out.returncode == 0


def test_script_is_executable():
    script = statusline.write_script()
    assert os.access(script, os.X_OK)


# --- Fix round 1 regression coverage ---


def test_percent_sign_survives_and_no_stderr():
    """FINDING 1: `$out` used as printf's FORMAT string treats any `%` in it
    (e.g. from `ctx 34%`) as a conversion directive, corrupting output and
    writing to stderr on every render. The literal text, not just loose
    membership, must appear — and `_run` itself now asserts empty stderr."""
    out = _run(_payload())
    assert "ctx 34%" in out


def test_sensor_file_survives_adversarial_session_id():
    """FINDING 2: the sensor JSON was hand-built with %s substitution and no
    escaping. A session_id containing a quote and a backslash must still
    round-trip through valid JSON."""
    payload = _payload(session_id='weird"id\\here')
    _run(payload)
    data = json.loads(paths.observed_effort_file().read_text(encoding="utf-8"))
    assert data["session_id"] == 'weird"id\\here'
    assert data["level"] == "high"


def _run_ctx_payload(used_percentage):
    """Run the script with a specific (possibly malformed) ctx value, without
    the strict `_run()` stderr assertion, so tests can inspect the raw
    stdout/stderr themselves."""
    script = statusline.write_script()
    env = dict(os.environ)
    env["SKILL_ADVISOR_CACHE_HOME"] = str(paths.cache_dir())
    payload = _payload()
    payload["context_window"]["used_percentage"] = used_percentage
    return subprocess.run(
        ["sh", str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )


def test_non_numeric_ctx_is_dropped_not_errored():
    """FINDING 3: an unvalidated non-numeric `used_percentage` must not reach
    `printf '%.0f'` — the ctx segment is simply omitted, silently."""
    out = _run_ctx_payload("not-a-number")
    assert out.returncode == 0
    assert out.stderr == ""
    assert "ctx" not in out.stdout


def test_ctx_multiple_dots_is_dropped_not_errored():
    """FINDING 3 (round 2): the round-1 guard `''|*[!0-9.]*` only checked the
    character SET, not numeric well-formedness. `34.2.5` contains nothing but
    digits and dots, so it passed the guard and still reached
    `printf '%.0f'`, which refuses to convert it and leaks
    `printf: 34.2.5: not completely converted` to stderr."""
    out = _run_ctx_payload("34.2.5")
    assert out.returncode == 0
    assert out.stderr == ""
    assert "ctx" not in out.stdout


def test_ctx_lone_dot_is_dropped_not_errored():
    """A bare `.` also passes the round-1 character-set guard (it contains
    only `.`) but has no digits at all; printf rejects it with `expected
    numeric value`. Found while fixing round 2's multi-dot case — same class
    of bug, same fix."""
    out = _run_ctx_payload(".")
    assert out.returncode == 0
    assert out.stderr == ""
    assert "ctx" not in out.stdout


def test_ctx_leading_dot_renders_without_stderr():
    """`.5` is well-formed (a single dot, at least one digit) — it must
    render cleanly with no stderr, whichever way the digit rounds."""
    out = _run_ctx_payload(".5")
    assert out.returncode == 0
    assert out.stderr == ""


def test_ctx_trailing_dot_renders_without_stderr():
    """`5.` is likewise well-formed and must render cleanly with no stderr."""
    out = _run_ctx_payload("5.")
    assert out.returncode == 0
    assert out.stderr == ""


def test_unwritable_cache_dir_does_not_leak_stderr(tmp_path):
    """FINDING 4: when the cache dir cannot be created, the shell's own
    redirection-setup failure (not the redirected command's stderr) must not
    leak. The whole sensor write is guarded on `mkdir -p` succeeding first."""
    script = statusline.write_script()
    blocker = tmp_path / "blocker-is-a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    env = dict(os.environ)
    env["SKILL_ADVISOR_CACHE_HOME"] = str(blocker / "cache")
    out = subprocess.run(
        ["sh", str(script)],
        input=json.dumps(_payload(level="xhigh")),
        capture_output=True,
        text=True,
        env=env,
    )
    assert out.returncode == 0
    assert out.stderr == ""


def test_missing_jq_exits_zero_silently(tmp_path):
    """FINDING 5: the jq-absent contract was never exercised because the
    whole file only skips when jq is genuinely absent from the test host."""
    script = statusline.write_script()
    sh_path = shutil.which("sh")
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    env = dict(os.environ)
    env["SKILL_ADVISOR_CACHE_HOME"] = str(paths.cache_dir())
    env["PATH"] = str(empty_bin)
    out = subprocess.run(
        [sh_path, str(script)],
        input=json.dumps(_payload()),
        capture_output=True,
        text=True,
        env=env,
    )
    assert out.returncode == 0
    assert out.stdout == ""
    assert out.stderr == ""
