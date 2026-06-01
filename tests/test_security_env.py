"""SECURITY (a) — scrubbed minimal subprocess env (ADR-0001).

`cad_generate` runs LLM-authored Python in a subprocess. Today both subprocess
chokepoints inherit the FULL parent environment, leaking OPENROUTER_API_KEY and
every other secret to model code. These tests assert that:

  - core._scrubbed_env() returns only allowlisted keys (PATH/HOME/DISPLAY/LANG/
    LC_*/CAD_OUT + render knobs), never arbitrary secrets;
  - generate/render/measure/review all pass a scrubbed env to subprocess.run, so
    a secret in os.environ is NOT visible to the child;
  - CAD_OUT is injected into generate's child env (the one var it DOES need).

All mocked — no CAD venv needed. The real end-to-end "secret invisible to
generated code" proof lives in test_integration.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_text_to_cad.core as core  # noqa: E402

SECRET_KEYS = [
    "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN", "HF_TOKEN", "SOME_RANDOM_SECRET", "DATABASE_URL",
]


class CompletedStub:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _set_secrets(monkeypatch):
    for k in SECRET_KEYS:
        monkeypatch.setenv(k, f"sentinel-{k}")
    # allowlisted essentials present so we can assert they survive
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")


# ---- _scrubbed_env --------------------------------------------------------------

def test_scrubbed_env_drops_all_secrets(monkeypatch):
    _set_secrets(monkeypatch)
    env = core._scrubbed_env()
    for k in SECRET_KEYS:
        assert k not in env, f"{k} leaked into scrubbed env"


def test_scrubbed_env_keeps_allowlisted_essentials(monkeypatch):
    _set_secrets(monkeypatch)
    env = core._scrubbed_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/tester"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "en_US.UTF-8"   # LC_* prefix allowlisted


def test_scrubbed_env_extra_overrides_and_adds(monkeypatch):
    _set_secrets(monkeypatch)
    env = core._scrubbed_env(extra={"CAD_OUT": "/tmp/out", "PATH": "/override"})
    assert env["CAD_OUT"] == "/tmp/out"
    assert env["PATH"] == "/override"


def test_scrubbed_env_does_not_leak_hermes_cad_python(monkeypatch):
    # HERMES_CAD_PYTHON is read in the PARENT to find the interpreter; the child
    # never needs it, so it must not ride along (ADR-0001).
    _set_secrets(monkeypatch)
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/some/py")
    assert "HERMES_CAD_PYTHON" not in core._scrubbed_env()


def test_scrubbed_env_preserves_display_for_render(monkeypatch):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/home/tester/.Xauthority")
    env = core._scrubbed_env()
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/home/tester/.Xauthority"


# ---- generate passes a scrubbed env ---------------------------------------------

def test_generate_subprocess_env_has_no_secrets(tmp_path, monkeypatch):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")

    def se(args, **kwargs):
        cad_out = Path(kwargs["env"]["CAD_OUT"])
        stem = Path(args[1]).name[: -len("_gen.py")]
        (cad_out / f"{stem}.stl").write_bytes(b"solid x\nendsolid x\n")
        return CompletedStub(0, "", "")

    with mock.patch.object(core.subprocess, "run", side_effect=se) as m:
        core.generate(code="import cadquery as cq\n", out_dir=str(tmp_path), stem="part")

    env = m.call_args.kwargs["env"]
    for k in SECRET_KEYS:
        assert k not in env, f"{k} leaked into generate subprocess"
    assert env["CAD_OUT"] == str(tmp_path)   # the one var generate DOES inject
    assert "PATH" in env                       # essentials survive


# ---- render / measure / review pass a scrubbed env ------------------------------

def test_render_subprocess_env_has_no_secrets(tmp_path, monkeypatch):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    montage = tmp_path / "m.png"
    montage.write_bytes(b"\x89PNG")
    with mock.patch.object(core.subprocess, "run",
                           return_value=CompletedStub(0, str(montage), "backend=vtk")) as m:
        core.render(stl=str(tmp_path / "p.stl"))
    env = m.call_args.kwargs["env"]
    for k in SECRET_KEYS:
        assert k not in env


def test_measure_subprocess_env_has_no_secrets(monkeypatch):
    _set_secrets(monkeypatch)
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with mock.patch.object(core.subprocess, "run",
                           return_value=CompletedStub(0, '{"measured": {}}', "")) as m:
        core.measure(stl="/x/part.stl")
    env = m.call_args.kwargs["env"]
    for k in SECRET_KEYS:
        assert k not in env


def test_review_subprocess_env_has_no_secrets(monkeypatch):
    # The vision gate reads OPENROUTER_API_KEY from ~/.hermes/.env ITSELF, so the
    # key must NOT travel through the subprocess env either (ADR-0001).
    _set_secrets(monkeypatch)
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with mock.patch.object(core.subprocess, "run",
                           return_value=CompletedStub(0, "reviews", "")) as m:
        core.review(montage="/x/m.png", spec_path="/x/spec.json")
    env = m.call_args.kwargs["env"]
    assert "OPENROUTER_API_KEY" not in env
