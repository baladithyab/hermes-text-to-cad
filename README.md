# hermes-text-to-cad

A **verified** text-to-CAD plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Generate parametric 3D models from natural language through a **closed verification loop** — not a one-shot black box.

The differentiator vs every other text-to-CAD tool: **two independent accept gates**.
1. **Numeric gate** — deterministic geometry checks (bounding box, watertight/printable, volume, connected-body count) asserted against a spec. Carries the *precision* load.
2. **Vision gate** — cross-family multi-model review of rendered views (does it *look like* the thing requested?). Carries the *intent* load.

A model is only "done" when **both** gates pass. This is the architecture the 2025–26 research literature (CADTests, CADCodeVerify, CAD-Coder) independently converged on.

## The loop

```
spec → generate (CadQuery) → render (3-view PNG) →
  ├─ numeric gate (trimesh): bbox / watertight / volume / shells  vs spec
  └─ vision gate (Gemini + Opus + GPT, cross-family): intent match
→ iterate on convergent findings → repeat (hard cap, keep best-so-far)
```

## Tools

| Tool | Step | What it does |
|------|------|--------------|
| `cad_generate` | 2 | Run CadQuery code → export STL (mesh) + STEP (manufacturing B-rep) |
| `cad_render`   | 3 | Headless 3-view montage PNG (VTK, matplotlib fallback) |
| `cad_measure`  | 4 | Numeric gate: measure + assert vs spec.json (`gate_pass` bool) |
| `cad_review`   | 5 | Cross-family vision gate (needs `OPENROUTER_API_KEY`) |

Plus a `hermes cad` CLI: `cad doctor` (readiness) and `cad setup` (provision the CAD venv).

## Install

> ⚠️ Three steps. A bare `pip install` is **not** enough — the Hermes loader only discovers plugins under `~/.hermes/plugins/<name>/`, and deps must land in the **Hermes venv**, not your shell's python.

```bash
# 1. Install the plugin (clones into ~/.hermes/plugins/hermes-text-to-cad/)
hermes plugins install baladithyab/hermes-text-to-cad

# 2. Provision the CAD venv (CadQuery + trimesh + vtk + ... via uv, no root needed)
hermes cad setup
#   — or manually:
#   uv venv ~/.venvs/cad --python 3.11
#   uv pip install --python ~/.venvs/cad/bin/python cadquery trimesh scipy numpy vtk matplotlib pillow

# 3. Enable + restart the gateway (plugins load at startup)
hermes plugins enable hermes-text-to-cad
hermes gateway restart      # or start a fresh `hermes` CLI session

# 4. Verify
hermes cad doctor
```

The plugin itself imports nothing heavy into the Hermes venv — the CAD stack lives in `~/.venvs/cad` and is invoked as a subprocess. Override the interpreter with `HERMES_CAD_PYTHON`.

## Requirements

- **CadQuery** (primary backend) installs without root and exports STEP + real fillets/shells.
- **Render gate** needs a display for best quality. On WSL, WSLg provides one at `:0` automatically. Headless servers fall back to matplotlib (lower fidelity) or need an OSMesa VTK build (see roadmap).
- **Vision gate** needs `OPENROUTER_API_KEY` in `~/.hermes/.env`. Optional — the numeric gate works without it.

## Backends

| Backend | When | Notes |
|---------|------|-------|
| **CadQuery** (default) | mechanical parts, anything needing STEP / fillets / shells | B-rep kernel (OpenCascade), no root |
| OpenSCAD (optional) | CSG-style authoring, parameter sliders | needs `openscad` binary; mesh-only |
| Zoo/KCL (optional) | organic / freeform surfaces | cloud, needs `ZOO_API_TOKEN` |

## Status

v0.1.0 — core loop functional and verified. See [`PLAN.md`](PLAN.md) for the research-grounded improvement backlog (prompt-derived geometric tests, ReAct error-feedback, structured-Q&A vision gate, Chamfer scoring, headless-portable rendering).

## Credit

The OpenSCAD-target pattern is inspired by [CADAM (Adam-CAD)](https://github.com/adam-cad) (GPL-3.0) — pattern only, no code vendored. CadQuery-as-target and the geometric/visual verification loop align with CAD-Coder (NeurIPS 2025), CADCodeVerify (ICLR 2025), and CADTests (2026). MIT licensed.
