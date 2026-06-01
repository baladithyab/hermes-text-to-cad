"""SECURITY (c+d) — AST denylist + code-size cap (ADR-0003).

Defense-in-depth layer 1 (the env-scrub ADR-0001 and opt-in sandbox ADR-0002 are
the real boundaries). check_code(src) parses with `ast` and rejects obvious
exfil/abuse BEFORE the code is executed:

  - banned imports: socket, subprocess, urllib, requests, http, ftplib, smtplib,
    ctypes, multiprocessing, pty, signal, asyncio (network/process abuse)
  - dangerous builtins: eval, exec, compile, __import__, input
  - os.system / os.popen / os.exec* / os.spawn* / os.fork
  - open(..., 'w'|'a'|...) writing OUTSIDE CAD_OUT (absolute non-CAD_OUT paths)
  - SyntaxError -> reject
  - code-size cap (default 100 KB)

Legitimate cadquery code MUST pass. The check is configurable
(HERMES_CAD_NO_AST_CHECK=1 disables) and overridable (extend the banned set).

Pure stdlib (ast) — no CAD venv needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_text_to_cad import safety  # noqa: E402


# ---- legitimate code passes ----------------------------------------------------

LEGIT_CADQUERY = """
import cadquery as cq
import os
import math
OUT = os.environ["CAD_OUT"]
part = cq.Workplane("XY").box(40, 30, 20).faces(">Z").workplane().hole(8)
cq.exporters.export(part, os.path.join(OUT, "part.stl"))
cq.exporters.export(part, os.path.join(OUT, "part.step"))
"""


def test_legit_cadquery_accepted():
    rep = safety.check_code(LEGIT_CADQUERY)
    assert rep["ok"] is True, rep["violations"]
    assert rep["violations"] == []


def test_legit_open_writing_into_cad_out_accepted():
    # Writing via os.path.join(OUT, ...) is the normal export idiom and must pass.
    src = (
        "import os\n"
        "OUT = os.environ['CAD_OUT']\n"
        "open(os.path.join(OUT, 'log.txt'), 'w').write('ok')\n"
    )
    assert safety.check_code(src)["ok"] is True


def test_relative_open_write_allowed_cwd_is_cad_out():
    # generate() runs with cwd=CAD_OUT, so a bare relative write lands in CAD_OUT.
    src = "open('part_notes.txt', 'w').write('hi')\n"
    assert safety.check_code(src)["ok"] is True


def test_read_open_always_allowed():
    src = "data = open('/etc/hostname').read()\n"
    # Reading is not the exfil vector the write-denylist targets; allowed.
    assert safety.check_code(src)["ok"] is True


# ---- banned imports rejected (the headline) ------------------------------------

@pytest.mark.parametrize("mod", [
    "socket", "subprocess", "urllib", "urllib.request", "requests",
    "http", "http.client", "ftplib", "smtplib", "ctypes", "multiprocessing",
    "pty", "asyncio",
])
def test_banned_import_rejected(mod):
    rep = safety.check_code(f"import {mod}\n")
    assert rep["ok"] is False
    assert any(mod.split(".")[0] in v for v in rep["violations"])


@pytest.mark.parametrize("stmt", [
    "from socket import socket",
    "from subprocess import run",
    "from urllib.request import urlopen",
    "import socket as s",
])
def test_banned_from_import_rejected(stmt):
    assert safety.check_code(stmt + "\n")["ok"] is False


# ---- dangerous builtins / os calls --------------------------------------------

@pytest.mark.parametrize("src", [
    "os.system('rm -rf /')",
    "import os\nos.system('id')",
    "os.popen('whoami')",
    "os.execv('/bin/sh', ['sh'])",
    "os.spawnl(os.P_WAIT, '/bin/sh')",
    "os.fork()",
])
def test_os_process_calls_rejected(src):
    assert safety.check_code(src)["ok"] is False


@pytest.mark.parametrize("src", [
    "eval('1+1')",
    "exec('x=1')",
    "__import__('socket')",
    "compile('x', '<s>', 'eval')",
])
def test_dangerous_builtins_rejected(src):
    assert safety.check_code(src)["ok"] is False


def test_dunder_import_evasion_rejected():
    # The classic AST-evasion attempt is itself a banned call.
    assert safety.check_code("__import__('so'+'cket')")["ok"] is False


# ---- open() writing outside CAD_OUT --------------------------------------------

@pytest.mark.parametrize("src", [
    "open('/etc/passwd', 'w').write('x')",
    "open('/home/victim/.ssh/authorized_keys', 'a').write('key')",
    "open('/tmp/evil', 'wb').write(b'x')",
])
def test_open_write_outside_cad_out_rejected(src):
    assert safety.check_code(src)["ok"] is False


def test_pathlib_write_outside_rejected():
    src = "from pathlib import Path\nPath('/etc/evil').write_text('x')\n"
    assert safety.check_code(src)["ok"] is False


# ---- write-denylist completeness (review findings #1, #2, #3) -----------------

@pytest.mark.parametrize("src", [
    "import os\nfd = os.open('/etc/cron.d/evil', os.O_WRONLY | os.O_CREAT, 0o644)",
    "import os\nos.open('/home/victim/.ssh/authorized_keys', os.O_WRONLY|os.O_APPEND)",
    "import os\nos.open('/etc/passwd', os.O_RDWR)",
])
def test_os_open_write_outside_cad_out_rejected(src):
    # os.open with a write/create flag to an absolute non-CAD_OUT path -> reject
    assert safety.check_code(src)["ok"] is False


def test_os_open_readonly_literal_allowed():
    # O_RDONLY (the default, value 0) to a literal path is a read — allowed.
    src = "import os\nfd = os.open('/etc/hostname', os.O_RDONLY)\n"
    assert safety.check_code(src)["ok"] is True


@pytest.mark.parametrize("src", [
    "import os\nos.rename('part.stl', '/etc/issue')",
    "import os\nos.replace('part.stl', '/home/victim/.ssh/authorized_keys')",
    "import os\nos.link('part.stl', '/etc/evil')",
    "import os\nos.symlink('part.stl', '/etc/evil')",
    "import shutil\nshutil.move('part.stl', '/etc/issue')",
])
def test_file_move_to_absolute_rejected(src):
    # move/link primitives with an absolute/home literal destination -> reject
    assert safety.check_code(src)["ok"] is False


def test_file_move_within_cad_out_allowed():
    # a relative-to-relative move stays in CAD_OUT (cwd) — allowed.
    src = "import os\nos.rename('tmp.stl', 'part.stl')\n"
    assert safety.check_code(src)["ok"] is True


@pytest.mark.parametrize("src", [
    "open('../../etc/issue', 'w').write('x')",
    "open('../escape.txt', 'a').write('x')",
    "from pathlib import Path\nPath('../../etc/issue').write_text('x')",
    "import os\nopen(os.path.join('..', '..', 'etc', 'x'), 'w')",
])
def test_relative_traversal_write_rejected(src):
    # cwd=CAD_OUT is NOT a boundary for '..' — a literal containing '..' escapes.
    assert safety.check_code(src)["ok"] is False


def test_normal_relative_write_still_allowed():
    # no '..' — a plain relative write lands in CAD_OUT (cwd) and is allowed.
    assert safety.check_code("open('part_notes.txt', 'w').write('ok')\n")["ok"] is True


def test_legit_export_idiom_still_passes_after_hardening():
    # The hardening must not break the canonical export idiom.
    src = (
        "import os, cadquery as cq\n"
        "OUT = os.environ['CAD_OUT']\n"
        "cq.exporters.export(cq.Workplane('XY').box(10,10,10), os.path.join(OUT,'part.stl'))\n"
    )
    assert safety.check_code(src)["ok"] is True


def test_syntax_error_uses_canonical_token():
    # review #6: emit 'SyntaxError' (CamelCase) so summarize_error's hint fires.
    rep = safety.check_code("def (:\n  pass")
    assert rep["ok"] is False
    assert any("SyntaxError" in v for v in rep["violations"])


# ---- syntax / size -------------------------------------------------------------

def test_syntax_error_rejected():
    rep = safety.check_code("def (:\n  pass")
    assert rep["ok"] is False
    assert any("syntax" in v.lower() for v in rep["violations"])


def test_code_size_cap_rejects_huge_blob():
    huge = "x = 1\n" * 50000   # > 100 KB
    rep = safety.check_code(huge)
    assert rep["ok"] is False
    assert any("size" in v.lower() or "large" in v.lower() for v in rep["violations"])


def test_code_size_cap_is_configurable():
    rep = safety.check_code("x = 1\n", max_bytes=2)
    assert rep["ok"] is False


# ---- configurability -----------------------------------------------------------

def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("HERMES_CAD_NO_AST_CHECK", "1")
    # Even blatant abuse passes when explicitly disabled (override is the point).
    rep = safety.check_code("import socket")
    assert rep["ok"] is True
    assert rep.get("skipped") is True


def test_banned_modules_set_is_extensible():
    # A caller can extend the banned set for their own policy.
    rep = safety.check_code("import json", banned_modules=safety.BANNED_MODULES | {"json"})
    assert rep["ok"] is False


def test_report_lists_multiple_violations():
    src = "import socket\nimport subprocess\nos.system('x')\n"
    rep = safety.check_code(src)
    assert rep["ok"] is False
    assert len(rep["violations"]) >= 2


def test_violations_have_line_numbers():
    rep = safety.check_code("x = 1\nimport socket\n")
    assert rep["ok"] is False
    # line info aids the agent's fix
    assert any("2" in v for v in rep["violations"])
