"""hermes-text-to-cad plugin entry point.

Registers the closed-loop CAD tools and a `hermes cad` CLI (setup/doctor).
Tools shell out to a dedicated CAD venv (~/.venvs/cad) so the heavy CAD stack
never imports into the Hermes venv.
"""
from __future__ import annotations

import json
import logging
from typing import Any

# Directory plugins load as hermes_plugins.<slug> with submodule_search_locations
# set to the plugin dir, so a RELATIVE import is the correct way to reach the
# sibling package (absolute `from hermes_text_to_cad import core` is not on sys.path).
from .hermes_text_to_cad import core

logger = logging.getLogger(__name__)

# Authoritative tool list (mirrors plugin.yaml provides_tools; the contract test
# asserts they match). Keep in sync with the register_tool calls below.
TOOL_NAMES = (
    "cad_spec_from_prompt", "cad_plan", "cad_generate",
    "cad_render", "cad_measure", "cad_compare", "cad_review",
)


# ---- tool handlers --------------------------------------------------------
# Hermes dispatch calls handlers as handler(args: dict, **kwargs) and JSON-encodes
# the returned dict. So each handler unpacks from the args dict.

def _cad_spec_from_prompt(args: dict, **_kw: Any) -> dict[str, Any]:
    return core.spec_from_prompt(prompt=args["prompt"], out_path=args.get("out_path"))


def _cad_plan(args: dict, **_kw: Any) -> dict[str, Any]:
    return core.plan_from_prompt(prompt=args["prompt"], out_path=args.get("out_path"))


def _cad_generate(args: dict, **_kw: Any) -> dict[str, Any]:
    return core.generate(code=args["code"], out_dir=args.get("out_dir"),
                         stem=args.get("stem", "part"), backend=args.get("backend", "cadquery"),
                         emit_glb=args.get("emit_glb", True))


def _cad_render(args: dict, **_kw: Any) -> dict[str, Any]:
    return core.render(stl=args["stl"])


def _cad_measure(args: dict, **_kw: Any) -> dict[str, Any]:
    return core.measure(stl=args["stl"], spec_path=args.get("spec_path"))


def _cad_compare(args: dict, **_kw: Any) -> dict[str, Any]:
    return core.compare(generated=args["generated"], reference=args["reference"],
                        samples=args.get("samples"), seed=args.get("seed"))


def _cad_review(args: dict, **_kw: Any) -> dict[str, Any]:
    return core.review(montage=args["montage"], spec_path=args["spec_path"],
                       models=args.get("models"), mode=args.get("mode", "qa"))


# ---- CLI: `hermes cad <subcommand>` --------------------------------------

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
    sp = parser.add_subparsers(dest="cad_subcommand")
    sp.add_parser("doctor", help="Check CAD venv, render display, vision-gate key")
    sp.add_parser("setup", help="Provision the ~/.venvs/cad venv (uv preferred)")


# ---- register -------------------------------------------------------------

