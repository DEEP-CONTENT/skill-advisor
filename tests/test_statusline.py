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
