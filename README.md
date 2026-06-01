# hermes-text-to-cad

A **verified** text-to-CAD plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Generate parametric 3D models from natural language through a **closed verification loop** — not a one-shot black box.

The differentiator vs every other text-to-CAD tool: **two independent accept gates**.
1. **Numeric gate** — deterministic geometry checks (bounding box, watertight/printable, volume, connected-body count) asserted against a spec. Carries the *precision* load.
2. **Vision gate** — cross-family multi-model review of rendered views (does it *look like* the thing requested?). Carries the *intent* load.

A model is only "done" when **both** gates pass. This is the architecture the 2025–26 research literature (CADTests, CADCodeVerify, CAD-Coder) independently converged on.

## The loop

```
prompt → derive spec (NL → machine-checkable assertions + intent/frame/features) →
  generate (CadQuery → STL + STEP + GLB + topology sidecar) →
  render (PBR studio 3-view PNG, per-body color from the GLB) →
  ├─ numeric gate (trimesh): bbox / watertight / volume / shells
  │                          + through-holes (genus) / solidity
  │                          + manifold / centroid / symmetry / placement  vs spec
  └─ vision gate (Gemini + Opus + GPT, cross-family): intent match
→ iterate on convergent findings → repeat (hard cap, keep best-so-far)
```

The numeric gate is **prompt-derived** (the CADTests finding: executable
geometric tests beat vision-only feedback). `cad_spec_from_prompt` turns
*"a 40×30×20 block with a through-hole"* into assertions including
`through_holes == 1` — so a solid block with the right bounding box but **no
hole** fails the gate, a case a bbox-only check silently passes.

## Tools

| Tool | Step | What it does |
|------|------|--------------|
| `cad_spec_from_prompt` | 1 | Derive a machine-checkable geometric spec from an NL prompt — bbox / through-holes / shells / internal-features **plus** intent, coordinate frame, named features, assumptions, and symmetry ([spec contract v2](docs/adr/0011-spec-contract-v2-and-placement-gate.md)). Deterministic, no model call |
| `cad_plan` | 1.5 | Emit a structured CoT modeling plan (primitives → operations → features → export) seeded from the derived spec, now with a coordinate-frame + per-feature placement narrative, a `claims_require` self-check ("never claim done unless the tool ran"), and `repair_classes` — optional, deterministic ([CAD-Coder](docs/adr/0007-cad-plan-cot-step.md)) |
| `cad_generate` | 2 | Run CadQuery (→ STL + STEP) or OpenSCAD (`backend="openscad"`, → STL) modeling code. On success also emits **`<stem>.glb`** (per-body-color PBR render input) + **`<stem>.topology.json`** (machine-readable sidecar) unless `emit_glb=false` ([ADR-0013](docs/adr/0013-generator-artifacts-glb-topology-sidecar.md)). Executes model-authored code — see [Security](#️-security-cad_generate-executes-model-authored-python) |
| `cad_render`   | 3 | Headless 3-view montage PNG with **PBR studio lighting** (GLTF import + image-based lighting + 3-point rig + soft shadows + filmic tone mapping — [ADR-0010](docs/adr/0010-pbr-render-path.md)); prefers a sibling `.glb` for per-body color, falls back to STL then matplotlib (display → OSMesa → Xvfb → matplotlib precedence); `section` adds an internal-feature cutaway view |
| `cad_measure`  | 4 | Numeric gate: measure + assert vs spec.json (`gate_pass` bool); through-hole (genus) & solidity, **plus manifold / degenerate-face / centroid / symmetry / per-body placement** checks ([placement gate](docs/adr/0011-spec-contract-v2-and-placement-gate.md)) — *numeric-PASS ≠ correct placement* |
| `cad_compare`  | 4b | Similarity gate: Chamfer Distance vs a reference mesh for "make it like THIS" tasks |
| `cad_review`   | 5 | Cross-family vision gate — structured Yes/No-Q&A (CADCodeVerify) by default (needs `OPENROUTER_API_KEY`) |

Plus a `hermes cad` CLI: `cad doctor` (readiness, incl. headless-GL / sandbox / OpenSCAD detection) and `cad setup` (provision the CAD venv).

## ReAct error-feedback loop

CadQuery's OCC kernel reports most B-rep failures (a fillet radius larger than
its edge, an invalid shell, a degenerate sketch) as the same opaque
`StdFail_NotDone: BRep_API: command not done`. Rather than let the model flail,
`cad_generate` returns the full `stderr`, and `core.summarize_error()` turns the
traceback into a structured, actionable observation:

