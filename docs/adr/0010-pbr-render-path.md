# 10. PBR render path: GLTF import + image-based lighting + studio rig + in-pipeline filmic tone mapping

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (reference-uplift Wave 2)

## Context and Problem Statement

The render gate feeds the vision gate. Today `render.py` loads an STL with
`vtkSTLReader` and shades it flat (Gouraud, one auto-created light, a single
hardcoded body color) — so multi-body **colored** assemblies render as one
ambiguous mono-color blob, and even single bodies look flat with hard aliased
edges. The clean-room study of earthtojake/text-to-cad
(`docs/research/reference-steal.md` §A) found their "product-grade" stills come
from **PBR materials + image-based lighting + a studio light rig + filmic tone
mapping**, driven from a **GLB** that carries per-body color — *not* from a
better kernel. The user's explicit goal is "well-rendered … supposedly correct"
models, so render legibility is a first-class correctness lever: the vision gate
can only judge what reads clearly.

Two facts constrain the choice (verified empirically 2026-05-31 in `~/.venvs/cad`):
the stock VTK wheel is 9.3.1 with **every** PBR capability present
(`vtkGLTFImporter`, `SetInterpolationToPBR`, `UseImageBasedLightingOn` /
`SetEnvironmentTexture`, `vtkShadowMapPass`, `vtkToneMappingPass.GenericFilmic`);
and the venv has **no** pyrender / OSMesa / PyOpenGL, so an EGL/offscreen-GL
Python renderer is not available. WSLg `:0` is present, so VTK GLX renders
offscreen in-process.

## Considered Options

1. **Headless Three.js via node + puppeteer** — closest to the reference, but
   pulls a browser + a JS render stack into the loop, far from our Python/VTK
   stack, and adds a heavyweight non-Python dependency surface.
2. **pyrender / trimesh+pyglet with an environment map** — needs pyrender +
   PyOpenGL + an EGL/OSMesa context, none of which are in the venv; would add
   heavy deps and a second GL path to maintain.
3. **Upgrade the existing VTK path to PBR** — `vtkGLTFImporter` (per-body color
   + scene graph in one call) → `SetInterpolationToPBR` per actor → procedural
   equirectangular env texture for image-based lighting → 3-point studio
   `vtkLight` rig → `vtkShadowMapPass` + `vtkToneMappingPass` (GenericFilmic)
   pass pipeline. Zero new deps; reuses the crash-safe backend precedence of
   ADR-0004.
4. **numpy ACES post-pass** (what the research note proposed) — tone-map the
   rendered RGBA buffer in numpy because the studying agent assumed VTK couldn't
   tone-map in-pipeline.

## Decision Outcome

Chosen: **option 3**, with the materials/lighting/tone-mapping pipeline built
entirely in VTK. It is the lowest-dependency path (no new packages in either
venv), reuses ADR-0004's headless precedence + crash-safe GL probe, and a probe
on the real `masterball.glb` fixture proved it preserves all 8 per-body colors
with soft shading, specular highlights, and grounded soft shadows — a visible
order-of-magnitude improvement over the flat STL montage.

The render input becomes **GLB-preferred**: when a `.glb` sibling exists (Wave 4
emits one), `render.py` imports it with `vtkGLTFImporter` so per-body color/
materials come from the asset; otherwise it falls back to the STL path (now also
PBR-shaded, single synthesized material). matplotlib Agg remains the
no-GL last-resort fallback (ADR-0004), upgraded with softer shading where cheap.

**Deviation from the research note (option 4 rejected):** an empirical probe
found `vtkToneMappingPass` supports `GenericFilmic` (an in-GPU filmic curve, ≈
ACES) with `SetExposure` + Uncharted2/default presets. So tone mapping is done
**in-pipeline**, not as a numpy post-pass — one fewer approximation, no CPU
buffer round-trip, and no render-diff threshold retuning. The §A "numpy ACES
post-pass" idea is explicitly **not** adopted.

### Consequences

- **Positive**: multi-body colored GLBs (the masterball) render product-grade —
  per-body color, metallic/roughness, IBL specular, soft contact shadow — so the
  vision gate sees a legible part instead of a flat blob.
- **Positive**: zero new dependencies in either venv; the PBR pipeline is all
  stock VTK 9.3, and the crash-safe GL precedence of ADR-0004 is unchanged.
- **Positive**: in-pipeline `GenericFilmic` tone mapping removes the proposed
  numpy post-pass and its threshold-tuning risk.
- **Negative**: a new failure surface — the pass pipeline (shadow-baker → shadows
  → camera → tone) must be nested in the correct delegate order or shadows
  silently no-op, and `MultiSamples` must be 0 with shadow passes (hardware MSAA
  conflicts with the shadow FBO). Both are encoded + smoke-tested so they don't
  regress.
- **Negative**: image-based lighting uses a *procedural* gradient env texture, not
  a real HDRI, so reflections are plausible-studio, not photoreal. Shipping a
  CC0 HDRI is deferred (it would add a binary asset); the procedural path needs
  no asset and is deterministic.
- **Neutral**: the section/cutaway render (ADR-0009) and the 3-view montage layout
  are preserved; only shading/lighting/material assignment and the GLB-preferred
  reader are added. The seven conventional view-direction vectors and 12%
  auto-fit padding from §A are adopted (conventional framing math, not protected
  expression).
