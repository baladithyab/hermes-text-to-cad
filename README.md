# hermes-text-to-cad

A **verified** text-to-CAD plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Generate parametric 3D models from natural language through a **closed verification loop** — not a one-shot black box.

The differentiator vs every other text-to-CAD tool: **two independent accept gates**.
1. **Numeric gate** — deterministic geometry checks (bounding box, watertight/printable, volume, connected-body count) asserted against a spec. Carries the *precision* load.
2. **Vision gate** — cross-family multi-model review of rendered views (does it *look like* the thing requested?). Carries the *intent* load.

A model is only "done" when **both** gates pass. This is the architecture the 2025–26 research literature (CADTests, CADCodeVerify, CAD-Coder) independently converged on.

## The loop

```
prompt → derive spec (NL → machine-checkable assertions) →
  generate (CadQuery) → render (3-view PNG) →
  ├─ numeric gate (trimesh): bbox / watertight / volume / shells
  │                          + through-holes (genus) / solidity  vs spec
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
| `cad_spec_from_prompt` | 1 | Derive a machine-checkable geometric spec (bbox / through-holes / shells) from an NL prompt — deterministic, no model call |
| `cad_generate` | 2 | Run CadQuery code → export STL (mesh) + STEP (manufacturing B-rep) |
| `cad_render`   | 3 | Headless 3-view montage PNG (VTK, matplotlib fallback) |
| `cad_measure`  | 4 | Numeric gate: measure + assert vs spec.json (`gate_pass` bool); now includes through-hole (genus) & solidity checks |
| `cad_review`   | 5 | Cross-family vision gate (needs `OPENROUTER_API_KEY`) |

Plus a `hermes cad` CLI: `cad doctor` (readiness) and `cad setup` (provision the CAD venv).

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