def register(ctx: Any) -> None:
    ctx.register_tool(
        name="cad_spec_from_prompt",
        toolset="cad",
        handler=_cad_spec_from_prompt,
        description=("Derive a machine-checkable geometric spec from a natural-language part "
                     "prompt (the CADTests pattern): bounding box, through-hole count, "
                     "connected-body count, watertight. Deterministic — no model call. Feed "
                     "the resulting spec.json straight to cad_measure --spec so the numeric "
                     "gate asserts INTENT (e.g. 'has a through-hole'), not just bbox. Step 1 "
                     "of the closed CAD loop."),
        emoji="📝",
        schema={
            "name": "cad_spec_from_prompt",
            "description": "NL prompt -> machine-checkable geometric spec (bbox/holes/shells).",
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string", "description": "Natural-language description of the part."},
                    "out_path": {"type": "string", "description": "Optional path to write spec.json (for cad_measure --spec)."},
                },
            },
        },
    )
    ctx.register_tool(
        name="cad_plan",
        toolset="cad",
        handler=_cad_plan,
        description=("Emit a structured modeling PLAN (primitives -> operations -> features -> "
                     "export) from a part prompt before codegen — the CAD-Coder chain-of-thought "
                     "pattern that measurably improves code validity. Deterministic (no model "
                     "call): seeded from the same derived spec cad_measure asserts, so the plan "
                     "and the numeric gate agree. Optional step 1.5 — expand the plan's "
                     "`operations` into CadQuery, then call cad_generate."),
        emoji="🧭",
        schema={
            "name": "cad_plan",
            "description": "NL prompt -> structured modeling plan (primitives/operations/features/export).",
            "parameters": {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string", "description": "Natural-language description of the part."},
                    "out_path": {"type": "string", "description": "Optional path to write plan.json."},
                },
            },
        },
    )
    ctx.register_tool(
        name="cad_generate",
        toolset="cad",
        handler=_cad_generate,
        description=("Execute modeling code to build a parametric 3D model. backend='cadquery' "
                     "(default): CadQuery Python that exports <stem>.stl and <stem>.step into "
                     "the CAD_OUT directory (os.environ['CAD_OUT']) — returns STL (mesh) + STEP "
                     "(B-rep). backend='openscad': SCAD source run via the openscad binary -> "
                     "STL only (mesh, no STEP). On success it ALSO emits <stem>.glb (per-body "
                     "color render input for cad_render's PBR gate) + <stem>.topology.json (a "
                     "machine-readable sidecar: bbox, counts, per-body table, valid) unless "
                     "emit_glb=false. To get per-body COLOR in the render, build a cq.Assembly "
                     "with .color and export it as the GLB. SECURITY: cadquery code is "
                     "model-authored and executed; it runs with a scrubbed secret-free env + an "
                     "AST pre-check, and with HERMES_CAD_SANDBOX=1 inside a no-network read-only "
                     "sandbox. Step 2 of the closed CAD loop."),
        emoji="📐",
        schema={
            "name": "cad_generate",
            "description": "Run CadQuery (->STL+STEP+GLB+topology) or OpenSCAD (->STL+GLB+topology) code, exporting into $CAD_OUT.",
            "parameters": {
                "type": "object",
                "required": ["code"],
                "properties": {
                    "code": {"type": "string", "description": "Modeling source. CadQuery Python (exports STL+STEP into os.environ['CAD_OUT']) or SCAD source when backend='openscad'."},
                    "out_dir": {"type": "string", "description": "Output dir; temp dir if omitted."},
                    "stem": {"type": "string", "description": "Filename stem (default 'part')."},
                    "backend": {"type": "string", "enum": ["cadquery", "openscad"], "description": "Modeling backend (default 'cadquery'; 'openscad' is mesh-only)."},
                    "emit_glb": {"type": "boolean", "description": "Emit <stem>.glb + <stem>.topology.json on success (default true). Set false to skip the GLB/sidecar (e.g. numeric-only checks)."},
                },
            },
        },
    )
    ctx.register_tool(
        name="cad_render",
        toolset="cad",
        handler=_cad_render,
        description=("Render an STL into a 3-view (front/top/iso) montage PNG, headless. Uses "
                     "VTK via a display when available, falls back to matplotlib. Step 3 — "
                     "the input to the vision gate."),
        emoji="🖼️",
        schema={
            "name": "cad_render",
            "description": "STL -> 3-view montage PNG (headless).",
            "parameters": {
                "type": "object",
                "required": ["stl"],
                "properties": {"stl": {"type": "string", "description": "Path to the .stl file."}},
            },
        },
    )
    ctx.register_tool(
        name="cad_measure",
        toolset="cad",
        handler=_cad_measure,
        description=("Numeric gate: measure an STL (bbox, watertight, volume, shells) and assert "
                     "against a spec JSON. gate_pass=True only if all checks pass. Carries the "
                     "PRECISION load of verification — vision cannot judge exact dims. Step 4."),
        emoji="📏",
        schema={
            "name": "cad_measure",
            "description": "Numeric gate: measure + assert STL vs spec.json.",
            "parameters": {
                "type": "object",
                "required": ["stl"],
                "properties": {
                    "stl": {"type": "string", "description": "Path to the .stl file."},
                    "spec_path": {"type": "string", "description": "Path to spec.json (bbox_mm, watertight, max_shells, min/max_volume_mm3)."},
                },
            },
        },
    )
    ctx.register_tool(
        name="cad_compare",
        toolset="cad",
        handler=_cad_compare,
        description=("Similarity gate for 'make it like THIS' tasks: compute the Chamfer "
                     "Distance between a GENERATED mesh and a user-supplied REFERENCE mesh "
                     "(STL/STEP/OBJ/PLY). CD~0 means identical; it grows monotonically with "
                     "deformation (CAD-Coder's geometric reward). Reports raw + bbox-normalized "
                     "scores. Compares meshes in their own frames (no ICP alignment yet) — not "
                     "applicable to pure-text generation with no reference."),
        emoji="📐",
        schema={
            "name": "cad_compare",
            "description": "Chamfer Distance between a generated mesh and a reference mesh.",
            "parameters": {
                "type": "object",
                "required": ["generated", "reference"],
                "properties": {
                    "generated": {"type": "string", "description": "Path to the generated mesh (.stl/.obj/.ply)."},
                    "reference": {"type": "string", "description": "Path to the reference mesh to compare against."},
                    "samples": {"type": "integer", "description": "Surface sample points per mesh (default 4096)."},
                    "seed": {"type": "integer", "description": "RNG seed for reproducible sampling (default 0)."},
                },
            },
        },
    )
    ctx.register_tool(
        name="cad_review",
        toolset="cad",
        handler=_cad_review,
        description=("Qualitative gate: cross-family multi-model VISION review of the render montage "
                     "against the spec. mode='qa' (default, CADCodeVerify) has each model GENERATE "
                     "spec-derived Yes/No questions, ANSWER them against the render with reasoning, "
                     "and turn 'no's into must_fix; mode='free' is legacy free-form critique. "
                     "Cross-family convergence (>=2 models agree) = hard must-fix. Needs "
                     "OPENROUTER_API_KEY. Catches intent errors the numeric gate cannot. Step 5."),
        emoji="👁️",
        requires_env=["OPENROUTER_API_KEY"],
        schema={
            "name": "cad_review",
            "description": "Cross-family vision gate over the render montage vs spec (structured Q&A or free-form).",
            "parameters": {
                "type": "object",
                "required": ["montage", "spec_path"],
                "properties": {
                    "montage": {"type": "string", "description": "Path to the 3-view montage PNG."},
                    "spec_path": {"type": "string", "description": "Path to spec.json."},
                    "models": {"type": "string", "description": "Optional comma-separated model slugs (default: gemini/opus/gpt cross-family)."},
                    "mode": {"type": "string", "enum": ["qa", "free"], "description": "qa = structured Yes/No-Q&A (default); free = legacy free-form critique."},
                },
            },
        },
    )

    ctx.register_cli_command(
        "cad",
        help="Verified text-to-CAD: setup the CAD venv and check readiness",
        setup_fn=_setup_cli,
        handler_fn=_cli_cad,
        description="hermes-text-to-cad plugin CLI",
    )
    logger.info("hermes-text-to-cad: registered %d tools + `cad` CLI", len(TOOL_NAMES))
