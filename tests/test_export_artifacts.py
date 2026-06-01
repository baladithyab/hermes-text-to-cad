"""Wave uplift-4 — generator contract: GLB + topology sidecar (ADR-0013).

Two layers:
  - PURE (no CAD stack): the topology_schema shape + _body_color extraction,
    loaded from scripts/export_artifacts.py bare (heavy imports are lazy).
  - INTEGRATION (venv-gated): core.generate now emits <stem>.glb +
    <stem>.topology.json; the sidecar records AUTHORITATIVE geometry from the STL
    (correct body count/validity, never the per-face-fragmented GLB) plus a
    distinct render_bodies color manifest from the GLB.
"""
from __future__ import annotations

import importlib.util
import json
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


def _load_export_module():
    """Load scripts/export_artifacts.py bare — trimesh/numpy are lazy inside
    functions, so the schema/helpers test without the CAD stack."""
    path = SCRIPTS / "export_artifacts.py"
    spec = importlib.util.spec_from_file_location("cad_export_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def export_mod():
    return _load_export_module()


# ---- pure: topology_schema shape ---------------------------------------------

def test_topology_schema_shape(export_mod):
    topo = export_mod.topology_schema(
        units="mm", bbox_min=[0, 0, 0], bbox_max=[40, 30, 20],
        counts={"solids": 1, "faces": 12, "vertices": 8},
        bodies=[{"id": 0, "name": "body_0"}], watertight=True, valid=True,
        source_hash="abc123")
    assert topo["schemaVersion"] == export_mod.SCHEMA_VERSION
    assert topo["units"] == "mm"
    assert topo["bbox"]["size"] == [40.0, 30.0, 20.0]  # max - min
    assert topo["bbox"]["min"] == [0.0, 0.0, 0.0]
    assert topo["counts"]["solids"] == 1
    assert topo["watertight"] is True and topo["valid"] is True
    assert topo["sourceHash"] == "abc123"
    # JSON-serialisable
    json.loads(json.dumps(topo))


def test_topology_schema_size_is_max_minus_min(export_mod):
    topo = export_mod.topology_schema(
        units="mm", bbox_min=[-5, -5, 0], bbox_max=[5, 5, 10],
        counts={}, bodies=[], watertight=False, valid=False)
    assert topo["bbox"]["size"] == [10.0, 10.0, 10.0]


def test_body_color_handles_missing(export_mod):
    class _NoVisual:
        visual = None
    assert export_mod._body_color(_NoVisual()) is None


# ---- integration: core.generate emits GLB + sidecar --------------------------

venv_or_skip = pytest.mark.skipif(not core.venv_ready(),
                                  reason="CAD venv (~/.venvs/cad) not ready")

PLAIN_CODE = (
    'import cadquery as cq, os\n'
    'OUT=os.environ["CAD_OUT"]\n'
    'p=cq.Workplane("XY").box(30,20,10).faces(">Z").hole(6)\n'
    'cq.exporters.export(p, os.path.join(OUT,"part.stl"))\n'
    'cq.exporters.export(p, os.path.join(OUT,"part.step"))\n'
)

COLORED_ASSEMBLY_CODE = (
    'import cadquery as cq, os\n'
    'OUT=os.environ["CAD_OUT"]\n'
    'base=cq.Workplane("XY").box(40,40,10); knob=cq.Workplane("XY").box(12,12,12).translate((0,0,11))\n'
    'asm=cq.Assembly()\n'
    'asm.add(base, name="base", color=cq.Color(0.2,0.45,0.85))\n'
    'asm.add(knob, name="knob", color=cq.Color(0.9,0.35,0.2))\n'
    'comp=base.union(knob)\n'
    'cq.exporters.export(comp, os.path.join(OUT,"part.stl"))\n'
    'cq.exporters.export(comp, os.path.join(OUT,"part.step"))\n'
    'from cadquery.occ_impl.exporters.assembly import exportGLTF\n'
    'exportGLTF(asm, os.path.join(OUT,"part.glb"))\n'
)


@pytest.mark.integration
@venv_or_skip
def test_generate_emits_glb_and_topology(tmp_path):
    res = core.generate(code=PLAIN_CODE, out_dir=str(tmp_path), stem="part")
    assert res["success"], res.get("stderr")
    assert res["glb"] and Path(res["glb"]).exists()          # synthesized
    assert res["topology"] and Path(res["topology"]).exists()
    topo = json.loads(Path(res["topology"]).read_text())
    assert topo["schemaVersion"] == 1
    assert topo["counts"]["solids"] == 1
    assert topo["valid"] is True and topo["watertight"] is True
    assert topo["sourceHash"]                                # provenance present
    assert topo["meshSource"] == "part.stl"                  # authoritative = STL


@pytest.mark.integration
@venv_or_skip
def test_generate_emit_glb_false_skips(tmp_path):
    res = core.generate(code=PLAIN_CODE, out_dir=str(tmp_path), stem="part",
                        emit_glb=False)
    assert res["success"], res.get("stderr")
    assert res["glb"] is None
    assert res["topology"] is None
    assert not (tmp_path / "part.glb").exists()
    assert not (tmp_path / "part.topology.json").exists()


@pytest.mark.integration
@venv_or_skip
def test_colored_assembly_glb_kept_and_manifest_distinct(tmp_path):
    """An agent-authored COLORED GLB is kept (not overwritten), and the sidecar
    records the AUTHORITATIVE STL geometry (1 valid solid) plus a render_bodies
    manifest of the DISTINCT materials (base/knob) — not the per-face fragments
    cadquery's GLTF export produces, and not valid=False from those fragments."""
    res = core.generate(code=COLORED_ASSEMBLY_CODE, out_dir=str(tmp_path), stem="part")
    assert res["success"], res.get("stderr")
    topo = json.loads(Path(res["topology"]).read_text())
    # authoritative geometry from the STL: a unioned 2-box part is 1 valid solid
    assert topo["counts"]["solids"] == 1
    assert topo["valid"] is True
    assert topo["meshSource"] == "part.stl"
    # distinct render materials recovered (collapsed from face fragments)
    names = {b["name"] for b in topo.get("render_bodies", [])}
    assert names == {"base", "knob"}, names
    colors = [tuple(b["color"]) for b in topo["render_bodies"] if b["color"]]
    assert len(set(colors)) == 2  # two distinct materials


@pytest.mark.integration
@venv_or_skip
def test_corrupt_agent_glb_is_replaced_not_kept(tmp_path):
    """Review LOW-finding regression: a nonzero-but-corrupt agent-authored
    part.glb must NOT be kept as the render artifact — export validates it loads
    and synthesizes a replacement when it doesn't."""
    code = (
        'import cadquery as cq, os\n'
        'OUT=os.environ["CAD_OUT"]\n'
        'p=cq.Workplane("XY").box(20,20,10)\n'
        'cq.exporters.export(p, os.path.join(OUT,"part.stl"))\n'
        'cq.exporters.export(p, os.path.join(OUT,"part.step"))\n'
        # write a corrupt but NONZERO glb (valid header magic, garbage body)
        'open(os.path.join(OUT,"part.glb"),"wb").write(b"glTF\\x02\\x00\\x00\\x00" + b"\\x00"*64)\n'
    )
    res = core.generate(code=code, out_dir=str(tmp_path), stem="part")
    assert res["success"], res.get("stderr")
    # the kept/synthesized GLB must actually load
    import trimesh
    loaded = trimesh.load(res["glb"])
    geoms = loaded.geometry if hasattr(loaded, "geometry") else {"_": loaded}
    assert len(geoms) > 0, "GLB did not load — corrupt file was kept"


@pytest.mark.integration
@venv_or_skip
def test_generated_glb_renders_with_color(tmp_path):
    """The whole pipeline: a colored assembly -> kept GLB -> PBR render picks the
    GLB and shows per-body color (backend vtk-pbr-glb, many distinct colors)."""
    res = core.generate(code=COLORED_ASSEMBLY_CODE, out_dir=str(tmp_path), stem="part")
    assert res["success"], res.get("stderr")
    rres = core.render(stl=res["stl"])  # given the STL, render prefers the sibling GLB
    assert rres["success"], rres.get("stderr")
    if rres["backend"].startswith("vtk"):
        assert rres["backend"].endswith("glb"), \
            f"expected GLB-sourced render, got {rres['backend']}"
