# 3. AST denylist as a cheap first line of defense

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (SECURITY wave)

## Context and Problem Statement

Before executing generated code we want a cheap, dependency-free reject of
obvious exfil/abuse (`import socket`, `import subprocess`, `os.system`,
`__import__`, `eval`/`exec`, network libs, file writes outside `CAD_OUT`). This is
**defense-in-depth**, not the only layer — the scrubbed env (ADR-0001) and the
opt-in sandbox (ADR-0002) are the real boundaries.

## Considered Options

1. **AST parse + node-type denylist** — `ast.parse`, walk, reject banned
   imports/calls/attributes; reject on `SyntaxError`.
2. **Regex/substring scan** — grep the source for `socket`, `os.system`, …
3. **No static check** — rely solely on env-scrub + sandbox.

## Decision Outcome

Chosen: **Option 1 (AST)**. Regex is trivially evaded (`__import__("so"+"cket")`,
whitespace, aliasing) and produces false positives on strings/comments. An AST
walk sees real import/call structure: banned module imports (`socket`,
`subprocess`, `urllib`, `requests`, `http`, `ftplib`, `smtplib`, `ctypes`,
`multiprocessing`, `pty`, `signal`), dangerous builtins (`eval`, `exec`,
`compile`, `__import__`, `input`), `os.system`/`os.popen`/`os.exec*`/`os.spawn*`,
and `open(..., 'w'|'a'|'x'|...)` / `Path.open(...,'w')` whose target is not
provably under `CAD_OUT`.

Design constraints:
- **Configurable/overridable**: `HERMES_CAD_NO_AST_CHECK=1` disables it; the
  banned-import set is a module-level constant a caller can extend. It is NOT the
  security boundary, so disabling it only drops the cheap pre-filter.
- **AST evasion is expected** (`getattr`, string-built imports). We do not claim
  completeness — anything the AST can't statically resolve, the env-scrub +
  sandbox still contain. The denylist exists to reject the 95% obvious case fast
  and give a clear error message, not to be a verifier.
- **Open() heuristic**: we allow `open()` when the path argument is a literal/
  f-string/`os.path.join(...)`/`Path(...)` that contains `CAD_OUT` or `os.environ`
  or is clearly relative (cwd is `CAD_OUT`); we reject absolute writes elsewhere
  (`/etc/...`, `~/...`). Read modes are always allowed. Ambiguous dynamic paths
  are **allowed by the AST layer** (the sandbox/env-scrub catch them) to avoid
  false-rejecting legitimate `cq.exporters.export(part, os.path.join(OUT, ...))`.

### Consequences

- Good: instant, dependency-free reject of blatant abuse with an actionable
  message; legitimate cadquery code passes (proven by tests both ways).
- Bad: false sense of security if treated as THE defense — mitigated by docs
  framing it as layer 1 of 3 and by ADR-0002 being the real boundary.
- Neutral: a code-size cap (reject absurdly large blobs, default 100 KB) lives
  alongside the AST check as a trivial DoS guard.