```jsonc
{ "error_type": "StdFail_NotDone",
  "message": "BRep_API: command not done",
  "hint": "OCC kernel could not complete… a fillet/chamfer radius larger than
           the adjacent edge — reduce the radius or fillet fewer edges." }
```

The agent feeds that back via `core.iterate(prompt, history, last_error)` — the
thin ReAct contract that assembles the next attempt's context (attempt #,
derived spec, summarized error, instruction). `core.generate_with_retry(code_fn,
prompt, max_iters=3)` is the executable driver: it regenerates **with the error
in context** until the part builds or the cap is hit, keeping a per-attempt
history. The loop is LLM-free in `core` — the agent is the caller-supplied
`code_fn(observation) -> code` — so it's deterministic and fully unit-tested.
A 10 mm cube asked for a 50 mm fillet fails on attempt 1 and auto-corrects to a
valid radius on attempt 2 (verified in `tests/test_integration.py`).

## Install

> ⚠️ Three steps. A bare `pip install` is **not** enough — the Hermes loader only discovers plugins under `~/.hermes/plugins/<name>/`, and deps must land in the **Hermes venv**, not your shell's python.

```bash
# 1. Install the plugin (clones into ~/.hermes/plugins/hermes-text-to-cad/)
hermes plugins install baladithyab/hermes-text-to-cad

# 2. Enable it + restart the gateway so the `hermes cad` CLI registers
hermes plugins enable hermes-text-to-cad
hermes gateway restart      # or start a fresh `hermes` CLI session

# 3. Provision the CAD venv (CadQuery + trimesh + vtk + ... via uv, no root needed)
hermes cad setup
#   — or manually:
#   uv venv ~/.venvs/cad --python 3.11
#   uv pip install --python ~/.venvs/cad/bin/python cadquery trimesh scipy numpy vtk matplotlib pillow

# 4. Verify
hermes cad doctor
```

The plugin itself imports nothing heavy into the Hermes venv — the CAD stack lives in `~/.venvs/cad` and is invoked as a subprocess. Override the interpreter with `HERMES_CAD_PYTHON`.

## ⚠️ Security: `cad_generate` executes model-authored Python

`cad_generate` runs **arbitrary, LLM-authored Python** in a subprocess — that is
how it builds the model. Treat generated code as untrusted input. Three defense
layers ship by default-or-opt-in (full detail in [`SECURITY.md`](SECURITY.md)):

1. **AST denylist** (always on) — rejects obvious exfil/abuse (`import socket`,
   `os.system`, `eval`, file writes outside the output dir) *before* execution.
2. **Scrubbed env** (always on) — the subprocess gets a minimal allowlisted
   environment; `OPENROUTER_API_KEY` and all secrets are dropped, so generated
   code can never read them from `os.environ`.
3. **Opt-in OS sandbox** — set **`HERMES_CAD_SANDBOX=1`** to confine generated
   code with bubblewrap (no network, read-only filesystem except the output dir,
   secret dirs masked). firejail is the fallback.

> **For untrusted use, run with `HERMES_CAD_SANDBOX=1`.** `hermes cad doctor`
> reports whether a sandbox tool (`bwrap`/`firejail`) is installed. Without one,
> the flag warns and runs unsandboxed (it degrades, it does not fail).

## Requirements

