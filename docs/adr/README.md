# Architecture Decision Records

Index of ADRs for hermes-text-to-cad. MADR 3.0 format. Decisions are immutable
once accepted; a reversal is a new ADR that supersedes the old one.

| ADR | Title | Status | Wave |
|-----|-------|--------|------|
| [0001](0001-scrubbed-subprocess-env.md) | Scrub the subprocess environment with an allowlist | accepted | SECURITY |
| [0002](0002-opt-in-bubblewrap-sandbox.md) | Opt-in bubblewrap sandbox for generated code | accepted | SECURITY |
| [0003](0003-ast-denylist-defense-in-depth.md) | AST denylist as a cheap first line of defense | accepted | SECURITY |
| [0004](0004-headless-render-precedence.md) | Headless render precedence (display→OSMesa/EGL→Xvfb→matplotlib) | accepted | 3.1 |
| [0005](0005-structured-qa-vision-gate.md) | Structured Yes/No-Q&A vision gate (CADCodeVerify) | accepted | 2.1 |
| [0006](0006-chamfer-distance-compare.md) | Chamfer-Distance similarity via trimesh sampling | accepted | 2.2 |
| [0007](0007-cad-plan-cot-step.md) | Optional CoT plan step (cad_plan), deterministic | accepted | 2.3 |
| [0008](0008-openscad-backend.md) | Optional OpenSCAD backend, mesh-only | accepted | 3.3 |
| [0009](0009-section-cutaway-render.md) | Section/cutaway render for internal features | accepted | 3.2 |
| [0010](0010-pbr-render-path.md) | PBR render path (GLTF import + IBL + studio rig + filmic tone mapping) | accepted | uplift-2 |
| [0011](0011-spec-contract-v2-and-placement-gate.md) | Spec contract v2 (intent/frame/features/symmetry) + placement/manifold gate | accepted | uplift-3 |
| [0012](0012-first-party-geometry-helper-library.md) | First-party parametric geometry-helper library | accepted | uplift-3 |
| [0013](0013-generator-artifacts-glb-topology-sidecar.md) | Generator emits GLB + topology sidecar alongside STL/STEP | accepted | uplift-4 |

## Cross-cutting invariants (apply to every ADR)

- The CAD stack runs ONLY in `~/.venvs/cad` via subprocess; Hermes-venv plugin
  code imports only stdlib.
- `core` is LLM-free and unit-testable offline; the agent is the caller.
- `register_tool(name, toolset, schema, handler, check_fn=None, requires_env=None,
  is_async=False, description="", emoji="", override=False)`; handlers take
  `(args: dict, **kwargs) -> dict`.
- Manifest `requires_env: []` stays empty; optional creds surface at doctor time.
- Lazy log formatting; broad excepts log at WARNING with context.
