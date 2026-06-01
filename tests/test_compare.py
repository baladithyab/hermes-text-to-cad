"""Wave 2.2 — cad_compare Chamfer Distance (ADR-0006), core wrapper (mocked).

core.compare(generated, reference, ...) shells out to scripts/compare.py in the
CAD venv and parses the JSON report. These tests mock subprocess.run to assert
argv shape and parsing — no CAD stack needed. The real CD-monotonicity proof is
in test_integration.py.
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


class CompletedStub:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(returncode=0, stdout="", stderr=""):
    return mock.patch.object(core.subprocess, "run",
                             return_value=CompletedStub(returncode, stdout, stderr))


def test_compare_builds_argv(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    report = '{"chamfer_distance": 0.0, "forward": 0.0, "backward": 0.0, "normalized": 0.0, "samples": 4096}'
    with _patch_run(returncode=0, stdout=report) as m:
        res = core.compare(generated="/x/gen.stl", reference="/x/ref.stl")
    argv = m.call_args.args[0]
    assert argv[0] == "/fake/py"
    assert argv[1].endswith("scripts/compare.py")
    assert argv[2] == "/x/gen.stl"
    assert argv[3] == "/x/ref.stl"
    assert res["success"] is True
    assert res["chamfer_distance"] == 0.0
    assert res["report"]["samples"] == 4096


def test_compare_passes_samples_flag(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with _patch_run(returncode=0, stdout='{"chamfer_distance": 1.0}') as m:
        core.compare(generated="/x/g.stl", reference="/x/r.stl", samples=1024)
    argv = m.call_args.args[0]
    assert "--samples" in argv and argv[argv.index("--samples") + 1] == "1024"


def test_compare_env_is_scrubbed(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    with _patch_run(returncode=0, stdout='{"chamfer_distance": 0.0}') as m:
        core.compare(generated="/x/g.stl", reference="/x/r.stl")
    env = m.call_args.kwargs["env"]
    assert "OPENROUTER_API_KEY" not in env   # routed through _scrubbed_env (ADR-0001)


def test_compare_failure_nonzero_exit(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with _patch_run(returncode=1, stdout="", stderr="trimesh load failed"):
        res = core.compare(generated="/x/g.stl", reference="/x/missing.stl")
    assert res["success"] is False
    assert res["chamfer_distance"] is None


def test_compare_nonjson_stdout_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with _patch_run(returncode=0, stdout="not json", stderr="warn"):
        res = core.compare(generated="/x/g.stl", reference="/x/r.stl")
    assert res["success"] is False
    assert "raw" in res["report"]


# ---- final-review fixes: non-finite CD + samples guard -----------------------

@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN"])
def test_compare_rejects_non_finite_cd(monkeypatch, bad):
    # final-review MEDIUM: a NaN/Inf chamfer_distance must NOT pass the success
    # check (it would otherwise look like a valid score).
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    report = '{"chamfer_distance": %s, "normalized": %s}' % (bad, bad)
    with _patch_run(returncode=0, stdout=report):
        res = core.compare(generated="/x/g.stl", reference="/x/r.stl")
    assert res["success"] is False
    assert res["chamfer_distance"] is None


def test_compare_finite_cd_still_succeeds(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with _patch_run(returncode=0, stdout='{"chamfer_distance": 2.5, "normalized": 0.1}'):
        res = core.compare(generated="/x/g.stl", reference="/x/r.stl")
    assert res["success"] is True
    assert res["chamfer_distance"] == 2.5


def test_compare_module_rejects_nonpositive_samples():
    # final-review MEDIUM: samples<=0 must error (exit 2), not produce inf/nan.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cad_compare_script", REPO_ROOT / "scripts" / "compare.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_validate_samples")
    with pytest.raises((ValueError, SystemExit)):
        mod._validate_samples(0)
    with pytest.raises((ValueError, SystemExit)):
        mod._validate_samples(-5)
    assert mod._validate_samples(4096) == 4096
