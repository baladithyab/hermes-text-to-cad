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

# os.<attr> file-move/link calls: attr -> index of the DESTINATION arg. A move to
# an absolute/home/.. literal escapes CAD_OUT just like an open(...,'w') would.
_OS_MOVE_CALLS = {"rename": 1, "replace": 1, "link": 1, "symlink": 1}

# os.open flag attributes that imply a write (vs O_RDONLY, the default read).
_OS_WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"})

DEFAULT_MAX_BYTES = 100 * 1024  # 100 KB


def _is_write_mode(mode: str) -> bool:
    return any(c in mode for c in ("w", "a", "x", "+"))


def _has_write_flag(node: ast.AST) -> bool:
    """True if an os.open flags expression statically contains a write flag
    (os.O_WRONLY / O_RDWR / O_CREAT / O_APPEND / O_TRUNC), possibly OR'd."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _OS_WRITE_FLAGS:
            return True
        if isinstance(sub, ast.Name) and sub.id in _OS_WRITE_FLAGS:
            return True
    return False


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
    """A literal path that escapes CAD_OUT: absolute, ~-rooted, UNC, or containing
    a '..' parent-dir segment (cwd=CAD_OUT is NOT a boundary for '..')."""
    if path.startswith("/") or path.startswith("~") or path.startswith("\\\\"):
        return True
    parts = path.replace("\\", "/").split("/")
    return ".." in parts


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

    def _escaping_literal(self, target: ast.AST) -> str | None:
        """If a write-target expression PROVABLY escapes CAD_OUT, return the
        offending literal string; else None (dynamic/relative-in-bounds paths are
        allowed — the sandbox covers what the AST can't statically resolve).

        Handles a bare string literal AND os.path.join(...) with literal parts
        (so os.path.join('..','..','etc') and os.path.join('/etc','x') are caught,
        while os.path.join(OUT, 'part.stl') is allowed).
        """
        # CAD_OUT-referencing expressions are always in-bounds.
        if _path_mentions_cad_out(target):
            return None
        lit = _literal_path(target)
        if lit is not None:
            if _absolute_or_home_literal(lit) and "CAD_OUT" not in lit:
                return lit
            return None
        # os.path.join(a, b, ...) with literal parts
        if (isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute)
                and target.func.attr == "join"):
            parts = [_literal_path(a) for a in target.args]
            str_parts = [p for p in parts if p is not None]
            joined = "/".join(str_parts)
            # an absolute first part, a '~', or a '..' segment anywhere -> escape
            if str_parts and _absolute_or_home_literal(str_parts[0]):
                return joined
            if any(".." == p or ".." in p.replace("\\", "/").split("/") for p in str_parts):
                return joined
        return None

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
        esc = self._escaping_literal(node.args[0])
        if esc is not None:
            self._add(node.lineno, f"open() writing outside CAD_OUT: {esc!r}")

    def _check_attr_call(self, node: ast.Call, func: ast.Attribute) -> None:
        attr = func.attr
        base = func.value
        is_os = (isinstance(base, ast.Name) and base.id == "os") or \
                (isinstance(base, ast.Attribute) and base.attr == "os")
        # os.system / os.popen / os.exec* / os.spawn* / os.fork ...
        if attr in BANNED_OS_CALLS and is_os:
            self._add(node.lineno, f"banned os.{attr}(...)")
        # os.open(path, flags) with a write flag -> a write to the target path
        if attr == "open" and is_os:
            self._check_os_open(node)
        # os.rename/replace/link/symlink(src, DEST) escaping CAD_OUT
        if attr in _OS_MOVE_CALLS and is_os:
            self._check_move(node, f"os.{attr}", _OS_MOVE_CALLS[attr])
        # shutil.move(src, DEST)
        if attr == "move" and isinstance(base, ast.Name) and base.id == "shutil":
            self._check_move(node, "shutil.move", 1)
        # subprocess.* called via attribute even if 'subprocess' slipped past
        if isinstance(base, ast.Name) and base.id == "subprocess":
            self._add(node.lineno, f"banned subprocess.{attr}(...)")
        # pathlib Path(...).write_text/write_bytes to an escaping literal
        if attr in _PATH_WRITE_METHODS:
            self._check_path_write(node, func)

    def _check_os_open(self, node: ast.Call) -> None:
        # write only when the flags arg statically carries a write flag; a bare
        # os.open(path) or O_RDONLY is a read (allowed).
        flags = node.args[1] if len(node.args) >= 2 else None
        for kw in node.keywords:
            if kw.arg == "flags":
                flags = kw.value
        if flags is None or not _has_write_flag(flags):
            return
        if not node.args:
            return
        esc = self._escaping_literal(node.args[0])
        if esc is not None:
            self._add(node.lineno, f"os.open() writing outside CAD_OUT: {esc!r}")

    def _check_move(self, node: ast.Call, what: str, dest_idx: int) -> None:
        if len(node.args) <= dest_idx:
            return
        esc = self._escaping_literal(node.args[dest_idx])
        if esc is not None:
            self._add(node.lineno, f"{what}() moving outside CAD_OUT: {esc!r}")

    def _check_path_write(self, node: ast.Call, func: ast.Attribute) -> None:
        # func.value is the Path(...) expression; check its first literal arg
        base = func.value
        if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "Path":
            if base.args:
                esc = self._escaping_literal(base.args[0])
                if esc is not None:
                    self._add(node.lineno, f"Path.{func.attr}() writing outside CAD_OUT: {esc!r}")


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
        # Emit the canonical CamelCase token "SyntaxError" so the ReAct loop's
        # summarize_error() hint mapping (which greps for "SyntaxError") fires and
        # the agent gets an actionable hint (review finding #6).
        return {
            "ok": False,
            "violations": [f"line {e.lineno or 0}: SyntaxError: {e.msg}"],
            "skipped": False,
        }

    bm = frozenset(banned_modules) if banned_modules is not None else BANNED_MODULES
    visitor = _Visitor(bm)
    visitor.visit(tree)
    violations.extend(visitor.violations)

    return {"ok": not violations, "violations": violations, "skipped": False}
