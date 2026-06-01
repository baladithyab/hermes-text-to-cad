# Security model — hermes-text-to-cad

## TL;DR

`cad_generate` **executes model-authored Python** in a subprocess. That is the
entire point of the tool — an LLM writes CadQuery code and we run it to build a
mesh. Treat generated code as **untrusted input**.

- For **trusted use** (you wrote/reviewed the prompt, you trust the model and the
  loop): the default configuration is fine. Generated code runs with a scrubbed,
  secret-free environment and a static safety pre-check.
- For **untrusted use** (exposed to arbitrary users, multi-tenant, or you can't
  vet prompts): set **`HERMES_CAD_SANDBOX=1`** so generated code runs inside a
  bubblewrap container with no network and a read-only filesystem.

Run `hermes cad doctor` to see whether a sandbox tool is installed.

## Threat model

| Asset | Threat | Vector |
|-------|--------|--------|
| API keys / secrets in the Hermes process env (`OPENROUTER_API_KEY`, cloud creds) | Exfiltration | Generated code reads `os.environ` and writes/sends it |
| Files outside the output dir (`~/.ssh`, `~/.hermes/.env`, source) | Read/exfil or tamper | Generated code opens arbitrary paths |
| Network | Exfiltration / SSRF / C2 | Generated code opens sockets / HTTP |
| Host process / other tenants | Escalation, DoS | `os.system`, `subprocess`, fork bombs, huge code blobs |

**In scope:** a model (possibly steered by a malicious prompt) emitting Python
that tries to read secrets, touch the filesystem, or reach the network.

**Out of scope:** a compromised CAD venv / supply-chain attack on cadquery itself,
kernel exploits that defeat bubblewrap, and a malicious *operator* who sets
`HERMES_CAD_NO_AST_CHECK=1` and disables the sandbox. The plugin defends against
malicious *generated code*, not a malicious host operator.

## Defense in depth — three layers

Layered so that defeating one does not defeat the others. The OS sandbox (layer
3) is the real boundary; layers 1–2 are cheap and always-on.

### Layer 1 — static AST denylist (always on, before execution)

`hermes_text_to_cad/safety.check_code()` parses generated code with `ast` and
**rejects before running** (ADR-0003):

- banned imports: `socket`, `subprocess`, `urllib`, `requests`, `http`,
  `ftplib`, `smtplib`, `ctypes`, `multiprocessing`, `pty`, `asyncio`, …
- dangerous builtins: `eval`, `exec`, `compile`, `__import__`, `input`
- `os.system` / `os.popen` / `os.exec*` / `os.spawn*` / `os.fork`
- `open(..., 'w'|'a'|...)` / `Path.write_*` to an absolute, non-`CAD_OUT` path
- syntax errors; code blobs over 100 KB (DoS guard)

This is a **first line, not a verifier.** AST evasion (dynamic imports built from
strings, `getattr`, runtime-computed paths) is expected and is contained by
layers 2–3. Disable with `HERMES_CAD_NO_AST_CHECK=1` (drops only the cheap
pre-filter; the env-scrub and sandbox still apply).

### Layer 2 — scrubbed subprocess environment (always on)

Every CAD subprocess (`generate`/`render`/`measure`/`review`) runs with a
**minimal allowlisted environment** (ADR-0001), never the full `os.environ`.
Allowlist: `PATH`, `HOME`, locale (`LANG`/`LC_*`), `CAD_OUT`, and render/X/GL
knobs (`DISPLAY`, `XAUTHORITY`, …). **`OPENROUTER_API_KEY` and every `*_API_KEY`/
`*_TOKEN`/`*_SECRET` are dropped by construction** — they are not on the
allowlist, so generated code dumping `os.environ` finds nothing.

The vision gate (`scatter_review.py`) needs the OpenRouter key, but it reads the
key from `~/.hermes/.env` **itself** — the key never travels through a subprocess
environment, so even the trusted review path doesn't expose it via env.

### Layer 3 — opt-in OS sandbox (`HERMES_CAD_SANDBOX=1`)

When enabled, the `generate` subprocess is wrapped in **bubblewrap** (`bwrap`,
preferred) or **firejail** (fallback) (ADR-0002):

- `--unshare-net` → **no network** (exfil/SSRF/C2 blocked at the OS).
- `--ro-bind / /` → **read-only filesystem** (covers the interpreter, libs).
- `--bind CAD_OUT CAD_OUT` → the output dir is the **only** writable path.
- `--tmpfs` over `~/.hermes`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`,
  `~/.docker`, `~/.netrc` (and more) → secret dirs are **masked** inside the
  sandbox; the host copies are untouched.

If `HERMES_CAD_SANDBOX=1` is set but no sandbox tool is installed, the plugin
**warns and runs unsandboxed** (degrade, not fail) and reports
`sandbox: "requested-unavailable"` in the result. This is a deliberate choice
(ADR-0002): a hard failure would break operators who set the flag globally.
`hermes cad doctor`'s `sandbox_available` check tells you whether the flag will
actually confine code, so you can decide before relying on it.

## Configuration reference

| Variable | Effect |
|----------|--------|
| `HERMES_CAD_SANDBOX=1` | Wrap generated code in bwrap/firejail (no net, ro-FS, secrets masked). **Set this for untrusted use.** |
| `HERMES_CAD_NO_AST_CHECK=1` | Disable the layer-1 AST pre-check. Use only when you trust the code; layers 2–3 still apply. |
| `HERMES_CAD_PYTHON` | Path to the CAD venv interpreter (read in the parent; never passed to the child). |

## Reporting

This is a research/utility plugin. If you find a sandbox escape or an env-scrub
bypass, open an issue on the repo with a minimal reproducer.
