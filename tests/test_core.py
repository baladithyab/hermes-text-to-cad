"""Wave 0.2 — core unit tests (mocked subprocess).

core.py is pure orchestration over the CAD venv: it builds argv, shells out, and
parses stdout/exit codes. None of that needs the real CAD stack — we mock
``subprocess.run`` and assert (a) the argv is built correctly, (b) stdout is
parsed correctly, and (c) gate_pass is derived from the exit code (0=pass,
1=fail, both are valid runs).

Hard rule under test: nothing here imports cadquery/trimesh/vtk.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_text_to_cad.core as core  # noqa: E402


# ---- helpers ------------------------------------------------------------------

class CompletedStub:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def patch_run(returncode=0, stdout="", stderr="", side_effect=None):
    """Patch core.subprocess.run; return the mock so tests can inspect call_args."""
    if side_effect is not None:
        return mock.patch.object(core.subprocess, "run", side_effect=side_effect)
    return mock.patch.object(
        core.subprocess, "run",
        return_value=CompletedStub(returncode, stdout, stderr),
    )


def gen_side_effect(create_stl=True, create_step=False, returncode=0, stdout="",
                    stderr="", stl_bytes=b"solid x\nendsolid x\n"):
    """side_effect for generate(): optionally writes the expected STL/STEP.

    stl_bytes lets a test write a zero-byte STL (b"") to exercise the
    non-empty-file guard.
    """
    def _se(args, **kwargs):
        cad_out = Path(kwargs["env"]["CAD_OUT"])
        script = Path(args[1])              # args = [py, "<out>/<stem>_gen.py"]
        stem = script.name[: -len("_gen.py")]
        if create_stl:
            (cad_out / f"{stem}.stl").write_bytes(stl_bytes)
        if create_step:
            (cad_out / f"{stem}.step").write_text("ISO-10303-21;\n")
        return CompletedStub(returncode, stdout, stderr)
    return _se


# ---- cad_venv_python ----------------------------------------------------------

def test_cad_venv_python_default(monkeypatch):
    monkeypatch.delenv("HERMES_CAD_PYTHON", raising=False)
    p = core.cad_venv_python()
    assert p.endswith("/.venvs/cad/bin/python")


def test_cad_venv_python_override(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/custom/py")
    assert core.cad_venv_python() == "/custom/py"


# ---- generate -----------------------------------------------------------------

def test_generate_builds_argv_and_writes_script(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    code = "import cadquery as cq\n# build...\n"
    with patch_run(side_effect=gen_side_effect(create_stl=True, create_step=True)) as m:
        res = core.generate(code=code, out_dir=str(tmp_path), stem="widget")

    args, kwargs = m.call_args
    argv = args[0]
    assert argv[0] == "/fake/py"
    assert argv[1] == str(tmp_path / "widget_gen.py")
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["CAD_OUT"] == str(tmp_path)
    # the user code is written verbatim to the generated script
    assert (tmp_path / "widget_gen.py").read_text() == code

    assert res["success"] is True
    assert res["stl"] == str(tmp_path / "widget.stl")
    assert res["step"] == str(tmp_path / "widget.step")
    assert res["out_dir"] == str(tmp_path)


def test_generate_failure_when_no_stl(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    # exit 0 but the code never wrote an STL → not a success.
    with patch_run(side_effect=gen_side_effect(create_stl=False, returncode=0)):
        res = core.generate(code="pass", out_dir=str(tmp_path))
    assert res["success"] is False
    assert res["stl"] is None
    assert res["step"] is None


def test_generate_failure_when_stl_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    # exit 0 and the STL file exists but is ZERO bytes (export touched then
    # aborted) → not a success. Guards the `stat().st_size > 0` check.
    with patch_run(side_effect=gen_side_effect(create_stl=True, stl_bytes=b"", returncode=0)):
        res = core.generate(code="pass", out_dir=str(tmp_path))
    assert res["success"] is False
    assert res["stl"] is None


def test_generate_surfaces_traceback_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    tb = "Traceback (most recent call last):\nValueError: fillet radius too large\n"
    with patch_run(side_effect=gen_side_effect(create_stl=False, returncode=1, stderr=tb)):
        res = core.generate(code="boom", out_dir=str(tmp_path))
    assert res["success"] is False
    assert "fillet radius too large" in res["stderr"]


def test_generate_truncates_long_streams(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    long_err = "x" * 5000
    long_out = "y" * 5000
    with patch_run(side_effect=gen_side_effect(
        create_stl=True, returncode=0, stdout=long_out, stderr=long_err)):
        res = core.generate(code="ok", out_dir=str(tmp_path))
    assert len(res["stderr"]) == 3000   # last 3000 chars
    assert len(res["stdout"]) == 2000   # last 2000 chars


def test_generate_defaults_to_tempdir(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(side_effect=gen_side_effect(create_stl=True)):
        res = core.generate(code="ok")  # no out_dir
    out = Path(res["out_dir"])
    assert out.exists()
    assert "cad_" in out.name


# ---- render -------------------------------------------------------------------

def test_render_parses_montage_and_vtk_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    montage = tmp_path / "part_montage.png"
    montage.write_bytes(b"\x89PNG\r\n")
    stdout = f"some chatter\n{montage}\n"
    with patch_run(returncode=0, stdout=stdout, stderr="[render] backend=vtk views=3") as m:
        res = core.render(stl=str(tmp_path / "part.stl"))

    argv = m.call_args.args[0]
    assert argv[0] == "/fake/py"
    assert argv[1].endswith("scripts/render.py")
    assert argv[2] == str(tmp_path / "part.stl")
    assert res["success"] is True
    assert res["montage"] == str(montage)
    assert res["backend"] == "vtk"


def test_render_matplotlib_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    montage = tmp_path / "m.png"
    montage.write_bytes(b"\x89PNG")
    with patch_run(returncode=0, stdout=str(montage),
                   stderr="VTK failed; falling back to matplotlib"):
        res = core.render(stl=str(tmp_path / "p.stl"))
    assert res["backend"] == "matplotlib"
    assert res["success"] is True


def test_render_failure_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(returncode=2, stdout="", stderr="boom"):
        res = core.render(stl=str(tmp_path / "p.stl"))
    assert res["success"] is False
    assert res["montage"] is None


def test_render_failure_when_montage_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    # exit 0 but the named montage file doesn't exist → not a success.
    with patch_run(returncode=0, stdout=str(tmp_path / "nope.png"), stderr="backend=vtk"):
        res = core.render(stl=str(tmp_path / "p.stl"))
    assert res["success"] is False


# ---- measure ------------------------------------------------------------------

def test_measure_builds_argv_with_spec_and_parses_json(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    report_json = '{"measured": {"bbox_mm": [40,30,20]}, "gate": {"PASS": true}}'
    with patch_run(returncode=0, stdout=report_json) as m:
        res = core.measure(stl="/x/part.stl", spec_path="/x/spec.json")

    argv = m.call_args.args[0]
    assert argv[1].endswith("scripts/measure.py")
    assert argv[2] == "/x/part.stl"
    assert "--spec" in argv and argv[argv.index("--spec") + 1] == "/x/spec.json"
    assert res["success"] is True
    assert res["gate_pass"] is True
    assert res["report"]["measured"]["bbox_mm"] == [40, 30, 20]


def test_measure_no_spec_omits_flag(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(returncode=0, stdout='{"measured": {}}') as m:
        core.measure(stl="/x/part.stl")
    argv = m.call_args.args[0]
    assert "--spec" not in argv


def test_measure_gate_fail_is_exit_1_but_still_success(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    # exit 1 = gate failed but the measurement RAN — success True, gate_pass False.
    with patch_run(returncode=1, stdout='{"gate": {"PASS": false}}'):
        res = core.measure(stl="/x/part.stl", spec_path="/x/spec.json")
    assert res["success"] is True
    assert res["gate_pass"] is False


def test_measure_crash_exit_2_is_not_success(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(returncode=2, stdout="", stderr="trimesh blew up"):
        res = core.measure(stl="/x/part.stl")
    assert res["success"] is False
    assert res["gate_pass"] is False


def test_measure_nonjson_stdout_falls_back_to_raw(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(returncode=0, stdout="not json at all", stderr="warn"):
        res = core.measure(stl="/x/part.stl")
    assert "raw" in res["report"]
    assert res["report"]["raw"] == "not json at all"


# ---- review -------------------------------------------------------------------

def test_review_builds_argv_without_models(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(returncode=0, stdout="reviews...") as m:
        res = core.review(montage="/x/m.png", spec_path="/x/spec.json")
    argv = m.call_args.args[0]
    assert argv[1].endswith("scripts/scatter_review.py")
    assert argv[2] == "/x/m.png"
    assert argv[3] == "/x/spec.json"
    # default mode is qa, passed explicitly; no --models when none given
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "qa"
    assert "--models" not in argv
    assert res["success"] is True
    assert res["reviews"] == "reviews..."


def test_review_appends_models(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(returncode=0, stdout="ok") as m:
        core.review(montage="/x/m.png", spec_path="/x/s.json", models="a/b,c/d")
    argv = m.call_args.args[0]
    assert "--models" in argv and argv[argv.index("--models") + 1] == "a/b,c/d"


def test_review_passes_free_mode(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with patch_run(returncode=0, stdout="ok") as m:
        core.review(montage="/x/m.png", spec_path="/x/s.json", mode="free")
    argv = m.call_args.args[0]
    assert argv[argv.index("--mode") + 1] == "free"


def test_review_parses_structured_block(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    import json as _json
    block = _json.dumps({"mode": "qa", "reviewers": [
        {"model": "a/x", "verdict": "needs_fixes", "must_fix": ["missing hole"]},
        {"model": "b/y", "verdict": "needs_fixes", "must_fix": ["the hole is missing"]},
    ]})
    stdout = "human readable stuff\nREVIEWS_JSON\n" + block + "\n"
    with patch_run(returncode=0, stdout=stdout):
        res = core.review(montage="/x/m.png", spec_path="/x/s.json")
    assert len(res["reviewers"]) == 2
    assert res["aggregate"]["gate_pass"] is False
    assert res["aggregate"]["convergent_must_fix"]


# ---- doctor -------------------------------------------------------------------

def test_doctor_shape_when_venv_ready(tmp_path, monkeypatch):
    # Make the venv python "exist" and venv_ready() pass via a mocked import check.
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_CAD_PYTHON", str(fake_py))
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: True)

    with patch_run(returncode=0, stdout="ok"):
        rep = core.doctor()

    assert set(rep.keys()) == {"ready", "vision_ready", "checks"}
    names = [c["check"] for c in rep["checks"]]
    assert names == [
        "cad_venv_exists", "cad_libs_import", "render_display",
        "vision_gate_key", "scripts_present", "sandbox_available",
    ]
    for c in rep["checks"]:
        assert set(c.keys()) == {"check", "pass", "detail"}
        assert isinstance(c["pass"], bool)
    assert rep["ready"] is True       # venv + libs + scripts all present
    assert rep["vision_ready"] is True


def test_doctor_not_ready_when_venv_missing(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/nonexistent/py")
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: False)
    # venv_ready won't even run subprocess (path missing); patch anyway for safety.
    with patch_run(returncode=1, stdout=""):
        rep = core.doctor()
    assert rep["ready"] is False
    assert rep["vision_ready"] is False
    by_name = {c["check"]: c for c in rep["checks"]}
    assert by_name["cad_venv_exists"]["pass"] is False
    assert by_name["cad_libs_import"]["pass"] is False


def test_doctor_ready_but_vision_not_ready_without_key(tmp_path, monkeypatch):
    # The plugin's whole "optional cred" design: core can be ready while the
    # vision gate is not, purely because OPENROUTER_API_KEY is absent. Isolates
    # the key's contribution to vision_ready (kills the `vision_ready: core_ok`
    # mutant).
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_CAD_PYTHON", str(fake_py))
    monkeypatch.setattr(core, "venv_ready", lambda: True)   # libs import OK
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: False)
    rep = core.doctor()
    assert rep["ready"] is True
    assert rep["vision_ready"] is False


def test_doctor_not_ready_when_a_script_missing(tmp_path, monkeypatch):
    # venv + libs present, but a required script is missing → ready must be False.
    # Isolates scripts_ok's contribution to the ready verdict.
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_CAD_PYTHON", str(fake_py))
    monkeypatch.setattr(core, "venv_ready", lambda: True)
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: True)
    empty_scripts = tmp_path / "scripts"     # exists but contains none of the 3
    empty_scripts.mkdir()
    monkeypatch.setattr(core, "SCRIPTS", empty_scripts)
    rep = core.doctor()
    by_name = {c["check"]: c for c in rep["checks"]}
    assert by_name["cad_venv_exists"]["pass"] is True
    assert by_name["cad_libs_import"]["pass"] is True
    assert by_name["scripts_present"]["pass"] is False
    assert rep["ready"] is False


# ---- doctor: sandbox check ----------------------------------------------------

def test_doctor_reports_sandbox_available(tmp_path, monkeypatch):
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_CAD_PYTHON", str(fake_py))
    monkeypatch.setattr(core, "venv_ready", lambda: True)
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: True)
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/bwrap" if b == "bwrap" else None)
    rep = core.doctor()
    by = {c["check"]: c for c in rep["checks"]}
    assert by["sandbox_available"]["pass"] is True
    assert "bwrap" in by["sandbox_available"]["detail"]


def test_doctor_sandbox_absent_does_not_block_ready(tmp_path, monkeypatch):
    # No sandbox tool: sandbox check fails but core readiness is unaffected
    # (generated code runs unsandboxed by default).
    fake_py = tmp_path / "bin" / "python"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_CAD_PYTHON", str(fake_py))
    monkeypatch.setattr(core, "venv_ready", lambda: True)
    monkeypatch.setattr(core, "_has_openrouter_key", lambda: True)
    monkeypatch.setattr(core.shutil, "which", lambda b: None)
    rep = core.doctor()
    by = {c["check"]: c for c in rep["checks"]}
    assert by["sandbox_available"]["pass"] is False
    assert rep["ready"] is True   # sandbox absence does NOT block readiness


# ---- _has_openrouter_key ------------------------------------------------------

def test_has_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xyz")
    assert core._has_openrouter_key() is True


def test_has_key_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    home = tmp_path
    (home / ".hermes").mkdir()
    (home / ".hermes" / ".env").write_text('OPENROUTER_API_KEY="sk-or-fromfile"\n')
    monkeypatch.setenv("HOME", str(home))
    assert core._has_openrouter_key() is True


def test_has_key_ignores_commented_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / ".env").write_text("# OPENROUTER_API_KEY=sk-or-disabled\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert core._has_openrouter_key() is False


def test_has_key_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.hermes/.env
    assert core._has_openrouter_key() is False
