"""hermes-text-to-cad plugin entry point.

Registers the closed-loop CAD tools and a `hermes cad` CLI (setup/doctor).
Tools shell out to a dedicated CAD venv (~/.venvs/cad) so the heavy CAD stack
never imports into the Hermes venv.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from hermes_text_to_cad import core

logger = logging.getLogger(__name__)


# ---- tool handlers (return dicts; the loader JSON-encodes) -------------------

def _cad_generate(code: str, out_dir: str | None = None, stem: str = "part") -> dict[str, Any]:
    return core.generate(code=code, out_dir=out_dir, stem=stem)


def _cad_render(stl: str) -> dict[str, Any]:
    return core.render(stl=stl)


def _cad_measure(stl: str, spec_path: str | None = None) -> dict[str, Any]:
    return core.measure(stl=stl, spec_path=spec_path)


def _cad_review(montage: str, spec_path: str, models: str | None = None) -> dict[str, Any]:
    return core.review(montage=montage, spec_path=spec_path, models=models)


# ---- CLI: `hermes cad <subcommand>` -----------------------------------------

def _cli_cad(args) -> int:
    sub = getattr(args, "cad_subcommand", None)
    if sub == "doctor":
        rep = core.doctor()
        print(json.dumps(rep, indent=2))
        return 0 if rep.get("ready") else 1
    if sub == "setup":
        rep = core.setup()
        print(json.dumps(rep, indent=2))
        return 0 if rep.get("success") else 1
    print("usage: hermes cad {doctor|setup}")
    return 2


def _setup_cli(parser) -> None:
    parser.set_defaults(_handler=_cli_cad)
    sp = parser.add_subparsers(dest="cad_subcommand")
    sp.add_parser("doctor", help="Check CAD venv, render display, vision-gate key")
    sp.add_parser("setup", help="Provision the ~/.venvs/cad venv (uv preferred)")


# ---- register ----------------------------------------------------------------

def register(ctx: Any) -> None:
    ctx.register_tool(
        name="cad_generate",
        fn=_cad_generate,
        description=("Execute CadQuery Python code to build a parametric 3D model. The code "
                     "must export <stem>.stl and <stem>.step into the CAD_OUT directory. "
                     "Returns paths to the STL (mesh, for render/measure/print) and STEP "
                     "(B-rep, for manufacturing). Step 2 of the closed CAD loop."),
        schema={
            "type": "object",
            "required": ["code"],
            "properties": {
                "code": {"type": "string", "description": "CadQuery Python source; exports STL+STEP into CAD_OUT (env)."},
                "out_dir": {"type": "string", "description": "Output dir; temp dir if omitted."},
                "stem": {"type": "string", "description": "Filename stem (default 'part')."},
            },
        },
    )
    ctx.register_tool(
        name="cad_render",
        fn=_cad_render,
        description=("Render an STL into a 3-view (front/top/iso) montage PNG, headless. Uses "
                     "VTK via a display when available, falls back to matplotlib. Step 3 — "
                     "the input to the vision gate."),
        schema={
            "type": "object",
            "required": ["stl"],
            "properties": {"stl": {"type": "string", "description": "Path to the .stl file."}},
        },
    )
    ctx.register_tool(
        name="cad_measure",
        fn=_cad_measure,
        description=("Numeric gate: measure an STL (bbox, watertight, volume, shells) and assert "
                     "against a spec JSON. gate_pass=True only if all checks pass. Carries the "
                     "PRECISION load of verification — vision cannot judge exact dims. Step 4."),
        schema={
            "type": "object",
            "required": ["stl"],
            "properties": {
                "stl": {"type": "string", "description": "Path to the .stl file."},
                "spec_path": {"type": "string", "description": "Path to spec.json (bbox_mm, watertight, max_shells, min/max_volume_mm3)."},
            },
        },
    )
    ctx.register_tool(
        name="cad_review",
        fn=_cad_review,
        description=("Qualitative gate: cross-family multi-model VISION review of the render montage "
                     "against the spec (intent match, feature presence/placement, defects). Needs "
                     "OPENROUTER_API_KEY. Catches intent errors the numeric gate cannot. Step 5."),
        schema={
            "type": "object",
            "required": ["montage", "spec_path"],
            "properties": {
                "montage": {"type": "string", "description": "Path to the 3-view montage PNG."},
                "spec_path": {"type": "string", "description": "Path to spec.json."},
                "models": {"type": "string", "description": "Optional comma-separated model slugs (default: gemini/opus/gpt cross-family)."},
            },
        },
    )

    ctx.register_cli_command("cad", _setup_cli)
    logger.info("hermes-text-to-cad: registered 4 tools + `cad` CLI")
