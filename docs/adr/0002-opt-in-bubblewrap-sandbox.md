# 2. Opt-in bubblewrap sandbox for generated code

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (SECURITY wave)

## Context and Problem Statement

A scrubbed env (ADR-0001) stops secret *exfiltration via env*, but generated code
can still read files (`~/.ssh`, `~/.hermes/.env`), write anywhere, and open
network sockets. For untrusted use we want OS-level confinement: no network,
read-only filesystem except `CAD_OUT`, no access to dotfiles/secrets.

## Considered Options

1. **bubblewrap (`bwrap`)** — userspace container, no root, fine-grained binds.
2. **firejail** — seccomp/namespace sandbox, profile-driven.
3. **No sandbox, env-scrub only** — rely on ADR-0001 + AST denylist.
4. **Always-on sandbox** — mandatory bwrap.

## Decision Outcome

Chosen: **Option 1, opt-in via `HERMES_CAD_SANDBOX=1`**, with **firejail as a
documented fallback** and graceful degradation when neither is present.

Verified on the dev box: a single `bwrap` invocation gives `--unshare-net`
(network blocked), `--ro-bind / /` (root read-only), `--bind CAD_OUT CAD_OUT`
(writable output), and `--tmpfs $HOME/.hermes` / `--tmpfs $HOME/.ssh` (masks
secrets inside the sandbox while the host copies stay intact).

Opt-in (not always-on) because: (a) bwrap isn't universally installed, (b) the
default trusted-use case shouldn't pay the setup cost or risk a sandbox-induced
failure, (c) the README states the trust assumption so operators choose. When
`HERMES_CAD_SANDBOX=1` is set but no sandbox binary exists, we **warn loudly and
run unsandboxed** rather than silently failing closed (a hard failure would break
trusted users who set the flag globally) — the warning is logged at WARNING and
surfaced in the result dict (`sandbox: "requested-unavailable"`).

### Consequences

- Good: untrusted-use operators get real network+FS confinement with one env var.
- Good: detection + degradation means it never hard-breaks a box without bwrap.
- Bad: the warn-and-run-unsandboxed degrade is a softer default than fail-closed;
  documented explicitly so it's a conscious choice, and `cad doctor` reports
  sandbox availability so operators can see the gap before relying on it.
- Neutral: only `generate` is sandboxed by default (it runs model code);
  render/measure/review run trusted repo scripts, though the same wrapper is
  reusable for them.
