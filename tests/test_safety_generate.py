"""SECURITY (c+d) — AST denylist wired into core.generate (ADR-0003).

generate() must run the AST safety pre-check BEFORE spawning the subprocess:
unsafe code is rejected without ever executing, with a structured result the
agent can act on; safe code proceeds normally. Mocked subprocess — no CAD venv.
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


def _gen_se(create_stl=True):
    def se(args, **kwargs):
        cad_out = Path(kwargs["env"]["CAD_OUT"])
        stem = Path(args[1]).name[: -len("_gen.py")]
        if create_stl:
            (cad_out / f"{stem}.stl").write_bytes(b"solid x\nendsolid x\n")
        return CompletedStub(0, "", "")
    return se


def test_generate_rejects_socket_import_without_running(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with mock.patch.object(core.subprocess, "run", side_effect=_gen_se()) as m:
        res = core.generate(
            code="import socket\nimport cadquery as cq\n",
            out_dir=str(tmp_path), stem="part",
        )
    assert res["success"] is False
    assert res.get("rejected") is True
    assert res["stl"] is None
    assert any("socket" in v for v in res["violations"])
    m.assert_not_called()   # the subprocess never ran — rejected pre-exec


def test_generate_rejects_os_system(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    with mock.patch.object(core.subprocess, "run", side_effect=_gen_se()) as m:
        res = core.generate(code="import os\nos.system('id')\n", out_dir=str(tmp_path))
    assert res["success"] is False
    assert res.get("rejected") is True
    m.assert_not_called()


def test_generate_runs_legit_cadquery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    code = (
        "import cadquery as cq, os\n"
        "OUT = os.environ['CAD_OUT']\n"
        "cq.exporters.export(cq.Workplane('XY').box(10,10,10), os.path.join(OUT,'part.stl'))\n"
    )
    with mock.patch.object(core.subprocess, "run", side_effect=_gen_se()) as m:
        res = core.generate(code=code, out_dir=str(tmp_path), stem="part")
    assert res["success"] is True
    assert res.get("rejected") in (None, False)
    m.assert_called_once()   # legit code DID run


def test_generate_ast_check_overridable_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    monkeypatch.setenv("HERMES_CAD_NO_AST_CHECK", "1")
    # With the check disabled, even a banned import is allowed to run.
    with mock.patch.object(core.subprocess, "run", side_effect=_gen_se()) as m:
        res = core.generate(code="import socket\n", out_dir=str(tmp_path), stem="part")
    assert res["success"] is True
    m.assert_called_once()
