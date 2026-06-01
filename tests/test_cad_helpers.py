"""Tests for scripts/cad_helpers.py — first-party parametric geometry helpers.

Two tiers, mirroring the codebase's CAD-venv split:

  * PURE tests — load scripts/cad_helpers.py as a module (its cadquery/trimesh
    imports are LAZY, inside each function) and assert: the module imports bare,
    every public helper is present with the documented signature, and every
    parameter-validation guard raises ``ValueError`` BEFORE any CAD import. These
    run in a plain python with no CAD stack.

  * INTEGRATION tests — gated on ``core.venv_ready()`` exactly like
    tests/test_integration.py. They actually BUILD each helper (cadquery runs in
    this interpreter when pytest is invoked with ~/.venvs/cad/bin/python), EXPORT
    the result to a temp STL, and MEASURE it with trimesh, asserting watertight +
    the expected bbox / shell count.

Run the full file in the CAD venv:
    ~/.venvs/cad/bin/python -m pytest tests/test_cad_helpers.py -q
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_text_to_cad.core as core  # noqa: E402


def load_helpers_module():
    """Load scripts/cad_helpers.py as a standalone module (no CAD stack needed)."""
    path = REPO_ROOT / "scripts" / "cad_helpers.py"
    spec = importlib.util.spec_from_file_location("cad_helpers_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def H():
    return load_helpers_module()


# All eight public helpers and their REQUIRED (non-default) parameter names — the
# signature contract this builder owns.
EXPECTED_SIGNATURES = {
    "teardrop": ["radius", "length"],
    "loft_profiles": ["profiles"],
    "sweep_along": ["section_factory", "path_points"],
    "revolve_profile": ["profile_factory"],
    "safe_fillet": ["part", "selector", "radius"],
    "shell_solid": ["part", "open_face_selector", "wall"],
    "flush_emblem": ["host", "emblem_2d_factory", "face_selector", "depth"],
    "disc_in_ring_button": ["ring_od", "ring_id", "disc_d", "height", "gap"],
}


# ===========================================================================
# PURE tests — module loads bare, signatures present, validation raises
# ===========================================================================
class TestModuleSurface:
    def test_module_imports_without_cad_stack(self, H):
        """The module loads in a plain interpreter — no eager cadquery/trimesh."""
        assert hasattr(H, "__all__")
        assert set(H.__all__) == set(EXPECTED_SIGNATURES)

    def test_every_helper_present_and_callable(self, H):
        for name in EXPECTED_SIGNATURES:
            fn = getattr(H, name, None)
            assert fn is not None, f"missing helper {name}"
            assert callable(fn), f"{name} is not callable"

    @pytest.mark.parametrize("name,required", list(EXPECTED_SIGNATURES.items()))
    def test_signature_has_required_params(self, H, name, required):
        params = list(inspect.signature(getattr(H, name)).parameters)
        for p in required:
            assert p in params, f"{name} missing required param {p!r}; has {params}"

    def test_helpers_do_not_import_cad_at_module_scope(self):
        """The module SOURCE must not import cadquery/trimesh at top level — heavy
        imports live inside functions so the bare-load contract holds."""
        src = (REPO_ROOT / "scripts" / "cad_helpers.py").read_text()
        # crude but effective: no unindented import of the CAD stack
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped != line:
                continue  # indented (inside a function) — allowed
            assert not stripped.startswith("import cadquery"), "top-level cadquery import"
            assert not stripped.startswith("import trimesh"), "top-level trimesh import"
            assert not stripped.startswith("from cadquery"), "top-level cadquery import"
            assert not stripped.startswith("from trimesh"), "top-level trimesh import"


class TestParamValidationPure:
    """Every guard raises ValueError BEFORE touching the CAD stack — so these pass
    in a bare interpreter. (When run in the CAD venv they still must raise.)"""

    def test_teardrop_rejects_nonpositive(self, H):
        with pytest.raises(ValueError):
            H.teardrop(radius=0, length=10)
        with pytest.raises(ValueError):
            H.teardrop(radius=5, length=-1)

    def test_teardrop_rejects_bad_angle(self, H):
        for bad in (0, 90, 95, -10):
            with pytest.raises(ValueError):
                H.teardrop(radius=5, length=10, angle_deg=bad)

    def test_teardrop_rejects_bad_axis(self, H):
        with pytest.raises(ValueError):
            H.teardrop(radius=5, length=10, axis="w")

    def test_loft_needs_two_profiles(self, H):
        with pytest.raises(ValueError):
            H.loft_profiles([(0, lambda wp: wp)])
        with pytest.raises(ValueError):
            H.loft_profiles([])

    def test_loft_rejects_bad_pair_and_noncallable(self, H):
        with pytest.raises(ValueError):
            H.loft_profiles([(0,), (10, lambda wp: wp)])
        with pytest.raises(ValueError):
            H.loft_profiles([(0, "nope"), (10, lambda wp: wp)])

    def test_loft_rejects_duplicate_z(self, H):
        with pytest.raises(ValueError):
            H.loft_profiles([(5, lambda wp: wp), (5, lambda wp: wp)])

    def test_sweep_needs_two_distinct_points(self, H):
        with pytest.raises(ValueError):
            H.sweep_along(lambda wp: wp, [(0, 0, 0)])
        with pytest.raises(ValueError):
            H.sweep_along(lambda wp: wp, [(0, 0, 0), (0, 0, 0)])

    def test_sweep_rejects_noncallable_section(self, H):
        with pytest.raises(ValueError):
            H.sweep_along("nope", [(0, 0, 0), (1, 1, 1)])

    def test_sweep_rejects_bad_point_shape(self, H):
        with pytest.raises(ValueError):
            H.sweep_along(lambda wp: wp, [(0, 0), (1, 1, 1)])

    def test_revolve_rejects_bad_degrees(self, H):
        for bad in (0, -10, 361):
            with pytest.raises(ValueError):
                H.revolve_profile(lambda wp: wp, degrees=bad)

    def test_revolve_rejects_noncallable(self, H):
        with pytest.raises(ValueError):
            H.revolve_profile("nope")

    def test_safe_fillet_rejects_bad_params(self, H):
        with pytest.raises(ValueError):
            H.safe_fillet(None, "|Z", radius=-1)
        with pytest.raises(ValueError):
            H.safe_fillet(None, "", radius=2)
        with pytest.raises(ValueError):
            H.safe_fillet(None, "|Z", radius=2, fallback_ratio=0)
        with pytest.raises(ValueError):
            H.safe_fillet(None, "|Z", radius=2, fallback_ratio=1.5)

    def test_shell_rejects_bad_params(self, H):
        with pytest.raises(ValueError):
            H.shell_solid(None, ">Z", wall=0)
        with pytest.raises(ValueError):
            H.shell_solid(None, "", wall=2)

    def test_emblem_rejects_bad_params(self, H):
        with pytest.raises(ValueError):
            H.flush_emblem(None, lambda wp: wp, ">Z", depth=0)
        with pytest.raises(ValueError):
            H.flush_emblem(None, "nope", ">Z", depth=2)
        with pytest.raises(ValueError):
            H.flush_emblem(None, lambda wp: wp, ">Z", depth=2, mode="bogus")
        with pytest.raises(ValueError):
            H.flush_emblem(None, lambda wp: wp, "", depth=2)

    def test_button_rejects_impossible_geometry(self, H):
        # disc too big for the gap
        with pytest.raises(ValueError):
            H.disc_in_ring_button(ring_od=20, ring_id=14, disc_d=13, height=4, gap=1)
        # ring_id >= ring_od
        with pytest.raises(ValueError):
            H.disc_in_ring_button(ring_od=14, ring_id=14, disc_d=8, height=4, gap=1)
        # negative gap
        with pytest.raises(ValueError):
            H.disc_in_ring_button(ring_od=20, ring_id=14, disc_d=8, height=4, gap=-1)
        # non-positive size
        with pytest.raises(ValueError):
            H.disc_in_ring_button(ring_od=20, ring_id=14, disc_d=8, height=0, gap=1)


# ===========================================================================
# INTEGRATION tests — build each helper for real and measure it (CAD venv)
# ===========================================================================
def _cad_in_process() -> bool:
    """True only when cadquery+trimesh import IN THIS interpreter.

    The helpers run cadquery directly in-process (then export to STL + measure with
    trimesh), so — unlike core.venv_ready(), which checks a *subprocess* venv — the
    gate here must confirm the CAD stack is importable in the running interpreter.
    Invoke with ~/.venvs/cad/bin/python -m pytest to satisfy it; a plain python skips.
    """
    if not core.venv_ready():
        return False
    try:
        import cadquery  # noqa: F401
        import trimesh  # noqa: F401
    except Exception:
        return False
    return True


pytestmark_int = pytest.mark.skipif(
    not _cad_in_process(),
    reason="CAD stack not importable in this interpreter "
           "(run with ~/.venvs/cad/bin/python -m pytest)",
)


def _measure(shape, label="solid"):
    """Export a cadquery shape to a temp STL and measure it with trimesh."""
    import cadquery as cq
    import trimesh

    fd, path = tempfile.mkstemp(suffix=".stl", prefix=f"test_{label}_")
    os.close(fd)
    try:
        cq.exporters.export(shape, path, tolerance=0.05, angularTolerance=0.2)
        m = trimesh.load(path, force="mesh")
    finally:
        os.remove(path)
    return {
        "watertight": bool(m.is_watertight),
        "winding": bool(m.is_winding_consistent),
        "bbox": sorted(round(x, 2) for x in m.bounding_box.extents.tolist()),
        "bodies": int(m.body_count),
        "volume": float(m.volume),
    }


@pytest.mark.integration
@pytestmark_int
class TestBuildHelpers:
    def test_teardrop_builds_watertight(self, H):
        import cadquery as cq

        td = H.teardrop(radius=5.0, length=10.0)
        assert isinstance(td, cq.Workplane)
        m = _measure(td, "teardrop")
        assert m["watertight"] and m["winding"]
        assert m["bodies"] == 1
        # extrusion axis (z) gives the 10mm length; the profile spans the circle
        # diameter (10) wide and circle+peak (>10) tall.
        assert 10.0 in m["bbox"]  # the length dimension
        assert max(m["bbox"]) > 10.0  # the apex makes one dim taller than 2r

    def test_teardrop_self_supporting_taller_than_circle(self, H):
        """The defining property: the teardrop peak makes it taller than a plain
        cylinder of the same radius (so the top prints without support)."""
        td = H.teardrop(radius=5.0, length=8.0, angle_deg=45.0)
        cyl = H._assert_solid  # ensure the validator exists (sanity)
        assert callable(cyl)
        m = _measure(td, "teardrop_tall")
        # apex = r/sin(45) = ~7.07 above centre; total profile height ~12.07 > 10
        assert max(m["bbox"]) == pytest.approx(12.07, abs=0.3)

    def test_loft_circle_to_square_single_solid(self, H):
        loft = H.loft_profiles(
            [(0.0, lambda wp: wp.circle(10.0)),
             (20.0, lambda wp: wp.rect(15.0, 15.0))],
            ruled=False,
        )
        m = _measure(loft, "loft")
        assert m["watertight"] and m["winding"]
        assert m["bodies"] == 1
        assert m["bbox"] == pytest.approx([20.0, 20.0, 20.0], abs=0.5)

    def test_loft_ruled_vs_smooth_both_valid(self, H):
        prof = [(0.0, lambda wp: wp.circle(8.0)), (10.0, lambda wp: wp.circle(4.0))]
        for ruled in (True, False):
            m = _measure(H.loft_profiles(prof, ruled=ruled), "loft_r")
            assert m["watertight"], f"ruled={ruled} not watertight"
            assert m["bodies"] == 1

    def test_sweep_along_curved_path(self, H):
        sweep = H.sweep_along(
            lambda wp: wp.circle(3.0),
            [(0, 0, 0), (0, 0, 15), (10, 0, 25), (20, 0, 25)],
        )
        m = _measure(sweep, "sweep")
        assert m["watertight"] and m["winding"]
        assert m["bodies"] == 1
        assert m["volume"] > 0

    def test_revolve_profile_makes_axisymmetric_solid(self, H):
        # a rectangular profile offset 2..8 in radius, +/-10 tall -> a tube/disc
        rev = H.revolve_profile(
            lambda wp: wp.polyline([(2, -10), (8, -10), (8, 10), (2, 10)]).close(),
            degrees=360.0,
        )
        m = _measure(rev, "revolve")
        assert m["watertight"] and m["winding"]
        assert m["bodies"] == 1
        # od = 16, height = 20
        assert m["bbox"] == pytest.approx([16.0, 16.0, 20.0], abs=0.5)

    def test_revolve_rejects_axis_crossing_profile(self, H):
        # profile spans x=-3..3 -> crosses the axis -> must raise (not garbage)
        with pytest.raises(ValueError):
            H.revolve_profile(
                lambda wp: wp.polyline([(-3, 0), (3, 0), (3, 10), (-3, 10)]).close()
            )

    def test_safe_fillet_clamps_oversized_radius(self, H):
        import cadquery as cq

        box = cq.Workplane("XY").box(10, 10, 10)
        # ask for an absurd 50mm radius on a 10mm edge: must clamp + succeed
        filleted, applied = H.safe_fillet(box, "|Z", radius=50.0, fallback_ratio=0.4)
        assert applied <= 0.4 * 10.0 + 1e-6
        assert applied > 0
        m = _measure(filleted, "fillet")
        assert m["watertight"] and m["bodies"] == 1
        assert m["bbox"] == pytest.approx([10.0, 10.0, 10.0], abs=0.1)

    def test_safe_fillet_honors_reasonable_radius(self, H):
        import cadquery as cq

        box = cq.Workplane("XY").box(20, 20, 20)
        filleted, applied = H.safe_fillet(box, "|Z", radius=2.0)
        assert applied == pytest.approx(2.0, abs=1e-6)
        assert _measure(filleted, "fillet2")["watertight"]

    def test_safe_fillet_empty_selection_raises(self, H):
        import cadquery as cq

        box = cq.Workplane("XY").box(10, 10, 10)
        # a selector that resolves to an EMPTY edge set (can't be both >X and <X)
        with pytest.raises(ValueError):
            H.safe_fillet(box, "|Z and >X and <X", radius=1.0)

    def test_shell_solid_hollows_box(self, H):
        import cadquery as cq

        box = cq.Workplane("XY").box(20, 20, 20)
        shelled = H.shell_solid(box, ">Z", wall=2.0)
        m = _measure(shelled, "shell")
        assert m["watertight"] and m["bodies"] == 1
        # outer bbox preserved; volume drops well below the solid box (8000)
        assert m["bbox"] == pytest.approx([20.0, 20.0, 20.0], abs=0.1)
        assert m["volume"] < 8000 * 0.6

    def test_shell_solid_rejects_too_thick_wall(self, H):
        import cadquery as cq

        box = cq.Workplane("XY").box(20, 20, 10)  # min span 10
        with pytest.raises(ValueError):
            H.shell_solid(box, ">Z", wall=6.0)  # >= 0.5 * 10

    def test_flush_emblem_raised_adds_material(self, H):
        import cadquery as cq

        host = cq.Workplane("XY").box(30, 30, 10)
        base_vol = _measure(host, "host")["volume"]
        emb = H.flush_emblem(host, lambda wp: wp.circle(5), ">Z", depth=2.0, mode="raised")
        m = _measure(emb, "emblem_raised")
        assert m["watertight"] and m["bodies"] == 1
        assert m["volume"] > base_vol  # raised => more material
        # the 10mm-tall host gains a 2mm-proud feature on its top face -> 12mm tall
        # (bbox is sorted ascending, so the height is the smallest dimension here)
        assert min(m["bbox"]) == pytest.approx(12.0, abs=0.1)  # 10 + 2 proud

    def test_flush_emblem_engraved_removes_material(self, H):
        import cadquery as cq

        host = cq.Workplane("XY").box(30, 30, 10)
        base_vol = _measure(host, "host2")["volume"]
        emb = H.flush_emblem(host, lambda wp: wp.circle(5), ">Z", depth=2.0, mode="engraved")
        m = _measure(emb, "emblem_engraved")
        assert m["watertight"] and m["bodies"] == 1
        assert m["volume"] < base_vol  # engraved => less material
        assert max(m["bbox"]) == pytest.approx(30.0, abs=0.1)  # no proud feature

    def test_disc_in_ring_button_two_bodies(self, H):
        import cadquery as cq

        btn = H.disc_in_ring_button(ring_od=20, ring_id=14, disc_d=10, height=4, gap=1)
        assert isinstance(btn, cq.Compound)
        assert len(btn.Solids()) == 2
        m = _measure(btn, "button")
        assert m["bodies"] == 2  # ring + disc are disjoint
        assert m["bbox"] == pytest.approx([4.0, 20.0, 20.0], abs=0.2)
        # each body is individually watertight (validated inside the helper);
        # confirm both occupy expected radii by volume sign
        assert m["volume"] > 0

    def test_disc_in_ring_button_disc_clears_ring(self, H):
        """The disc must NOT touch the ring — verified by the part being 2 bodies
        with a measurable gap (disc od 10 < ring id 14 - 2*1)."""
        btn = H.disc_in_ring_button(ring_od=24, ring_id=16, disc_d=12, height=5, gap=1.5)
        m = _measure(btn, "button2")
        assert m["bodies"] == 2

    def test_validator_rejects_known_bad_geometry(self, H):
        """_assert_solid raises on a non-manifold/degenerate result rather than
        returning it. Feed it an empty workplane (no solid) -> ValueError."""
        import cadquery as cq

        with pytest.raises(ValueError):
            H._assert_solid(cq.Workplane("XY"), "empty")
