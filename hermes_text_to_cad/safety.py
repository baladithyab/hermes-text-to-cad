"""hermes_text_to_cad.safety — AST denylist for generated code (ADR-0003).

DEFENSE-IN-DEPTH, layer 1. The real boundaries are the scrubbed subprocess env
(ADR-0001) and the opt-in bubblewrap sandbox (ADR-0002). This is a cheap,
dependency-free first line that rejects the OBVIOUS exfil/abuse before the code
is ever executed, and gives the agent an actionable rejection message.

`check_code(src) -> {ok, violations, skipped}` parses with `ast` and rejects:
  - banned imports (socket/subprocess/urllib/requests/http/ftp/smtp/ctypes/...)
  - dangerous builtins (eval/exec/compile/__import__/input)
  - os.system / os.popen / os.exec* / os.spawn* / os.fork
  - open(..., 'w'|'a'|'x'|...) writing to a provably-non-CAD_OUT absolute path
  - SyntaxError
  - oversized blobs (default 100 KB)

NOT a verifier. AST evasion (getattr, string-built imports, dynamic paths) is
EXPECTED and is contained by the env-scrub + sandbox, not here. Ambiguous dynamic
file paths are ALLOWED by this layer to avoid false-rejecting the normal
`cq.exporters.export(part, os.path.join(OUT, ...))` idiom.

Configurable: HERMES_CAD_NO_AST_CHECK=1 disables it entirely; the banned sets are
module-level constants a caller can extend via check_code(..., banned_modules=).
"""
from __future__ import annotations

import ast
import os
from typing import Iterable

# Network / process / FFI modules that have no place in a CAD codegen script.
BANNED_MODULES = frozenset({
    "socket", "subprocess", "urllib", "requests", "http", "httplib",
    "ftplib", "smtplib", "telnetlib", "poplib", "imaplib", "xmlrpc",
    "ctypes", "cffi", "multiprocessing", "pty", "asyncio",
    "socketserver", "asyncore", "asynchat", "paramiko", "fabric",
})

# Builtins that execute arbitrary code or import dynamically.
BANNED_BUILTINS = frozenset({"eval", "exec", "compile", "__import__", "input"})

# os.<attr> calls that spawn shells/processes.
BANNED_OS_CALLS = frozenset({
    "system", "popen", "popen2", "popen3", "popen4", "fork", "forkpty",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "posix_spawn", "posix_spawnp", "startfile", "abort",
})

# pathlib write methods that bypass open().
_PATH_WRITE_METHODS = frozenset({"write_text", "write_bytes"})

DEFAULT_MAX_BYTES = 100 * 1024  # 100 KB


def _is_write_mode(mode: str) -> bool:
    return any(c in mode for c in ("w", "a", "x", "+"))


def _literal_path(node: ast.AST) -> str | None:
    """Best-effort static extraction of a path literal from an open()/Path() arg.

    Returns the literal string if the path is a plain constant, else None
    (meaning "dynamic — can't prove, so don't reject here").
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _path_mentions_cad_out(node: ast.AST) -> bool:
    """True if the path expression clearly references CAD_OUT / os.environ / a
    relative cwd path. Used to ALLOW the normal export idiom.

    We dump the arg's source-ish names: any reference to CAD_OUT, environ, or a
    non-absolute literal is treated as in-bounds (cwd is CAD_OUT at runtime).
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in ("OUT", "CAD_OUT", "OUTDIR", "out_dir", "outdir"):
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "environ":
            return True
        if isinstance(sub, ast.Subscript):
            # os.environ["CAD_OUT"] style
            v = sub.value
            if isinstance(v, ast.Attribute) and v.attr == "environ":
                return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            s = sub.value
            if "CAD_OUT" in s:
                return True
    return False


def _absolute_or_home_literal(path: str) -> bool:
    """A literal path that escapes the CAD_OUT sandbox: absolute or ~-rooted."""
    return path.startswith("/") or path.startswith("~") or path.startswith("\\\\")


