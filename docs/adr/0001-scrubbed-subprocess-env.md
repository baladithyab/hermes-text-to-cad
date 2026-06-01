# 1. Scrub the subprocess environment with an allowlist

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (SECURITY wave)

## Context and Problem Statement

`cad_generate` runs arbitrary, LLM-authored Python in a subprocess. Today both
subprocess chokepoints (`core._run` and `core.generate`) build the child env as
`dict(os.environ)` — inheriting the **entire** parent environment, including
`OPENROUTER_API_KEY` and any other secret in the Hermes process. A generated
script can `print(os.environ)` and exfiltrate every secret. The venv-subprocess
design isolated the heavy CAD *stack*, not the *secrets*. How do we stop
model-authored code from seeing secrets while keeping the engine working?

## Considered Options

1. **Allowlist a minimal env** — pass only `PATH, HOME, DISPLAY, LANG, LC_*,
   CAD_OUT` (+ a few VTK/X knobs), drop everything else.
2. **Denylist known secrets** — start from full env, delete `*_API_KEY`,
   `*_TOKEN`, `*_SECRET`, etc.
3. **Empty env** — pass `env={}` and reconstruct everything.

## Decision Outcome

Chosen: **Option 1 (allowlist)**. A denylist is a losing game — any
not-yet-patterned secret leaks. An empty env breaks PATH/HOME-dependent tooling
(uv-built interpreters, font/config lookup, VTK). An allowlist is the only
default-deny posture that still runs.

`core._scrubbed_env(extra=None)` is the single source of truth. `generate`,
`render`, `measure`, and `review` all route through it. **Exception:** the
vision gate's `scatter_review.py` legitimately needs `OPENROUTER_API_KEY` — but
it reads the key from `~/.hermes/.env` *itself*, so `review` does **not** need to
pass it in the env. We therefore scrub `review`'s env too; the key never travels
through an env var into any subprocess. `cad_generate` in particular MUST NOT see
the key.

### Consequences

- Good: a secret in the parent env is invisible to generated code (proven by a
  test that sets a fake secret and asserts the generated script can't read it).
- Good: one chokepoint, easy to audit and extend.
- Bad: if a future feature genuinely needs a new env var in the subprocess, it
  must be added to the allowlist explicitly (intended friction).
- Neutral: `HERMES_CAD_PYTHON` is read in the parent to locate the interpreter;
  it is not needed inside the child, so it stays out of the allowlist.
