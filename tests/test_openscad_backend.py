"""Wave 3.3 — optional OpenSCAD backend (ADR-0008), unit tests (mocked).

core gains:
  openscad_bin() — resolve via HERMES_OPENSCAD_BIN / shutil.which / common
                   AppImage-extract locations; None if absent.
  generate(code, backend="openscad") — write .scad, run `openscad -o part.stl`,
                   mesh-only (step is None), AST check skipped (SCAD != Python).
  doctor() — reports openscad_backend.

All subprocess + discovery mocked; no real binary needed. A real-binary
integration test lives in test_integration (skips when absent, like the CAD
venv skip).
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


# ---- openscad_bin discovery ---------------------------------------------------

def test_openscad_bin_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_OPENSCAD_BIN", "/custom/openscad")
    monkeypatch.setattr(core.os.path, "exists", lambda p: p == "/custom/openscad")
    assert core.openscad_bin() == "/custom/openscad"


def test_openscad_bin_from_path(monkeypatch):
    monkeypatch.delenv("HERMES_OPENSCAD_BIN", raising=False)
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/openscad" if b == "openscad" else None)
    assert core.openscad_bin() == "/usr/bin/openscad"


def test_openscad_bin_none_when_absent(monkeypatch):
    monkeypatch.delenv("HERMES_OPENSCAD_BIN", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda b: None)
    monkeypatch.setattr(core.os.path, "exists", lambda p: False)
    assert core.openscad_bin() is None


# ---- generate(backend="openscad") --------------------------------------------

def test_generate_openscad_builds_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OPENSCAD_BIN", "/usr/bin/openscad")
    monkeypatch.setattr(core.os.path, "exists", lambda p: p == "/usr/bin/openscad")

    def se(args, **kwargs):
        # openscad -o <out>/part.stl <scad>
        out = Path(kwargs["env"]["CAD_OUT"])
        (out / "part.stl").write_bytes(b"solid x\nendsolid x\n")
        return CompletedStub(0, "", "")

    scad = "cube([10,10,10]);"
    # emit_glb=False: this unit-test asserts the openscad ARGV, not the GLB/
    # topology export step (ADR-0013) — which would otherwise fire a second mocked
    # subprocess call and shift m.call_args off the openscad invocation.
    with mock.patch.object(core.subprocess, "run", side_effect=se) as m:
        res = core.generate(code=scad, out_dir=str(tmp_path), stem="part",
                            backend="openscad", emit_glb=False)

    argv = m.call_args.args[0]
    assert argv[0] == "/usr/bin/openscad"
    assert "-o" in argv
    stl_arg = argv[argv.index("-o") + 1]
    assert stl_arg.endswith("part.stl")
    assert any(a.endswith(".scad") for a in argv)
    # the .scad source was written verbatim
    assert (tmp_path / "part.scad").read_text() == scad
    assert res["success"] is True
    assert res["backend"] == "openscad"
    assert res["step"] is None     # OpenSCAD is mesh-only (ADR-0008)


def test_generate_openscad_skips_ast_check(tmp_path, monkeypatch):
    # SCAD is not Python — the Python AST denylist must NOT run on it. A SCAD
    # source containing 'socket' as an identifier must not be rejected.
    monkeypatch.setenv("HERMES_OPENSCAD_BIN", "/usr/bin/openscad")
    monkeypatch.setattr(core.os.path, "exists", lambda p: p == "/usr/bin/openscad")

    def se(args, **kwargs):
        out = Path(kwargs["env"]["CAD_OUT"])
        (out / "part.stl").write_bytes(b"solid x\nendsolid x\n")
        return CompletedStub(0, "", "")

    with mock.patch.object(core.subprocess, "run", side_effect=se):
        res = core.generate(code="module socket(){cube(5);} socket();",
                            out_dir=str(tmp_path), stem="part", backend="openscad",
                            emit_glb=False)
    assert res["success"] is True
    assert res.get("rejected") in (None, False)


def test_generate_openscad_scrubbed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OPENSCAD_BIN", "/usr/bin/openscad")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    monkeypatch.setattr(core.os.path, "exists", lambda p: p == "/usr/bin/openscad")

    def se(args, **kwargs):
        out = Path(kwargs["env"]["CAD_OUT"])
        (out / "part.stl").write_bytes(b"solid\nendsolid\n")
        return CompletedStub(0, "", "")

    with mock.patch.object(core.subprocess, "run", side_effect=se) as m:
        core.generate(code="cube(5);", out_dir=str(tmp_path), stem="part",
                      backend="openscad", emit_glb=False)
    assert "OPENROUTER_API_KEY" not in m.call_args.kwargs["env"]


def test_generate_openscad_clean_error_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_OPENSCAD_BIN", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda b: None)
    monkeypatch.setattr(core.os.path, "exists", lambda p: False)
    with mock.patch.object(core.subprocess, "run") as m:
        res = core.generate(code="cube(5);", out_dir=str(tmp_path), stem="part", backend="openscad")
    assert res["success"] is False
    assert "openscad" in res.get("error", "").lower() or "openscad" in res["stderr"].lower()
    m.assert_not_called()   # never tried to run a missing binary


def test_generate_unknown_backend_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    res = core.generate(code="cube(5);", out_dir=str(tmp_path), stem="part", backend="bogus")
    assert res["success"] is False
    assert "backend" in res.get("error", "").lower() or "backend" in res["stderr"].lower()


def test_generate_default_backend_is_cadquery(tmp_path, monkeypatch):
    # the default path is unchanged: cadquery via the CAD venv python
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")

    def se(args, **kwargs):
        out = Path(kwargs["env"]["CAD_OUT"])
        stem = Path(args[1]).name[: -len("_gen.py")]
        (out / f"{stem}.stl").write_bytes(b"solid\nendsolid\n")
        return CompletedStub(0, "", "")

    with mock.patch.object(core.subprocess, "run", side_effect=se) as m:
        res = core.generate(code="import cadquery as cq\n", out_dir=str(tmp_path), stem="part")
    assert m.call_args.args[0][0] == "/fake/py"
    assert res["backend"] == "cadquery"


# ---- doctor reports openscad --------------------------------------------------

def test_doctor_reports_openscad_available(tmp_path, monkeypatch):
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_CAD_PYTHON", str(fake_py))
    monkeypatch.setattr(core, "venv_ready", lambda: True)
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: True)
    monkeypatch.setattr(core, "openscad_bin", lambda: "/usr/bin/openscad")
    rep = core.doctor()
    by = {c["check"]: c for c in rep["checks"]}
    assert by["openscad_backend"]["pass"] is True
    assert "openscad" in by["openscad_backend"]["detail"].lower()


def test_doctor_openscad_absent_does_not_block_ready(tmp_path, monkeypatch):
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_CAD_PYTHON", str(fake_py))
    monkeypatch.setattr(core, "venv_ready", lambda: True)
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: True)
    monkeypatch.setattr(core, "openscad_bin", lambda: None)
    rep = core.doctor()
    by = {c["check"]: c for c in rep["checks"]}
    assert by["openscad_backend"]["pass"] is False
    assert rep["ready"] is True   # openscad is optional
