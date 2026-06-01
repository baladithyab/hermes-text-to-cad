#!/usr/bin/env python3
"""pbr.py — physically-based studio rendering helpers for the render gate (ADR-0010).

The render gate feeds the vision gate, and the vision gate can only judge what
reads clearly. This module turns VTK's flat default shading into a product-grade
studio look — the #1 visible quality lever found in the clean-room reference
study (docs/research/reference-steal.md §A): PBR materials + image-based lighting
+ a 3-point studio light rig + filmic tone mapping, all in stock VTK 9.3 (no new
deps). It is loaded by render.py.

Split by testability:
  - PURE helpers (no vtk): light_rig(), gradient_color(), TONE_TYPES — unit-tested
    offline like the rest of the codebase.
  - vtk helpers (lazy `import vtk` inside each fn): build_env_texture(),
    apply_pbr_to_actors(), apply_studio(), clip_actors(), install_passes() —
    exercised in the venv-gated integration test.

Clean-room: technique adapted from earthtojake/text-to-cad's Three.js snapshot
renderer (all-rights-reserved — ideas only). Every VTK call here is our own.
"""
from __future__ import annotations

import math

# ---- tunables (studio look) -------------------------------------------------
# Moderate metallic + mid roughness reads as molded plastic/painted metal — the
# common CAD-part finish. GLB-imported bodies keep their own material; these are
# the synthesized values for STL (no material) and the floor for under-specified
# GLTF materials.
SYNTH_METALLIC = 0.20
SYNTH_ROUGHNESS = 0.50
SYNTH_COLOR = (0.55, 0.57, 0.62)        # neutral cool grey for material-less STL
SECTION_COLOR = (0.84, 0.55, 0.48)      # warm tint to distinguish a cut panel

# Background vertical gradient (cool studio sweep). VTK gradient goes bottom→top
# when GradientBackgroundOn: Background = bottom, Background2 = top.
BG_BOTTOM = (0.10, 0.11, 0.13)
BG_TOP = (0.17, 0.18, 0.21)

# Procedural equirectangular environment (sky→ground) for image-based lighting,
# so we get believable specular without shipping an HDRI binary.
ENV_SKY = (205, 210, 224)
ENV_GROUND = (96, 86, 78)

TONE_EXPOSURE = 1.25

# Map a friendly tone-curve name → the vtkToneMappingPass enum attribute name.
# Pure data so callers/tests can validate the vocabulary without vtk. GenericFilmic
# is VTK's in-GPU filmic curve (≈ ACES) — chosen over a numpy post-pass (ADR-0010).
TONE_TYPES = {
    "filmic": "GenericFilmic",
    "clamp": "Clamp",
    "reinhard": "Reinhard",
    "exponential": "Exponential",
    "none": None,
}


# ---- pure helpers (no vtk) --------------------------------------------------

def light_rig(center, radius):
    """3-point studio rig as world-space lights, scaled/placed around the model.

    Returns a list of dicts {position, focal_point, intensity, key} — a key light
    high to the front-right (the shadow caster), a softer fill to the front-left,
    and a rim/back light. Positions are center + direction*radius*k so the rig
    tracks model size (a 5 mm part and a 200 mm part get the same relative look).
    Pure: no vtk, fully unit-testable.
    """
    cx, cy, cz = center
    r = max(float(radius), 1e-6)
    # (unit-ish direction, intensity, is_key) — directions mirror a conventional
    # key/fill/rim placement (front-right-high / front-left-low / back-high).
    rig = [
        ((0.62, -0.54, 0.92), 1.15, True),    # key
        ((-0.80, -0.30, 0.45), 0.55, False),  # fill
        ((0.05, 0.85, 0.70), 0.65, False),    # rim / back
    ]
    out = []
    for (dx, dy, dz), inten, key in rig:
        out.append({
            "position": (cx + dx * r * 3.0, cy + dy * r * 3.0, cz + dz * r * 3.0),
            "focal_point": (cx, cy, cz),
            "intensity": inten,
            "key": key,
        })
    return out


def gradient_color(t, sky=ENV_SKY, ground=ENV_GROUND):
    """Linear sky→ground RGB (0..255 ints) for the equirectangular env map row.

    t in [0,1], t=0 = ground (bottom), t=1 = sky (top). Pure; used to fill the
    procedural environment texture and unit-testable on its own.
    """
    t = 0.0 if t < 0 else 1.0 if t > 1 else float(t)
    return tuple(int(round(ground[i] * (1 - t) + sky[i] * t)) for i in range(3))


# ---- vtk helpers (lazy import) ----------------------------------------------

def build_env_texture(width=128, height=64):
    """A procedural equirectangular sky→ground gradient as a vtkTexture, for
    image-based lighting (renderer.SetEnvironmentTexture). No HDRI asset needed."""
    import vtk
    img = vtk.vtkImageData()
    img.SetDimensions(width, height, 1)
    img.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 3)
    for y in range(height):
        r, g, b = gradient_color(y / (height - 1) if height > 1 else 0.5)
        for x in range(width):
            img.SetScalarComponentFromDouble(x, y, 0, 0, r)
            img.SetScalarComponentFromDouble(x, y, 0, 1, g)
            img.SetScalarComponentFromDouble(x, y, 0, 2, b)
    tex = vtk.vtkTexture()
    tex.SetInputData(img)
    tex.SetColorModeToDirectScalars()
    tex.MipmapOn()
    tex.InterpolateOn()
    return tex


