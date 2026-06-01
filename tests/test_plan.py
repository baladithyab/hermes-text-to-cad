"""Wave 2.3 — cad_plan CoT step (ADR-0007).

core.plan(prompt) emits a deterministic, JSON-serializable modeling plan
(primitives -> operations -> features -> export) seeded from derive_spec. It is
a SCAFFOLD the agent expands, not a finished plan — so it stays LLM-free and
unit-testable offline (like derive_spec / iterate). The headline value is plan↔
gate coherence: the plan carries the same spec the numeric gate will assert.

Pure stdlib — no CAD venv.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_text_to_cad.core as core  # noqa: E402


def test_plan_has_all_sections():
    plan = core.plan("a 40x30x20 block with a through hole")
    for key in ("prompt", "spec", "primitives", "operations", "features", "export", "notes"):
        assert key in plan, f"plan missing {key}"


def test_plan_echoes_prompt_and_spec():
    p = "a 40x30x20 block with a through hole"
    plan = core.plan(p)
    assert plan["prompt"] == p
    # the plan carries the SAME spec the numeric gate will assert (coherence)
    assert plan["spec"] == core.derive_spec(p)


def test_plan_seeds_primitive_from_bbox():
    plan = core.plan("a 40x30x20 block")
    # bbox dims should produce a box primitive mentioning the dims
    joined = " ".join(plan["primitives"]).lower()
    assert "box" in joined
    assert "40" in joined and "30" in joined and "20" in joined


def test_plan_seeds_feature_from_through_holes():
    plan = core.plan("a 40x30x20 plate with 4 mounting holes")
    joined = " ".join(plan["features"]).lower()
    assert "hole" in joined
    assert "4" in joined


def test_plan_no_holes_no_hole_feature():
    plan = core.plan("a solid 40x30x20 block")
    joined = " ".join(plan["features"]).lower()
    assert "hole" not in joined


def test_plan_export_always_stl_and_step():
    plan = core.plan("any part")
    exports = " ".join(plan["export"]).lower()
    assert "stl" in exports
    assert "step" in exports


def test_plan_operations_is_a_list_for_agent_to_fill():
    plan = core.plan("a bracket")
    assert isinstance(plan["operations"], list)


def test_plan_is_json_serialisable():
    plan = core.plan("a 60x40x5 bracket with 4 holes")
    json.loads(json.dumps(plan))   # round-trips, no custom types


def test_plan_notes_guide_the_agent():
    plan = core.plan("a widget")
    assert isinstance(plan["notes"], str) and plan["notes"].strip()


# ---- tool wrapper -------------------------------------------------------------

def test_plan_from_prompt_wrapper_shape():
    res = core.plan_from_prompt("a 40x30x20 block with a through hole")
    assert res["success"] is True
    assert res["plan"]["spec"]["through_holes"] == 1
    assert "export" in res["plan"]


def test_plan_from_prompt_writes_file(tmp_path):
    out = tmp_path / "plan.json"
    res = core.plan_from_prompt("a 40x30x20 block", out_path=str(out))
    assert res["plan_path"] == str(out)
    written = json.loads(out.read_text())
    assert written["spec"]["bbox_mm"] == [40.0, 30.0, 20.0]


# ==================== PLAN v2 — self-check enrichment =========================
# coordinate_frame echo + feature-placement narrative + claims_require +
# repair_classes. The plan now enumerates frame + placement BEFORE codegen
# (prevents "supposedly correct but deformed" output), and binds every claim to
# the tool that legitimises it (never claim done unless the tool ran).

def test_plan_v2_has_new_sections():
    plan = core.plan("a 60x40x5 plate with 4 mounting holes")
    for key in ("coordinate_frame", "claims_require", "repair_classes"):
        assert key in plan, f"plan missing {key}"


def test_plan_echoes_coordinate_frame_from_spec():
    p = "a 60x40x5 mounting plate"
    plan = core.plan(p)
    assert plan["coordinate_frame"] == core.derive_spec(p)["coordinate_frame"]
    assert plan["coordinate_frame"]["origin"] == "footprint_center"


def test_plan_feature_placement_narrative_references_frame():
    # Each feature's narrative must state HOW it is placed relative to the frame.
    plan = core.plan("a 60x40x5 plate with 4 mounting holes")
    joined = " ".join(plan["features"]).lower()
    # the placement story names the frame origin and the count
    assert "footprint_center" in joined or "origin" in joined
    assert "4" in joined


def test_plan_claims_require_maps_claims_to_tools():
    plan = core.plan("a 40x30x20 block with a through hole")
    cr = plan["claims_require"]
    assert isinstance(cr, dict)
    # the contract's named bindings
    assert cr["watertight"] == "cad_measure"
    assert cr["bbox"] == "cad_measure"
    assert cr["through_holes"] == "cad_measure"
    assert cr["render_exists"] == "cad_render"
    assert cr["looks_right"] == "cad_review"


def test_plan_repair_classes_map_failure_to_smallest_fix():
    plan = core.plan("a 40x30x20 block with a through hole")
    rc = plan["repair_classes"]
    # a list of {failure, fix} mappings, deterministic
    assert isinstance(rc, list) and rc
    for entry in rc:
        assert {"failure", "fix"} <= set(entry)
        assert isinstance(entry["failure"], str) and entry["failure"]
        assert isinstance(entry["fix"], str) and entry["fix"]
    blob = json.dumps(rc).lower()
    # the headline repair classes from the contract
    assert "bbox" in blob and "scale" in blob
    assert "through" in blob
    assert "manifold" in blob or "non-manifold" in blob
    assert "fillet" in blob


def test_plan_v2_preserves_existing_sections():
    plan = core.plan("a 40x30x20 block with a through hole")
    for key in ("prompt", "spec", "primitives", "operations", "features",
                "export", "notes"):
        assert key in plan
    # operations stays the agent's to fill
    assert plan["operations"] == []


def test_plan_v2_is_json_serialisable():
    plan = core.plan("a 60x40x5 aluminium bracket with 4 M3 mounting holes")
    json.loads(json.dumps(plan))   # all v2 sections round-trip


def test_plan_v2_is_deterministic():
    p = "a 60x40x5 aluminium bracket with 4 M3 mounting holes"
    assert core.plan(p) == core.plan(p)
