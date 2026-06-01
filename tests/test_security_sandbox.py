"""SECURITY (b) — opt-in bubblewrap/firejail sandbox (ADR-0002).

The generate subprocess can be confined (no network, read-only FS except
CAD_OUT, secret dirs masked) when HERMES_CAD_SANDBOX=1. We test:

  - sandbox_info(): detects bwrap/firejail, reports availability + which binary;
  - _wrap_sandbox(argv, out_dir): builds the bwrap argv with --unshare-net,
    --ro-bind / /, the CAD_OUT rw bind, and tmpfs masks of existing secret dirs
    (resolved realpath, so a symlinked ~/.aws doesn't break it);
  - generate() wraps only when HERMES_CAD_SANDBOX=1 AND a sandbox exists;
  - when requested-but-unavailable: warn + run UNSANDBOXED (degrade), result
    records sandbox="requested-unavailable" (ADR-0002 conscious choice).

bwrap argv building is pure (no exec) — fully mockable. Detection is probed via
shutil.which, mocked here.
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
        # the generated script path is the LAST arg in both wrapped/unwrapped argv
        script = Path(args[-1])
        stem = script.name[: -len("_gen.py")]
        if create_stl:
            (cad_out / f"{stem}.stl").write_bytes(b"solid x\nendsolid x\n")
        return CompletedStub(0, "", "")
    return se


# ---- sandbox_info detection -----------------------------------------------------

def test_sandbox_info_detects_bwrap(monkeypatch):
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/bwrap" if b == "bwrap" else None)
    info = core.sandbox_info()
    assert info["available"] is True
    assert info["tool"] == "bwrap"
    assert info["bin"] == "/usr/bin/bwrap"


def test_sandbox_info_falls_back_to_firejail(monkeypatch):
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/firejail" if b == "firejail" else None)
    info = core.sandbox_info()
    assert info["available"] is True
    assert info["tool"] == "firejail"


def test_sandbox_info_none_when_absent(monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda b: None)
    info = core.sandbox_info()
    assert info["available"] is False
    assert info["tool"] is None


def test_sandbox_info_prefers_bwrap_over_firejail(monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert core.sandbox_info()["tool"] == "bwrap"


# ---- _wrap_sandbox argv ---------------------------------------------------------

def test_wrap_sandbox_bwrap_argv_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/bwrap" if b == "bwrap" else None)
    inner = ["/fake/py", str(tmp_path / "part_gen.py")]
    argv = core._wrap_sandbox(inner, out_dir=str(tmp_path))
    assert argv[0] == "/usr/bin/bwrap"
    # no network, isolation flags
    assert "--unshare-net" in argv
    # read-only root
    assert "--ro-bind" in argv
    i = argv.index("--ro-bind")
    assert argv[i + 1] == "/" and argv[i + 2] == "/"
    # CAD_OUT is a writable bind
    assert "--bind" in argv
    # the inner command is appended verbatim at the end
    assert argv[-2:] == inner


def test_wrap_sandbox_masks_existing_secret_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/bwrap" if b == "bwrap" else None)
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".ssh").mkdir()
    monkeypatch.setenv("HOME", str(home))
    argv = core._wrap_sandbox(["/fake/py", "x.py"], out_dir=str(tmp_path / "out"))
    joined = " ".join(argv)
    # existing secret dirs get a tmpfs mask
    assert str((home / ".hermes").resolve()) in joined
    assert str((home / ".ssh").resolve()) in joined
    # a NON-existent secret dir must NOT be masked (bwrap errors on missing tmpfs target)
    assert str(home / ".gnupg") not in joined


def test_wrap_sandbox_firejail_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/firejail" if b == "firejail" else None)
    inner = ["/fake/py", "part_gen.py"]
    argv = core._wrap_sandbox(inner, out_dir=str(tmp_path))
    assert argv[0] == "/usr/bin/firejail"
    assert any(a.startswith("--net") for a in argv)   # net disabled
    assert argv[-2:] == inner


# ---- generate integration -------------------------------------------------------

def test_generate_no_sandbox_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    monkeypatch.delenv("HERMES_CAD_SANDBOX", raising=False)
    with mock.patch.object(core.subprocess, "run", side_effect=_gen_se()) as m:
        res = core.generate(code="import cadquery as cq\n", out_dir=str(tmp_path), stem="part")
    argv = m.call_args.args[0]
    assert argv[0] == "/fake/py"          # not wrapped
    assert res["sandbox"] == "off"
    assert res["success"] is True


def test_generate_wraps_when_sandbox_requested_and_available(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    monkeypatch.setenv("HERMES_CAD_SANDBOX", "1")
    monkeypatch.setattr(core.shutil, "which",
                        lambda b: "/usr/bin/bwrap" if b == "bwrap" else None)
    with mock.patch.object(core.subprocess, "run", side_effect=_gen_se()) as m:
        res = core.generate(code="import cadquery as cq\n", out_dir=str(tmp_path), stem="part")
    argv = m.call_args.args[0]
    assert argv[0] == "/usr/bin/bwrap"    # wrapped
    assert "--unshare-net" in argv
    assert res["sandbox"] == "bwrap"
    assert res["success"] is True


def test_generate_degrades_when_sandbox_requested_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CAD_PYTHON", "/fake/py")
    monkeypatch.setenv("HERMES_CAD_SANDBOX", "1")
    monkeypatch.setattr(core.shutil, "which", lambda b: None)   # no sandbox tool
    with mock.patch.object(core.subprocess, "run", side_effect=_gen_se()) as m:
        res = core.generate(code="import cadquery as cq\n", out_dir=str(tmp_path), stem="part")
    argv = m.call_args.args[0]
    assert argv[0] == "/fake/py"          # ran UNSANDBOXED (degrade, not fail)
    assert res["sandbox"] == "requested-unavailable"
    assert res["success"] is True
