"""Wave uplift-2 — PBR studio render path (ADR-0010).

Two layers, matching the codebase's discipline:
  - PURE unit tests (no vtk): the pbr module's light_rig/gradient_color/TONE_TYPES
    and render.py's resolve_render_source + _frame_camera presence — run anywhere.
  - INTEGRATION (venv-gated): actually render the colored multi-body masterball
    GLB through render.py and assert the montage exists, the backend is the PBR
    path, and per-body COLOR survives (the whole point — a flat STL render can't).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_text_to_cad.core as core  # noqa: E402

MASTERBALL = Path(os.path.expanduser("~/masterball_final/masterball.glb"))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_script(name):
    """Load a scripts/*.py module. pbr.py has no module-top vtk import (all lazy),
    so the pure helpers load + test with no CAD stack present."""
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(f"cad_{name[:-3]}_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pbr():
    return _load_script("pbr.py")


@pytest.fixture
def render():
    return _load_script("render.py")


# ---- pure: pbr.light_rig -----------------------------------------------------

def test_light_rig_returns_three_lights(pbr):
    rig = pbr.light_rig((0, 0, 0), 10.0)
    assert len(rig) == 3
    # exactly one key (the shadow caster)
    assert sum(1 for l in rig if l["key"]) == 1


def test_light_rig_scales_with_radius(pbr):
    """Light standoff tracks model size, so a 5mm and a 200mm part get the same
    relative look (positions are center + dir*radius*k)."""
    near = pbr.light_rig((0, 0, 0), 1.0)
    far = pbr.light_rig((0, 0, 0), 100.0)
    near_d = max(abs(c) for l in near for c in l["position"])
    far_d = max(abs(c) for l in far for c in l["position"])
    assert far_d > near_d * 50  # roughly proportional to the 100x radius


def test_light_rig_centers_on_focal_point(pbr):
    rig = pbr.light_rig((5, -3, 2), 4.0)
    for l in rig:
        assert l["focal_point"] == (5, -3, 2)


# ---- pure: pbr.gradient_color ------------------------------------------------

def test_gradient_color_endpoints(pbr):
    assert pbr.gradient_color(0.0) == pbr.ENV_GROUND   # bottom = ground
    assert pbr.gradient_color(1.0) == pbr.ENV_SKY      # top = sky


def test_gradient_color_clamps(pbr):
    assert pbr.gradient_color(-5) == pbr.ENV_GROUND
    assert pbr.gradient_color(99) == pbr.ENV_SKY


def test_gradient_color_monotonic_midpoint(pbr):
    mid = pbr.gradient_color(0.5)
    # each channel sits between ground and sky
    for i in range(3):
        lo, hi = sorted((pbr.ENV_GROUND[i], pbr.ENV_SKY[i]))
        assert lo <= mid[i] <= hi


# ---- pure: pbr.TONE_TYPES vocabulary ----------------------------------------

def test_tone_types_vocabulary(pbr):
    # filmic (≈ACES) is the chosen default; 'none' disables tone mapping.
    assert pbr.TONE_TYPES["filmic"] == "GenericFilmic"
    assert pbr.TONE_TYPES["none"] is None
    assert set(pbr.TONE_TYPES) >= {"filmic", "clamp", "reinhard", "exponential", "none"}


# ---- pure: render.resolve_render_source (GLB-preferred input) ----------------

def test_resolve_prefers_sibling_glb(render):
    # part.stl with a sibling part.glb -> render the GLB
    assert render.resolve_render_source("/x/part.stl", glb_exists=True) == "/x/part.glb"


def test_resolve_falls_back_to_stl_when_no_glb(render):
    assert render.resolve_render_source("/x/part.stl", glb_exists=False) == "/x/part.stl"


def test_resolve_passes_glb_through(render):
    # a .glb given directly is used as-is regardless of the flag
    assert render.resolve_render_source("/x/m.glb", glb_exists=False) == "/x/m.glb"
    assert render.resolve_render_source("/x/m.glb", glb_exists=True) == "/x/m.glb"


def test_pbr_helpers_present(render, pbr):
    # the studio pipeline entrypoints exist (wired into _render_vtk)
    assert hasattr(render, "_frame_camera")
    assert hasattr(render, "_load_into_renderer")
    for fn in ("apply_studio", "apply_pbr_to_actors", "install_passes",
               "clip_actors", "build_env_texture"):
        assert hasattr(pbr, fn)


# ---- integration: real PBR render of the colored masterball GLB --------------

venv_or_skip = pytest.mark.skipif(not core.venv_ready(),
                                  reason="CAD venv (~/.venvs/cad) not ready")
glb_or_skip = pytest.mark.skipif(not MASTERBALL.exists(),
                                 reason="masterball.glb fixture not present")


@pytest.mark.integration
@venv_or_skip
@glb_or_skip
def test_render_glb_pbr_montage(tmp_path):
    """Render the 8-body colored masterball GLB; the montage must exist and the
    backend must be a PBR path (or matplotlib if no GL — still must produce a
    montage, never crash)."""
    out = tmp_path / "mb_montage.png"
    res = core.render(stl=str(MASTERBALL))
    assert res["success"] is True, res.get("stderr")
    assert res["montage"] and Path(res["montage"]).exists()
    assert Path(res["montage"]).stat().st_size > 0
    assert res["backend"].startswith("vtk") or res["backend"] == "matplotlib"


@pytest.mark.integration
@venv_or_skip
@glb_or_skip
def test_render_glb_preserves_per_body_color(tmp_path):
    """The whole point of GLTF import (ADR-0010): a colored multi-body GLB renders
    with DISTINCT body colors, not one mono-color blob. We render the masterball
    and assert the montage PNG has many distinct colors (a flat single-material
    STL render would have far fewer). Skips if the live path fell back to a
    non-GL backend (matplotlib concatenates the GLB to one mesh)."""
    res = core.render(stl=str(MASTERBALL))
    assert res["success"], res.get("stderr")
    if not res["backend"].startswith("vtk"):
        pytest.skip(f"no GL backend (got {res['backend']}); color test needs VTK")
    from PIL import Image
    img = Image.open(res["montage"]).convert("RGB")
    # downsample the unique-color count; a colored multi-body PBR render has a
    # rich histogram (shells + domes + band + button, each shaded), whereas a
    # mono-material render clusters around one hue + background.
    colors = img.getcolors(maxcolors=1_000_000)
    assert colors is not None
    distinct = len(colors)
    assert distinct > 500, f"only {distinct} distinct colors — color may be lost"


@pytest.mark.integration
@venv_or_skip
def test_render_stl_pbr_still_works(tmp_path):
    """A material-less STL still renders via the PBR path with a synthesized
    studio material (the common case — most parts are STL with no GLB)."""
    box = FIXTURES / "solid_box.stl"
    res = core.render(stl=str(box))
    assert res["success"] is True, res.get("stderr")
    assert Path(res["montage"]).exists()


@pytest.mark.integration
@venv_or_skip
@glb_or_skip
def test_section_render_of_glb_preserves_color(tmp_path):
    """Adversarial-review regression: clip_actors swaps each actor's mapper for a
    fresh one. A fresh vtkPolyDataMapper defaults to map-scalars-through-a-LUT,
    which would run a GLB body's float COLOR_0 RGB scalars through the rainbow
    lookup table and FABRICATE colors. The section of a colored GLB must keep the
    true per-body colors (many distinct hues), not a rainbow-LUT recolor."""
    res = core.render(stl=str(MASTERBALL), section=True)
    assert res["success"], res.get("stderr")
    if not res["backend"].startswith("vtk"):
        pytest.skip(f"no GL backend (got {res['backend']}); needs VTK")
    section_png = Path(res["montage"]).with_name(MASTERBALL.stem + "_section.png")
    assert section_png.exists(), "section panel not written"
    from PIL import Image
    img = Image.open(section_png).convert("RGB")
    colors = img.getcolors(maxcolors=1_000_000)
    assert colors is not None and len(colors) > 300, \
        f"section has only {len(colors) if colors else 0} colors — LUT recolor?"


@pytest.mark.integration
@venv_or_skip
def test_corrupt_glb_falls_back_not_blank(tmp_path):
    """Adversarial-review regression: vtkGLTFImporter does NOT raise on a corrupt
    .glb — it yields zero actors and would render three BLANK panels the vision
    gate trusts. A truncated .glb must degrade (matplotlib fallback or error), not
    silently produce a blank montage."""
    bad = tmp_path / "broken.glb"
    bad.write_bytes(b"glTF\x02\x00\x00\x00" + b"\x00" * 64)  # bogus header/body
    res = core.render(stl=str(bad))
    # Either it fell back to matplotlib (trimesh may also fail -> success False),
    # or it errored — but it must NOT report a successful VTK render of a blank
    # scene. We accept any non-vtk-pbr outcome; the key is no silent blank PBR.
    if res["success"] and res["backend"].startswith("vtk"):
        # if it claims a vtk render, the montage must not be an empty/near-empty
        # frame — assert it has real geometry by checking distinct colors are low
        # (a blank gradient bg) would be the failure. But matplotlib fallback is
        # the expected path, so a vtk success here means the importer found actors
        # (shouldn't for a bogus file). Treat a vtk success as a failure signal.
        pytest.fail(f"corrupt .glb produced a VTK render ({res['backend']}) — "
                    "expected fallback, possible blank scene")


@pytest.mark.integration
@venv_or_skip
def test_corrupt_sibling_glb_degrades_to_stl(tmp_path):
    """The realistic Wave 4 scenario: a valid part.stl with a CORRUPT sibling
    part.glb. render.py prefers the .glb, the VTK import raises, and the fallback
    must retry the ORIGINAL valid STL — not re-feed the bad GLB to matplotlib
    (which also crashes). Result: a montage still gets produced."""
    import shutil
    stl = tmp_path / "part.stl"
    shutil.copy(FIXTURES / "solid_box.stl", stl)
    (tmp_path / "part.glb").write_bytes(b"glTF\x02\x00\x00\x00" + b"\x00" * 64)
    res = core.render(stl=str(stl))
    assert res["success"] is True, f"degrade-to-STL failed: {res.get('stderr')}"
    assert Path(res["montage"]).exists()