class _Visitor(ast.NodeVisitor):
    def __init__(self, banned_modules: frozenset[str]):
        self.banned_modules = banned_modules
        self.violations: list[str] = []

    def _add(self, lineno: int, msg: str) -> None:
        self.violations.append(f"line {lineno}: {msg}")

    # -- imports --
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in self.banned_modules:
                self._add(node.lineno, f"banned import '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            top = node.module.split(".")[0]
            if top in self.banned_modules:
                self._add(node.lineno, f"banned import from '{node.module}'")
        self.generic_visit(node)

    # -- calls --
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # bare-name builtins: eval(), exec(), __import__(), compile(), input()
        if isinstance(func, ast.Name) and func.id in BANNED_BUILTINS:
            self._add(node.lineno, f"banned builtin '{func.id}(...)'")
        # open(path, mode) writing outside CAD_OUT
        if isinstance(func, ast.Name) and func.id == "open":
            self._check_open(node)
        # attribute calls: os.system(...), os.popen(...), Path(...).write_text(...)
        if isinstance(func, ast.Attribute):
            self._check_attr_call(node, func)
        self.generic_visit(node)

    def _check_open(self, node: ast.Call) -> None:
        mode = "r"
        if len(node.args) >= 2:
            m = _literal_path(node.args[1])
            if m is not None:
                mode = m
        for kw in node.keywords:
            if kw.arg == "mode":
                m = _literal_path(kw.value)
                if m is not None:
                    mode = m
        if not _is_write_mode(mode):
            return  # reads are fine
        if not node.args:
            return
        target = node.args[0]
        lit = _literal_path(target)
        if lit is not None and _absolute_or_home_literal(lit):
            # a literal absolute/home path that doesn't mention CAD_OUT -> reject
            if "CAD_OUT" not in lit and not _path_mentions_cad_out(target):
                self._add(node.lineno, f"open() writing outside CAD_OUT: {lit!r}")
        # dynamic / relative / CAD_OUT-referencing paths: allowed (sandbox covers)

    def _check_attr_call(self, node: ast.Call, func: ast.Attribute) -> None:
        attr = func.attr
        # os.system / os.popen / os.exec* / os.spawn* / os.fork ...
        if attr in BANNED_OS_CALLS:
            base = func.value
            if isinstance(base, ast.Name) and base.id == "os":
                self._add(node.lineno, f"banned os.{attr}(...)")
            elif isinstance(base, ast.Attribute) and base.attr == "os":
                self._add(node.lineno, f"banned os.{attr}(...)")
        # subprocess.* called via attribute even if 'subprocess' slipped past
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            self._add(node.lineno, f"banned subprocess.{attr}(...)")
        # pathlib Path(...).write_text/write_bytes to an absolute/home literal
        if attr in _PATH_WRITE_METHODS:
            self._check_path_write(node, func)

    def _check_path_write(self, node: ast.Call, func: ast.Attribute) -> None:
        # func.value is the Path(...) expression; find a literal path inside it
        base = func.value
        if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "Path":
            if base.args:
                lit = _literal_path(base.args[0])
                if lit is not None and _absolute_or_home_literal(lit) and "CAD_OUT" not in lit:
                    self._add(node.lineno, f"Path.{func.attr}() writing outside CAD_OUT: {lit!r}")


def check_code(
    src: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    banned_modules: Iterable[str] | None = None,
) -> dict:
    """Static safety pre-check for generated CadQuery/Python code (ADR-0003).

    Returns {ok: bool, violations: [str], skipped: bool}. ``ok`` is True only when
    there are no violations. ``skipped`` is True when disabled via
    HERMES_CAD_NO_AST_CHECK (ok is then forced True). Defense-in-depth only —
    NOT the security boundary.
    """
    if os.environ.get("HERMES_CAD_NO_AST_CHECK"):
        return {"ok": True, "violations": [], "skipped": True}

    violations: list[str] = []

    # size cap first (cheap DoS guard)
    if len(src.encode("utf-8", "ignore")) > max_bytes:
        violations.append(
            f"code too large ({len(src)} bytes > {max_bytes} byte cap)"
        )
        return {"ok": False, "violations": violations, "skipped": False}

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {
            "ok": False,
            "violations": [f"line {e.lineno or 0}: syntax error: {e.msg}"],
            "skipped": False,
        }

    bm = frozenset(banned_modules) if banned_modules is not None else BANNED_MODULES
    visitor = _Visitor(bm)
    visitor.visit(tree)
    violations.extend(visitor.violations)

    return {"ok": not violations, "violations": violations, "skipped": False}