- **CadQuery** (primary backend) installs without root and exports STEP + real fillets/shells.
- **Render gate** picks a backend by precedence (see [ADR-0004](docs/adr/0004-headless-render-precedence.md)): an existing `DISPLAY` (WSLg `:0` / real X) → an OSMesa/EGL VTK build → an auto-launched private `Xvfb` (software GLX) → matplotlib Agg (last resort). The stock pip `vtk` wheel is X11-only and would *hard-abort* with no display, so `render.py` first probes GL in a disposable child process and only renders in-process once a context is confirmed — a headless box never crashes the render step. `hermes cad doctor`'s `render_headless_gl` check reports which path is live. For real z-buffered VTK on a headless server, install an OSMesa VTK build into the CAD venv or ensure `Xvfb` is on `PATH`.
- **Vision gate** needs `OPENROUTER_API_KEY` in `~/.hermes/.env`. Optional — the numeric gate works without it.

## Backends

| Backend | When | Notes |
|---------|------|-------|
| **CadQuery** (default) | mechanical parts, anything needing STEP / fillets / shells | B-rep kernel (OpenCascade), no root |
| OpenSCAD (optional) | CSG-style authoring, parameter sliders | needs `openscad` binary; mesh-only |
| Zoo/KCL (optional) | organic / freeform surfaces | cloud, needs `ZOO_API_TOKEN` |

## Render quality (PBR studio)

The render gate feeds the vision gate, so render legibility *is* a correctness
lever. `cad_render` uses a physically-based studio pipeline ([ADR-0010](docs/adr/0010-pbr-render-path.md)):
`vtkGLTFImporter` loads a colored multi-body GLB (one PBR actor per body, per-body
color preserved), lit by a procedural equirectangular **image-based light** + a
3-point studio rig with **soft shadows**, and tone-mapped with VTK's in-pipeline
**GenericFilmic** curve — all stock VTK 9.3, no new dependencies. A material-less
STL gets a synthesized neutral studio material; matplotlib remains the no-GL
fallback. The result reads as a product shot (per-body color, specular, grounded
shadow) instead of a flat mono-color blob, so the vision gate sees a legible part.

## Status

v0.3.0 closed-loop core + a **reference-uplift** pass (clean-room study of two
public CAD repos → original implementations; see
[`docs/research/reference-steal.md`](docs/research/reference-steal.md)). Done and verified:
**SECURITY** (scrubbed secret-free subprocess env, AST denylist, opt-in bubblewrap
sandbox — see [`SECURITY.md`](SECURITY.md)); **prompt-derived geometric tests** +
ReAct error-feedback; **structured-Q&A vision gate**, Chamfer-Distance `cad_compare`,
`cad_plan` CoT step; **crash-safe headless rendering**, section/cutaway renders,
OpenSCAD backend; **PBR studio render** with per-body GLB color ([ADR-0010](docs/adr/0010-pbr-render-path.md));
**spec contract v2** (intent / coordinate frame / named features / assumptions /
symmetry) + a **placement/manifold gate** (centroid, symmetry, manifold,
degenerate-face, per-body checks — *numeric-PASS ≠ correct placement*,
[ADR-0011](docs/adr/0011-spec-contract-v2-and-placement-gate.md)); a first-party
**parametric geometry-helper library** ([ADR-0012](docs/adr/0012-first-party-geometry-helper-library.md));
and a **generator contract** that emits STL + STEP + GLB + topology sidecar
([ADR-0013](docs/adr/0013-generator-artifacts-glb-topology-sidecar.md)). Decisions
recorded as ADRs in [`docs/adr/`](docs/adr/). See [`PLAN.md`](PLAN.md) for the
remaining backlog (Wave 3.4 Zoo/KCL organic fallback, Wave 4 polish).

## Credit

The OpenSCAD-target pattern is inspired by [CADAM (Adam-CAD)](https://github.com/adam-cad) (GPL-3.0) — pattern only, no code vendored. CadQuery-as-target and the geometric/visual verification loop align with CAD-Coder (NeurIPS 2025), CADCodeVerify (ICLR 2025), and CADTests (2026). MIT licensed.
