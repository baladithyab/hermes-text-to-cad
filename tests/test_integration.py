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
