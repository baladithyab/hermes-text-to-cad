"""Wave 3.x — post-generation geometry SANITY beyond the numeric gate.

The hard-won lesson baked in here: a numeric PASS (right bbox, watertight,
right volume) does NOT mean the part is correctly PLACED or even manifold. A
mirrored bracket whose holes drifted off the symmetry plane, a body that landed
at the wrong centroid, or a mesh riddled with zero-area degenerate faces can all
sail through the existing bbox/volume gate. So we add:

  scripts/placement.py — PURE math helpers (no trimesh): centroid_offset,
      symmetry_residual, match_bodies. Unit-tested here with hand-computed
      expected values.

  measure.gate() — new ADDITIVE checks: "manifold", "max_degenerate_faces",
      "symmetry", "expect_centroid", "expect_bodies". Each skips silently when
      the spec key OR the needed measured field is absent, so the existing
      checks (and tests/test_spec.py) are never broken.

gate() is pure — it reads an already-measured dict — so every gate test below
builds a fake measured dict (mirroring tests/test_spec.py's _meas() helper) and
needs no trimesh. The placement helpers are likewise pure stdlib.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_measure_module():
    """Mirror conftest.load_measure_module(): load scripts/measure.py as a module.

    measure.py imports trimesh lazily (inside measure()), so the module — and its
    pure gate() — load without the CAD stack present.
    """
    measure_path = SCRIPTS / "measure.py"
    spec = importlib.util.spec_from_file_location("cad_measure_script_geo", measure_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def measure_mod():
    return _load_measure_module()


import placement  # noqa: E402  (scripts/ on sys.path; pure stdlib)


# ============================ placement.centroid_offset ========================
# centroid_offset = center_mass - bbox-center, per axis.

def test_centroid_offset_zero_when_centered():
    # Box from (0,0,0) to (40,30,20): bbox-center = (20,15,10). A centroid there
    # has zero offset on every axis.
    off = placement.centroid_offset([20.0, 15.0, 10.0], [0.0, 0.0, 0.0], [40.0, 30.0, 20.0])
    assert off == [0.0, 0.0, 0.0]


def test_centroid_offset_signed_per_axis():
    # Centroid drifted +3 in x, -2 in y, +0 in z relative to bbox-center (20,15,10).
    off = placement.centroid_offset([23.0, 13.0, 10.0], [0.0, 0.0, 0.0], [40.0, 30.0, 20.0])
    assert off == pytest.approx([3.0, -2.0, 0.0])


def test_centroid_offset_negative_origin_bbox():
    # bbox spanning (-10,-10,-10)..(10,10,10): center at origin. Centroid at
    # (1, -1, 0.5) => offset is exactly the centroid.
    off = placement.centroid_offset([1.0, -1.0, 0.5], [-10.0, -10.0, -10.0], [10.0, 10.0, 10.0])
    assert off == pytest.approx([1.0, -1.0, 0.5])


# ============================ placement.symmetry_residual ======================
# symmetry_residual[axis] = abs(centroid[axis] - bbox-center[axis]); a part
# symmetric about a plane has its centroid ON that plane => residual ~ 0.

def test_symmetry_residual_on_plane_is_zero():
    # Centroid exactly on the bbox-center in x and y => zero residual both axes.
    res = placement.symmetry_residual(
        [20.0, 15.0, 10.0], [0.0, 0.0, 0.0], [40.0, 30.0, 20.0], ["x", "y"]
    )
    assert res == {"x": 0.0, "y": 0.0}


def test_symmetry_residual_off_plane():
    # Centroid drifted to x=22 (bbox-center x=20) => residual 2.0 in x; y still on.
    res = placement.symmetry_residual(
        [22.0, 15.0, 10.0], [0.0, 0.0, 0.0], [40.0, 30.0, 20.0], ["x", "y"]
    )
    assert res["x"] == pytest.approx(2.0)
    assert res["y"] == pytest.approx(0.0)


def test_symmetry_residual_only_requested_axes():
    res = placement.symmetry_residual(
        [22.0, 99.0, 10.0], [0.0, 0.0, 0.0], [40.0, 30.0, 20.0], ["x"]
    )
    assert list(res.keys()) == ["x"]


# ============================ placement.match_bodies ===========================
# Greedy nearest matching of measured centroids to expected centroids; each
# measured centroid used at most once.

def test_match_bodies_exact():
    measured = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]]
    expected = [[10.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    matches = placement.match_bodies(measured, expected)
    # expected[0]=(10,0,0) matches measured[1]; expected[1]=(0,0,0) matches measured[0]
    by_exp = {m["expected_index"]: m for m in matches}
    assert by_exp[0]["measured_index"] == 1
    assert by_exp[0]["dist"] == pytest.approx(0.0)
    assert by_exp[1]["measured_index"] == 0
    assert by_exp[1]["dist"] == pytest.approx(0.0)


def test_match_bodies_nearest_with_distance():
    measured = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]]
    expected = [[0.5, 0.0, 0.0]]  # nearest to measured[0]
    matches = placement.match_bodies(measured, expected)
    assert len(matches) == 1
    assert matches[0]["expected_index"] == 0
    assert matches[0]["measured_index"] == 0
    assert matches[0]["dist"] == pytest.approx(0.5)


def test_match_bodies_each_measured_used_once():
    # Two expected near the same measured point: the closer one wins it; the
    # other must take a DIFFERENT measured body (greedy, no reuse).
    measured = [[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]
    expected = [[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    matches = placement.match_bodies(measured, expected)
    used = sorted(m["measured_index"] for m in matches)
    assert used == [0, 1], "each measured body used at most once"


def test_match_bodies_more_expected_than_measured_leaves_unmatched():
    measured = [[0.0, 0.0, 0.0]]
    expected = [[0.0, 0.0, 0.0], [50.0, 0.0, 0.0]]
    matches = placement.match_bodies(measured, expected)
    # only one measured body to give out
    matched_exp = {m["expected_index"] for m in matches if m.get("measured_index") is not None}
    assert 0 in matched_exp
    # the second expected is either absent or flagged with no measured_index
    leftover = [m for m in matches if m["expected_index"] == 1]
    if leftover:
        assert leftover[0].get("measured_index") is None


# ============================ measure.gate() — new checks ======================
# gate() is pure. Build fake measured dicts (mirroring test_spec.py::_meas).

def _meas(**over):
    base = {
        "bbox_sorted": [20.0, 30.0, 40.0],
        "bbox_min": [0.0, 0.0, 0.0],
        "bbox_max": [40.0, 30.0, 20.0],
        "watertight": True,
        "volume_mm3": 22995.0,
        "center_mass": [20.0, 15.0, 10.0],
        "n_shells": 1,
        "genus": 1,
        "through_holes": 1,
        "solidity": 0.958,
        "is_volume": True,
        "is_winding_consistent": True,
        "degenerate_faces": 0,
        "body_centroids": [[20.0, 15.0, 10.0]],
    }
    base.update(over)
    return base


def _by(checks):
    return {c["check"]: c for c in checks}


def _pass(checks, name):
    by = _by(checks)
    assert name in by, f"{name} not evaluated; got {list(by)}"
    return by[name]["PASS"]


# ---- manifold ----------------------------------------------------------------

def test_gate_manifold_pass(measure_mod):
    checks = measure_mod.gate(_meas(is_volume=True), {"manifold": True})
    assert _pass(checks, "manifold") is True


def test_gate_manifold_fail(measure_mod):
    checks = measure_mod.gate(_meas(is_volume=False), {"manifold": True})
    assert _pass(checks, "manifold") is False


def test_gate_manifold_skips_when_field_absent(measure_mod):
    meas = _meas()
    del meas["is_volume"]
    checks = measure_mod.gate(meas, {"manifold": True})
    assert "manifold" not in _by(checks)


def test_gate_manifold_skips_when_spec_absent(measure_mod):
    checks = measure_mod.gate(_meas(is_volume=False), {})
    assert "manifold" not in _by(checks)


# ---- max_degenerate_faces ----------------------------------------------------

def test_gate_max_degenerate_faces_pass(measure_mod):
    checks = measure_mod.gate(_meas(degenerate_faces=2), {"max_degenerate_faces": 5})
    assert _pass(checks, "max_degenerate_faces") is True


def test_gate_max_degenerate_faces_fail(measure_mod):
    checks = measure_mod.gate(_meas(degenerate_faces=9), {"max_degenerate_faces": 5})
    assert _pass(checks, "max_degenerate_faces") is False


def test_gate_max_degenerate_faces_skips_when_field_absent(measure_mod):
    meas = _meas()
    del meas["degenerate_faces"]
    checks = measure_mod.gate(meas, {"max_degenerate_faces": 5})
    assert "max_degenerate_faces" not in _by(checks)


# ---- symmetry ----------------------------------------------------------------

def test_gate_symmetry_pass_centroid_on_plane(measure_mod):
    # bbox 0..40 / 0..30 / 0..20 => center (20,15,10). Centroid sits exactly on
    # the x and y symmetry planes => residual 0 <= tol => PASS.
    meas = _meas(center_mass=[20.0, 15.0, 10.0],
                 bbox_min=[0.0, 0.0, 0.0], bbox_max=[40.0, 30.0, 20.0])
    checks = measure_mod.gate(meas, {"symmetry": ["x", "y"], "placement_tol_mm": 0.5})
    assert _pass(checks, "symmetry") is True


def test_gate_symmetry_fail_centroid_drifted_off_plane(measure_mod):
    # Same bbox, but the centroid drifted to x=23 (plane is x=20) => residual 3.0
    # > tol 0.5 => the mirror is broken => FAIL. This is the "numeric-PASS but
    # mis-placed" mode the whole module exists to catch.
    meas = _meas(center_mass=[23.0, 15.0, 10.0],
                 bbox_min=[0.0, 0.0, 0.0], bbox_max=[40.0, 30.0, 20.0])
    checks = measure_mod.gate(meas, {"symmetry": ["x", "y"], "placement_tol_mm": 0.5})
    assert _pass(checks, "symmetry") is False


def test_gate_symmetry_uses_tol_mm_fallback(measure_mod):
    # No placement_tol_mm => falls back to tol_mm. residual 0.3 < tol_mm 0.5 PASS.
    meas = _meas(center_mass=[20.3, 15.0, 10.0],
                 bbox_min=[0.0, 0.0, 0.0], bbox_max=[40.0, 30.0, 20.0])
    checks = measure_mod.gate(meas, {"symmetry": ["x"], "tol_mm": 0.5})
    assert _pass(checks, "symmetry") is True


def test_gate_symmetry_skips_when_centroid_absent(measure_mod):
    meas = _meas()
    del meas["center_mass"]
    checks = measure_mod.gate(meas, {"symmetry": ["x"]})
    assert "symmetry" not in _by(checks)


# ---- symmetry: DICT spec shape (the form derive_spec actually emits) ----------
# Adversarial-review regression: derive_spec emits symmetry as a DICT
# {"mirror": [...], "count": N}, but the gate originally passed it straight to
# symmetry_residual, which iterated the dict KEYS ('mirror','count') — neither a
# valid axis — so residual was {} and the check ALWAYS PASSED. Every drifted part
# sailed through in production; the suite was green only because these tests used
# the bare-list form. These exercise the real seam.

def test_gate_symmetry_dict_form_fails_on_drift(measure_mod):
    meas = _meas(center_mass=[23.0, 15.0, 10.0],
                 bbox_min=[0.0, 0.0, 0.0], bbox_max=[40.0, 30.0, 20.0])
    checks = measure_mod.gate(
        meas, {"symmetry": {"mirror": ["x", "y"], "count": 4}, "placement_tol_mm": 0.5})
    assert _pass(checks, "symmetry") is False  # 3mm drift off x-plane must FAIL


def test_gate_symmetry_dict_form_passes_on_plane(measure_mod):
    meas = _meas(center_mass=[20.0, 15.0, 10.0],
                 bbox_min=[0.0, 0.0, 0.0], bbox_max=[40.0, 30.0, 20.0])
    checks = measure_mod.gate(
        meas, {"symmetry": {"mirror": ["x", "y"], "count": 4}, "placement_tol_mm": 0.5})
    assert _pass(checks, "symmetry") is True


def test_gate_symmetry_empty_mirror_skips(measure_mod):
    # {"mirror": []} asserts no symmetry — skip, don't vacuously pass.
    meas = _meas(center_mass=[23.0, 15.0, 10.0],
                 bbox_min=[0.0, 0.0, 0.0], bbox_max=[40.0, 30.0, 20.0])
    checks = measure_mod.gate(meas, {"symmetry": {"mirror": [], "count": 1}})
    assert "symmetry" not in _by(checks)


def test_gate_symmetry_seam_derive_spec_to_gate(measure_mod):
    """End-to-end seam: the EXACT dict core.derive_spec produces, fed to gate().
    This is the cross-component path the parallel builders each half-tested but
    never together — the bug lived precisely in that gap."""
    import hermes_text_to_cad.core as core
    spec = core.derive_spec("a 60x40x5 symmetric plate with 4 mounting holes")
    assert isinstance(spec.get("symmetry"), dict)  # derive_spec emits the dict form
    # a plate whose centroid drifted off the x symmetry plane must FAIL
    drifted = _meas(center_mass=[25.0, 20.0, 2.5],
                    bbox_min=[0.0, 0.0, 0.0], bbox_max=[40.0, 40.0, 5.0])
    merged = {"symmetry": spec["symmetry"], "placement_tol_mm": 0.5}
    checks = measure_mod.gate(drifted, merged)
    assert _pass(checks, "symmetry") is False


def test_gate_symmetry_skips_when_bbox_bounds_absent(measure_mod):
    meas = _meas()
    del meas["bbox_min"]
    del meas["bbox_max"]
    checks = measure_mod.gate(meas, {"symmetry": ["x"]})
    assert "symmetry" not in _by(checks)


# ---- expect_centroid ---------------------------------------------------------

def test_gate_expect_centroid_pass(measure_mod):
    meas = _meas(center_mass=[20.0, 15.0, 10.0])
    checks = measure_mod.gate(meas, {"expect_centroid": [20.0, 15.0, 10.0], "placement_tol_mm": 0.5})
    assert _pass(checks, "expect_centroid") is True


def test_gate_expect_centroid_within_tol_pass(measure_mod):
    # Euclidean distance sqrt(0.3^2+0.0+0.0)=0.3 < 0.5 => PASS.
    meas = _meas(center_mass=[20.3, 15.0, 10.0])
    checks = measure_mod.gate(meas, {"expect_centroid": [20.0, 15.0, 10.0], "placement_tol_mm": 0.5})
    assert _pass(checks, "expect_centroid") is True


def test_gate_expect_centroid_fail(measure_mod):
    # distance from (25,15,10) to (20,15,10) = 5.0 > 0.5 => FAIL.
    meas = _meas(center_mass=[25.0, 15.0, 10.0])
    checks = measure_mod.gate(meas, {"expect_centroid": [20.0, 15.0, 10.0], "placement_tol_mm": 0.5})
    assert _pass(checks, "expect_centroid") is False


def test_gate_expect_centroid_skips_when_field_absent(measure_mod):
    meas = _meas()
    del meas["center_mass"]
    checks = measure_mod.gate(meas, {"expect_centroid": [0.0, 0.0, 0.0]})
    assert "expect_centroid" not in _by(checks)


# ---- expect_bodies -----------------------------------------------------------

def test_gate_expect_bodies_all_matched(measure_mod):
    meas = _meas(body_centroids=[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    spec = {"expect_bodies": [
        {"centroid": [0.0, 0.0, 0.0], "tol": 0.5},
        {"centroid": [10.0, 0.0, 0.0], "tol": 0.5},
    ]}
    checks = measure_mod.gate(meas, spec)
    assert _pass(checks, "expect_bodies") is True


def test_gate_expect_bodies_unmatched_fails(measure_mod):
    # Expected body at (50,0,0) has no measured centroid within tol => FAIL.
    meas = _meas(body_centroids=[[0.0, 0.0, 0.0]])
    spec = {"expect_bodies": [
        {"centroid": [0.0, 0.0, 0.0], "tol": 0.5},
        {"centroid": [50.0, 0.0, 0.0], "tol": 0.5},
    ]}
    checks = measure_mod.gate(meas, spec)
    assert _pass(checks, "expect_bodies") is False


def test_gate_expect_bodies_out_of_tol_fails(measure_mod):
    # One body present but matched distance (2.0) exceeds its tol (0.5) => FAIL.
    meas = _meas(body_centroids=[[2.0, 0.0, 0.0]])
    spec = {"expect_bodies": [{"centroid": [0.0, 0.0, 0.0], "tol": 0.5}]}
    checks = measure_mod.gate(meas, spec)
    assert _pass(checks, "expect_bodies") is False


def test_gate_expect_bodies_skips_when_field_absent(measure_mod):
    meas = _meas()
    del meas["body_centroids"]
    checks = measure_mod.gate(meas, {"expect_bodies": [{"centroid": [0.0, 0.0, 0.0], "tol": 0.5}]})
    assert "expect_bodies" not in _by(checks)


# ---- back-compat: new checks never disturb the existing ones -----------------

def test_gate_new_checks_do_not_break_existing(measure_mod):
    # A spec mixing old + new keys evaluates BOTH; old checks still behave.
    spec = {
        "bbox_mm": [40, 30, 20], "tol_mm": 0.5, "through_holes": 1,
        "manifold": True, "symmetry": ["x", "y"], "max_degenerate_faces": 0,
    }
    checks = measure_mod.gate(_meas(), spec)
    names = set(c["check"] for c in checks)
    assert {"bbox", "through_holes", "manifold", "symmetry", "max_degenerate_faces"} <= names
    assert all(c["PASS"] for c in checks)


def test_gate_empty_spec_emits_no_new_checks(measure_mod):
    # Sanity: a fully-populated measured dict + empty spec => zero checks.
    assert measure_mod.gate(_meas(), {}) == []


# ============================ venv-gated integration ==========================
# Optional: only runs under the CAD venv (trimesh present). Asserts the new
# measured fields are present + sane on a real solid box fixture.

def _has_trimesh():
    return importlib.util.find_spec("trimesh") is not None


@pytest.mark.skipif(not _has_trimesh(), reason="trimesh (CAD venv) not present")
def test_measure_emits_new_fields_on_solid_box(measure_mod):
    fixture = FIXTURES / "solid_box.stl"
    if not fixture.exists():
        pytest.skip("solid_box.stl fixture missing")
    meas = measure_mod.measure(str(fixture))
    # new fields present
    for key in ("body_centroids", "is_winding_consistent", "degenerate_faces",
                "is_volume", "bbox_min", "bbox_max"):
        assert key in meas, f"measure() missing new field {key!r}"
    # a clean printed box is a manifold volume with no degenerate faces
    assert meas["is_volume"] is True
    assert meas["degenerate_faces"] == 0
    assert isinstance(meas["body_centroids"], list) and len(meas["body_centroids"]) == 1
    # unsorted bbox bounds bracket the sorted extents
    assert len(meas["bbox_min"]) == 3 and len(meas["bbox_max"]) == 3
    for lo, hi in zip(meas["bbox_min"], meas["bbox_max"]):
        assert hi >= lo
