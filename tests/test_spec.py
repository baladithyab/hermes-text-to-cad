"""Wave 1.1 — prompt-derived geometric tests (the CADTests pattern).

Two pure, stdlib-only surfaces under test (no CAD venv needed):

  core.derive_spec(prompt) -> dict
      Deterministically turn an NL prompt into a machine-checkable spec
      (bbox, through-holes, watertight, shells). Deterministic on purpose: an
      LLM can emit the same JSON shape, but the *contract* is testable offline.

  measure.gate(meas, spec) -> [checks]
      The gate vocabulary, extended with topological/feature-count assertions
      (through_holes, genus, solidity, exact shells). gate() operates on an
      already-measured dict, so it's testable without trimesh.

The headline (CADTests thesis): a solid box and a box-with-a-through-hole have
identical bounding boxes but differ in genus. The bbox gate passes the wrong
part; the through_holes gate catches it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_text_to_cad.core as core  # noqa: E402


# ============================ core.derive_spec ================================

def test_derive_spec_extracts_bbox_x_notation():
    spec = core.derive_spec("a 40x30x20 mounting block")
    assert spec["bbox_mm"] == [40.0, 30.0, 20.0]


def test_derive_spec_extracts_bbox_with_mm_and_spaces():
    spec = core.derive_spec("bracket 60 mm x 40 mm x 5 mm")
    assert spec["bbox_mm"] == [60.0, 40.0, 5.0]


def test_derive_spec_extracts_bbox_by_notation():
    spec = core.derive_spec("a plate 100 by 50 by 8")
    assert spec["bbox_mm"] == [100.0, 50.0, 8.0]


def test_derive_spec_no_dims_omits_bbox():
    spec = core.derive_spec("a small widget")
    assert "bbox_mm" not in spec


def test_derive_spec_single_through_hole():
    spec = core.derive_spec("a 40x30x20 block with a through hole")
    assert spec["through_holes"] == 1


def test_derive_spec_counts_numeric_holes():
    spec = core.derive_spec("a plate with 4 mounting holes")
    assert spec["through_holes"] == 4


def test_derive_spec_counts_word_holes():
    spec = core.derive_spec("a bar with three through-holes")
    assert spec["through_holes"] == 3


def test_derive_spec_blind_hole_is_not_a_through_hole():
    # A blind hole adds no genus; it must not be counted as a through-hole.
    spec = core.derive_spec("a block with a blind hole")
    assert "through_holes" not in spec


def test_derive_spec_no_hole_omits_through_holes():
    spec = core.derive_spec("a solid 40x30x20 block")
    assert "through_holes" not in spec


def test_derive_spec_hole_free_is_not_a_hole():
    # "hole-free" / "no holes" / "without holes" are NEGATIONS — zero holes.
    for prompt in ("a hole-free plate 50x50x5",
                   "a solid block, no holes",
                   "a plate without holes"):
        spec = core.derive_spec(prompt)
        assert "through_holes" not in spec, f"{prompt!r} should derive no through_holes"


def test_derive_spec_whole_word_not_a_hole():
    # "whole" must not match the hole keyword (substring trap).
    spec = core.derive_spec("a whole bracket 40x30x20")
    assert "through_holes" not in spec


def test_derive_spec_numeric_multi_hole_count():
    spec = core.derive_spec("a 12-hole flange 80x80x10")
    assert spec["through_holes"] == 12


def test_derive_spec_counterbored_hole_counts_as_one():
    spec = core.derive_spec("a plate with a counterbored hole")
    assert spec["through_holes"] == 1


def test_derive_spec_mixed_blind_and_through():
    # A blind hole adds no genus, but it must not suppress a genuine through-hole.
    spec = core.derive_spec("a plate with a blind hole and one through hole")
    assert spec["through_holes"] == 1


def test_derive_spec_one_part_stays_single_shell():
    # "1 part" is explicitly single-body; must not be clamped up to 2.
    assert core.derive_spec("a 1 part design")["max_shells"] == 1
    assert core.derive_spec("a 1-piece bracket")["max_shells"] == 1


def test_derive_spec_two_part_is_multi_shell():
    assert core.derive_spec("a 2-part snap-fit assembly")["max_shells"] == 2
    assert core.derive_spec("a multi-part assembly")["max_shells"] == 2


def test_derive_spec_defaults_watertight_true():
    spec = core.derive_spec("a 10x10x10 cube")
    assert spec["watertight"] is True


def test_derive_spec_defaults_single_shell():
    spec = core.derive_spec("a 10x10x10 cube")
    assert spec["max_shells"] == 1


def test_derive_spec_assembly_allows_multiple_shells():
    spec = core.derive_spec("a two-part snap-fit assembly")
    assert spec["max_shells"] >= 2


def test_derive_spec_has_default_tolerance():
    spec = core.derive_spec("a 40x30x20 block")
    assert spec["tol_mm"] > 0


def test_derive_spec_flags_internal_features():
    # Wave 3.2 / ADR-0009: prompts mentioning internal geometry set the flag so
    # core.render can request a section/cutaway view.
    for prompt in ("a manifold block with an internal cooling channel",
                   "a part with a horizontal bore",
                   "an enclosure with an internal cavity",
                   "a hollow box",
                   "a duct with an internal passage"):
        spec = core.derive_spec(prompt)
        assert spec.get("internal_features") is True, f"{prompt!r} should flag internal_features"


def test_derive_spec_no_internal_features_for_plain_part():
    spec = core.derive_spec("a 40x30x20 mounting plate with 4 holes")
    # plain through-holes are visible in standard views — not 'internal'
    assert "internal_features" not in spec or spec["internal_features"] is False


def test_derive_spec_records_prompt():
    spec = core.derive_spec("a 40x30x20 block with a through hole")
    assert spec["prompt"] == "a 40x30x20 block with a through hole"


def test_derive_spec_is_json_serialisable():
    import json
    spec = core.derive_spec("a 60x40x5 bracket with 4 holes")
    json.loads(json.dumps(spec))  # round-trips with no custom types


def test_spec_from_prompt_wrapper_shape():
    res = core.spec_from_prompt("a 40x30x20 block with a through hole")
    assert res["success"] is True
    assert res["spec"]["bbox_mm"] == [40.0, 30.0, 20.0]
    assert res["spec"]["through_holes"] == 1


# ============================ measure.gate() vocab ============================
# gate() is pure: it reads an already-measured dict. Build fakes, no trimesh.

def _meas(**over):
    base = {
        "bbox_sorted": [20.0, 30.0, 40.0],
        "watertight": True,
        "volume_mm3": 22995.0,
        "n_shells": 1,
        "genus": 1,
        "through_holes": 1,
        "solidity": 0.958,
    }
    base.update(over)
    return base


def _pass(checks, name):
    by = {c["check"]: c for c in checks}
    assert name in by, f"{name} not evaluated; got {list(by)}"
    return by[name]["PASS"]


def test_gate_through_holes_pass(measure_mod):
    checks = measure_mod.gate(_meas(through_holes=1), {"through_holes": 1})
    assert _pass(checks, "through_holes") is True


def test_gate_through_holes_fail(measure_mod):
    # The headline: spec wants 1 through-hole, mesh has 0 (a solid box).
    checks = measure_mod.gate(_meas(through_holes=0, genus=0), {"through_holes": 1})
    assert _pass(checks, "through_holes") is False


def test_gate_genus_check(measure_mod):
    assert _pass(measure_mod.gate(_meas(genus=2), {"genus": 2}), "genus") is True
    assert _pass(measure_mod.gate(_meas(genus=1), {"genus": 2}), "genus") is False


def test_gate_solidity_min(measure_mod):
    assert _pass(measure_mod.gate(_meas(solidity=0.96), {"min_solidity": 0.9}), "min_solidity") is True
    assert _pass(measure_mod.gate(_meas(solidity=0.50), {"min_solidity": 0.9}), "min_solidity") is False


def test_gate_exact_shells(measure_mod):
    assert _pass(measure_mod.gate(_meas(n_shells=2), {"shells": 2}), "shells") is True
    assert _pass(measure_mod.gate(_meas(n_shells=1), {"shells": 2}), "shells") is False


def test_gate_bbox_only_misses_missing_hole(measure_mod):
    # Demonstrate the failure mode the CADTests pattern fixes: identical bbox,
    # different topology. bbox passes for BOTH the right and wrong part...
    spec_bbox_only = {"bbox_mm": [40, 30, 20], "tol_mm": 0.5}
    solid = _meas(bbox_sorted=[20.0, 30.0, 40.0], through_holes=0, genus=0)
    holed = _meas(bbox_sorted=[20.0, 30.0, 40.0], through_holes=1, genus=1)
    assert _pass(measure_mod.gate(solid, spec_bbox_only), "bbox") is True
    assert _pass(measure_mod.gate(holed, spec_bbox_only), "bbox") is True

    # ...but the prompt-derived spec (with through_holes) separates them.
    spec_full = {"bbox_mm": [40, 30, 20], "tol_mm": 0.5, "through_holes": 1}
    solid_checks = measure_mod.gate(solid, spec_full)
    holed_checks = measure_mod.gate(holed, spec_full)
    assert all(c["PASS"] for c in holed_checks)            # right part: all pass
    assert not all(c["PASS"] for c in solid_checks)        # wrong part: caught
    assert _pass(solid_checks, "through_holes") is False


def test_gate_unknown_keys_ignored(measure_mod):
    # A spec with only new keys still evaluates only those (back-compat).
    checks = measure_mod.gate(_meas(), {"through_holes": 1})
    assert [c["check"] for c in checks] == ["through_holes"]


def test_gate_missing_measured_field_skips_gracefully(measure_mod):
    # If a measured value is absent (older measure output), the check is skipped
    # rather than crashing — additive, never breaks the old path.
    meas = _meas()
    del meas["through_holes"]
    checks = measure_mod.gate(meas, {"through_holes": 1})
    names = [c["check"] for c in checks]
    assert "through_holes" not in names
