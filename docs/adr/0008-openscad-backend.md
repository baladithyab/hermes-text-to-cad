# 8. Optional OpenSCAD backend via binary/AppImage, mesh-only

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (Wave 3.3)

## Context and Problem Statement

PLAN Wave 3.3 wants an optional OpenSCAD backend for CSG-style authoring.
`cad_generate` should gain `backend="openscad"`, `cad doctor` should report
openscad availability, and `templates/part.scad` already exists. OpenSCAD needs a
binary; installs may be root-less (AppImage `--appimage-extract`).

## Considered Options

1. **Locate an `openscad` binary** (PATH, common AppImage-extract locations, an
   `HERMES_OPENSCAD_BIN` override); run `openscad -o part.stl part.scad`.
2. **Bundle/auto-download an AppImage** — heavier, network at runtime, trust.
3. **Skip OpenSCAD** — CadQuery only.

## Decision Outcome

Chosen: **Option 1 (locate-or-override)**. We do not vendor or download a binary
(supply-chain + size). `core` gains:

- `openscad_bin()` — resolve via `HERMES_OPENSCAD_BIN`, then `shutil.which`, then
  a small list of `--appimage-extract` squashfs-root locations. Returns None if
  absent.
- `generate(code, ..., backend="cadquery"|"openscad")` — for `openscad`, write
  the code to `part.scad`, run `openscad -o <out>/part.stl <scad>` with the
  scrubbed env (ADR-0001), and (optionally) a PNG. OpenSCAD is **mesh-only** (no
  STEP) — `step` is `None`, documented.
- The AST denylist (ADR-0003) is **CadQuery/Python-specific** and is skipped for
  the OpenSCAD path (SCAD is not Python); the scrubbed env + sandbox still apply.
- `cad doctor` reports `openscad_backend` (available + which binary).

The `backend` arg is additive: default stays `cadquery`, every existing call is
unchanged. When `backend="openscad"` and no binary is found, `generate` returns a
clear `{success: False, error: "openscad backend requested but no binary found
..."}` rather than crashing.

### Consequences

- Good: CSG authoring available when a binary exists; no new hard dep; root-less
  AppImage supported via discovery + override.
- Bad: mesh-only (no STEP); weak fillets — documented (use CadQuery for those).
- Neutral: tests mock the openscad subprocess for unit coverage; a real-binary
  integration test skips when no binary is present (mirrors the CAD-venv skip).