def apply_pbr_to_actors(renderer, synth=False, color=None):
    """Switch every actor in the renderer to PBR interpolation.

    GLB-imported actors keep their glTF metallic/roughness/base-color (synth=False)
    — we only ensure PBR shading. For a material-less STL actor (synth=True) we set
    a neutral studio material so it isn't pure white. ``color`` overrides the synth
    base color (e.g. a warm tint for a section panel).
    """
    actors = renderer.GetActors()
    actors.InitTraversal()
    n = actors.GetNumberOfItems()
    base = color if color is not None else SYNTH_COLOR
    for _ in range(n):
        a = actors.GetNextActor()
        p = a.GetProperty()
        p.SetInterpolationToPBR()
        if synth:
            p.SetColor(*base)
            p.SetMetallic(SYNTH_METALLIC)
            p.SetRoughness(SYNTH_ROUGHNESS)
        elif color is not None:
            # section panel of a colored GLB: keep PBR but tint to mark the cut
            p.SetColor(*color)
    return n


def apply_studio(renderer, center, bounds):
    """Image-based lighting + 3-point studio rig + gradient backdrop + render
    passes (soft shadows → filmic tone mapping). Replaces any lights the GLTF
    importer added so the look is deterministic across parts."""
    import vtk
    diag = math.sqrt(
        (bounds[1] - bounds[0]) ** 2
        + (bounds[3] - bounds[2]) ** 2
        + (bounds[5] - bounds[4]) ** 2
    ) or 1.0
    radius = diag / 2.0

    renderer.UseImageBasedLightingOn()
    renderer.SetEnvironmentTexture(build_env_texture())

    renderer.RemoveAllLights()
    for spec in light_rig(center, radius):
        L = vtk.vtkLight()
        L.SetLightTypeToSceneLight()
        L.SetPositional(False)
        L.SetPosition(*spec["position"])
        L.SetFocalPoint(*spec["focal_point"])
        L.SetIntensity(spec["intensity"])
        renderer.AddLight(L)

    renderer.GradientBackgroundOn()
    renderer.SetBackground(*BG_BOTTOM)
    renderer.SetBackground2(*BG_TOP)

    install_passes(renderer, shadows=True, tone="filmic", exposure=TONE_EXPOSURE)


def install_passes(renderer, shadows=True, tone="filmic", exposure=TONE_EXPOSURE):
    """Build the render-pass delegation chain (ADR-0010).

    Nesting order is load-bearing: tone → camera → sequence[shadow-baker, shadow].
    The outermost pass is assigned via renderer.SetPass. With shadows on, the
    caller must set renderWindow.SetMultiSamples(0) (hardware MSAA conflicts with
    the shadow FBO). Returns the outermost pass (or None if nothing to install).
    """
    import vtk
    delegate = None

    if shadows:
        seq = vtk.vtkSequencePass()
        passes = vtk.vtkRenderPassCollection()
        shadow = vtk.vtkShadowMapPass()
        passes.AddItem(shadow.GetShadowMapBakerPass())
        passes.AddItem(shadow)
        seq.SetPasses(passes)
        cam = vtk.vtkCameraPass()
        cam.SetDelegatePass(seq)
        delegate = cam

    tone_attr = TONE_TYPES.get(tone)
    if tone_attr is not None:
        tonepass = vtk.vtkToneMappingPass()
        tonepass.SetToneMappingType(getattr(vtk.vtkToneMappingPass, tone_attr))
        tonepass.SetExposure(exposure)
        if tone_attr == "GenericFilmic":
            tonepass.SetGenericFilmicDefaultPresets()
        if delegate is not None:
            tonepass.SetDelegatePass(delegate)
        delegate = tonepass

    if delegate is not None:
        renderer.SetPass(delegate)
    return delegate


def clip_actors(renderer, center, axis):
    """Clip every actor's polydata through ``center`` along ``axis`` (x|y|z), in
    place, for a section/cutaway view that reveals internal features (ADR-0009).
    Preserves each actor's material/color (vs the old single-color section)."""
    import vtk
    normals = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}
    n = normals.get(axis, (1, 0, 0))
    plane = vtk.vtkPlane()
    plane.SetOrigin(*center)
    plane.SetNormal(*n)

    actors = renderer.GetActors()
    actors.InitTraversal()
    count = actors.GetNumberOfItems()
    for _ in range(count):
        a = actors.GetNextActor()
        mapper = a.GetMapper()
        if mapper is None:
            continue
        mapper.Update()
        src = mapper.GetInput()
        if src is None:
            continue
        clipper = vtk.vtkClipPolyData()
        clipper.SetInputData(src)
        clipper.SetClipFunction(plane)
        clipper.SetValue(0)
        clipper.Update()
        out = vtk.vtkPolyDataMapper()
        out.SetInputConnection(clipper.GetOutputPort())
        # Carry the source mapper's scalar-coloring config across the swap. A GLB
        # body carries its color as a float COLOR_0 point-scalar array; the glTF
        # importer's mapper renders it with DirectScalars. A fresh mapper defaults
        # to map-through-LUT (ColorMode=0), which would run those RGB scalars
        # through the rainbow lookup table and FABRICATE colors. Mirror the source
        # mapper's color/scalar mode so the cut keeps true per-body color.
        out.SetColorMode(mapper.GetColorMode())
        out.SetScalarMode(mapper.GetScalarMode())
        out.SetScalarVisibility(mapper.GetScalarVisibility())
        a.SetMapper(out)
    return count
