"""Wave 0.3 — integration test (gated on the real CAD venv).

Exercises the full numeric half of the loop — generate → render → measure —
against the actual ~/.venvs/cad interpreter. Skipped cleanly when that venv
isn't present, so CI without a CAD venv stays green.

The fixture is an L-bracket emitted by CadQuery: base plate 60x40x5 with four
6mm mounting holes + a 30mm-tall upright leg. Ground truth (verified
2026-05-31 in cadquery 2.7 / trimesh 4.12): bbox 60x40x30, watertight, single
shell, genus 4 (four through-holes).
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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not core.venv_ready(), reason="CAD venv (~/.venvs/cad) not ready"),
]

BRACKET_CODE = '''
import cadquery as cq
import os

OUT = os.environ["CAD_OUT"]
L, W, T, H, hole_d = 60.0, 40.0, 5.0, 30.0, 6.0

part = (
    cq.Workplane("XY")
    .box(L, W, T, centered=(False, True, False))
    .faces(">Z").workplane()
    .pushPoints([(12, 12), (12, -12), (48, 12), (48, -12)])
    .hole(hole_d)
)
upright = cq.Workplane("XY").box(T, W, H, centered=(False, True, False))
part = part.union(upright)

cq.exporters.export(part, os.path.join(OUT, "part.stl"), tolerance=0.01, angularTolerance=0.1)
cq.exporters.export(part, os.path.join(OUT, "part.step"))
'''


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("cad_int")
    res = core.generate(code=BRACKET_CODE, out_dir=str(out), stem="part")
    assert res["success"], f"generate failed: {res.get('stderr')}"
    return res


def test_generate_produces_stl_and_step(generated):
    assert generated["stl"] and Path(generated["stl"]).exists()
    assert generated["step"] and Path(generated["step"]).exists()
    assert Path(generated["stl"]).stat().st_size > 0


def test_measure_passes_bbox_gate(generated, tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text('{"bbox_mm": [60, 40, 30], "tol_mm": 0.5, "watertight": true, "max_shells": 1}')
    res = core.measure(stl=generated["stl"], spec_path=str(spec))
    assert res["success"] is True
    assert res["gate_pass"] is True, res["report"]
    meas = res["report"]["measured"]
    assert sorted(meas["bbox_mm"]) == pytest.approx(sorted([60, 40, 30]), abs=0.5)
    assert meas["watertight"] is True
    assert meas["n_shells"] == 1


def test_measure_fails_wrong_bbox(generated, tmp_path):
    # A deliberately-wrong spec must fail the gate (exit 1) but still "run".
    spec = tmp_path / "bad.json"
    spec.write_text('{"bbox_mm": [10, 10, 10], "tol_mm": 0.1}')
    res = core.measure(stl=generated["stl"], spec_path=str(spec))
    assert res["success"] is True       # measurement ran
    assert res["gate_pass"] is False    # but the gate failed


def test_render_produces_montage(generated):
    res = core.render(stl=generated["stl"])
    assert res["success"] is True, res.get("stderr")
    assert res["montage"] and Path(res["montage"]).exists()
    assert res["backend"] in ("vtk", "matplotlib")


# ---- Wave 1.1: prompt-derived geometric gate, end-to-end (real geometry) -----

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_topological_gate_catches_missing_through_hole(tmp_path):
    """The CADTests headline, end-to-end against committed fixtures.

    holed_box and solid_box have IDENTICAL bounding boxes. A prompt asking for a
    through-hole derives through_holes==1; the gate must PASS the holed box and
    FAIL the solid box — something a bbox-only gate cannot do.
    """
    spec = core.spec_from_prompt(
        "a 40x30x20 block with a through hole",
        out_path=str(tmp_path / "spec.json"),
    )
    assert spec["spec"]["through_holes"] == 1
    spec_path = spec["spec_path"]

    holed = core.measure(stl=str(FIXTURES / "holed_box.stl"), spec_path=spec_path)
    solid = core.measure(stl=str(FIXTURES / "solid_box.stl"), spec_path=spec_path)

    # bbox passes for BOTH (identical extents) ...
    holed_checks = {c["check"]: c["PASS"] for c in holed["report"]["gate"]["checks"]}
    solid_checks = {c["check"]: c["PASS"] for c in solid["report"]["gate"]["checks"]}
    assert holed_checks["bbox"] is True
    assert solid_checks["bbox"] is True

    # ... but the topological check separates them.
    assert holed["gate_pass"] is True
    assert solid["gate_pass"] is False
    assert solid_checks["through_holes"] is False


def test_measure_reports_topology_fields():
    res = core.measure(stl=str(FIXTURES / "holed_box.stl"))
    meas = res["report"]["measured"]
    assert meas["genus"] == 1
    assert meas["through_holes"] == 1
    assert 0.0 < meas["solidity"] <= 1.0


def test_solid_box_genus_zero():
    res = core.measure(stl=str(FIXTURES / "solid_box.stl"))
    meas = res["report"]["measured"]
    assert meas["genus"] == 0
    assert meas["through_holes"] == 0
    assert meas["solidity"] == pytest.approx(1.0, abs=1e-3)


def test_multibody_genus_is_none_not_negative(tmp_path):
    """Two disjoint boxes: euler=4, so the naive (2-euler)/2 gives a bogus -1.
    measure() must report genus/through_holes as None for multi-body meshes so
    the gate skips them rather than comparing against garbage."""
    code = (
        "import cadquery as cq, os\n"
        "OUT = os.environ['CAD_OUT']\n"
        "a = cq.Workplane('XY').box(10, 10, 10)\n"
        "b = cq.Workplane('XY').box(10, 10, 10).translate((30, 0, 0))\n"
        "part = a.union(b, clean=False)\n"
        "cq.exporters.export(part, os.path.join(OUT, 'part.stl'))\n"
    )
    gen = core.generate(code=code, out_dir=str(tmp_path), stem="part")
    assert gen["success"], gen.get("stderr")
    meas = core.measure(stl=gen["stl"])["report"]["measured"]
    assert meas["n_shells"] == 2
    assert meas["genus"] is None
    assert meas["through_holes"] is None


# ---- Wave 1.2: ReAct error-feedback loop, end-to-end (real OCC failure) ------

def test_react_loop_autocorrects_bad_fillet(tmp_path):
    """A part that fails on first generate (fillet radius too large for the edge)
    gets auto-corrected within the iteration cap.

    First attempt: a 10mm cube with a 50mm fillet → OCC raises StdFail_NotDone.
    The loop summarizes that traceback into the observation; the (deterministic)
    agent reads last_error and picks a valid 2mm radius on the next attempt.
    """
    BROKEN = (
        "import cadquery as cq, os\n"
        "OUT = os.environ['CAD_OUT']\n"
        "part = cq.Workplane('XY').box(10, 10, 10).edges('|Z').fillet(50)\n"
        "cq.exporters.export(part, os.path.join(OUT, 'part.stl'))\n"
        "cq.exporters.export(part, os.path.join(OUT, 'part.step'))\n"
    )
    FIXED = (
        "import cadquery as cq, os\n"
        "OUT = os.environ['CAD_OUT']\n"
        "part = cq.Workplane('XY').box(10, 10, 10).edges('|Z').fillet(2)\n"
        "cq.exporters.export(part, os.path.join(OUT, 'part.stl'))\n"
        "cq.exporters.export(part, os.path.join(OUT, 'part.step'))\n"
    )

    def code_fn(obs):
        # The agent: use the summarized error to switch from the broken radius.
        if obs["last_error"] is None:
            return BROKEN
        # Confirm the loop actually surfaced the OCC kernel failure as context.
        assert "StdFail" in obs["last_error"]["error_type"] or \
               "radius" in obs["last_error"]["hint"].lower()
        return FIXED

    res = core.generate_with_retry(
        code_fn, prompt="a 10mm cube with lightly rounded vertical edges",
        out_dir=str(tmp_path), max_iters=3,
    )

    assert res["success"] is True, res["history"]
    assert res["attempts"] == 2
    assert res["history"][0]["success"] is False
    assert res["history"][0]["error"]["error_type"].startswith("StdFail") or \
           "BRep" in res["history"][0]["error"]["message"]
    assert res["history"][1]["success"] is True
    assert Path(res["result"]["stl"]).exists()


# ---- SECURITY (a): scrubbed env, end-to-end (the real proof) -----------------

def test_generated_code_cannot_read_parent_secret(tmp_path, monkeypatch):
    """ADR-0001, demonstrated end-to-end: a secret in the PARENT process env is
    INVISIBLE to model-authored code running in the generate subprocess.

    We plant a sentinel OPENROUTER_API_KEY in os.environ, then run generated code
    that dumps os.environ to a file. The dump must NOT contain the sentinel — the
    scrubbed allowlist env (ADR-0001) kept it out of the child entirely.
    """
    SENTINEL = "sk-or-PARENT-SECRET-MUST-NOT-LEAK-9173"
    monkeypatch.setenv("OPENROUTER_API_KEY", SENTINEL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-also-secret")

    # The generated code (an attacker would do this): write the whole env out,
    # then still produce a valid STL so generate() reports success and we KNOW
    # the code actually ran (a no-op proves nothing).
    EXFIL = (
        "import os, json, cadquery as cq\n"
        "OUT = os.environ['CAD_OUT']\n"
        "open(os.path.join(OUT, 'env_dump.json'), 'w').write(json.dumps(dict(os.environ)))\n"
        "cq.exporters.export(cq.Workplane('XY').box(10, 10, 10), os.path.join(OUT, 'part.stl'))\n"
    )
    res = core.generate(code=EXFIL, out_dir=str(tmp_path), stem="part")
    assert res["success"], res.get("stderr")   # the code ran AND produced an STL

    dump = json.loads((tmp_path / "env_dump.json").read_text())
    # The headline assertion: secrets did not reach the child.
    assert "OPENROUTER_API_KEY" not in dump, "OPENROUTER_API_KEY leaked to generated code!"
    assert "ANTHROPIC_API_KEY" not in dump
    assert SENTINEL not in dump.values()
    # CAD_OUT is the one var we DO inject; PATH survives so the interpreter runs.
    assert dump["CAD_OUT"] == str(tmp_path)
    assert "PATH" in dump
