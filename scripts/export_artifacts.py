#!/usr/bin/env python3
"""export_artifacts.py — emit the GLB + topology sidecar alongside STL/STEP.

Completes the generator contract (ADR-0013): a successful cad_generate already
writes <stem>.stl (mesh gate) + <stem>.step (B-rep, CadQuery only). This adds the
two artifacts the reference pipeline (docs/research/reference-steal.md §C) makes
the machine-readable ground truth:

  <stem>.glb            — a glTF the PBR render gate (ADR-0010) consumes for
                          per-body color. If the generated code already wrote a
                          (colored) <stem>.glb, we KEEP it; otherwise we
                          synthesize one from the STL so the render gate always
                          finds a GLB sibling.
  <stem>.topology.json  — a versioned manifest: units, bbox{min,max,size},
                          counts, per-body table (id/name/bbox/volume/color),
                          watertight/valid. The numeric gate can diff expected vs
                          recorded; the vision gate can caption the bodies.

Pure-ish: trimesh/numpy are imported INSIDE functions so the module loads bare
(the schema/helpers are unit-testable without the CAD stack). Run in ~/.venvs/cad.

Usage:
    python export_artifacts.py PART.stl [--step PART.step] [--no-glb]
                                        [--source-hash HEX] [--units mm]
Prints the topology JSON path on the last stdout line; logs what it wrote to
stderr. Exit 0 on success, 2 on a load/measure error (never a gate — this just
records artifacts).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SCHEMA_VERSION = 1


# ---- pure helpers (no heavy imports) ----------------------------------------

def _round3(seq):
    return [round(float(x), 3) for x in seq]


def topology_schema(*, units, bbox_min, bbox_max, counts, bodies,
                    watertight, valid, source_kind="python", source_hash=None):
    """Assemble the topology sidecar dict (pure — no trimesh).

    Kept separate so the SHAPE of the sidecar is unit-testable offline and stable
    across versions. bbox is recorded as min/max/size; bodies is a row-table.
    """
    bmin = _round3(bbox_min)
    bmax = _round3(bbox_max)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "sourceKind": source_kind,
        "sourceHash": source_hash,
        "units": units,
        "bbox": {
            "min": bmin,
            "max": bmax,
            "size": [round(hi - lo, 3) for lo, hi in zip(bmin, bmax)],
        },
        "counts": counts,
        "bodies": bodies,
        "watertight": bool(watertight),
        "valid": bool(valid),
    }


def _body_color(geom):
    """Best-effort per-body RGBA (0..255 ints) from a trimesh geometry, robust to
    where the color lives (PBR material baseColorFactor, visual.main_color, or a
    flat vertex color). None when no color is carried."""
    try:
        vis = getattr(geom, "visual", None)
        if vis is None:
            return None
        mat = getattr(vis, "material", None)
        if mat is not None:
            bcf = getattr(mat, "baseColorFactor", None)
            if bcf is not None:
                return [int(c) for c in bcf]
        mc = getattr(vis, "main_color", None)
        if mc is not None:
            return [int(c) for c in mc]
    except Exception:
        return None
    return None


# ---- measurement (lazy trimesh) ---------------------------------------------

def measure_topology(stl, *, units="mm", source_hash=None):
    """Build the topology sidecar dict from a mesh file (STL/GLB/OBJ/...).

    Handles a single mesh AND a multi-body scene (GLB): each connected
    body/geometry becomes a row in `bodies` with its own bbox/volume/color.
    """
    import trimesh
    import numpy as np

    loaded = trimesh.load(stl)
    bodies = []
    total_faces = 0
    total_verts = 0
    watertight_all = True

    if isinstance(loaded, trimesh.Scene):
        # a GLB scene: one row per named geometry (per-body color preserved)
        geoms = list(loaded.geometry.items())
        if loaded.bounds is not None:
            bbox_min, bbox_max = loaded.bounds[0], loaded.bounds[1]
        else:
            bbox_min = bbox_max = np.zeros(3)
        for i, (name, g) in enumerate(geoms):
            ext = g.extents if g.extents is not None else np.zeros(3)
            try:
                vol = round(float(g.volume), 3)
            except Exception:
                vol = None
            wt = bool(g.is_watertight)
            watertight_all = watertight_all and wt
            total_faces += int(len(g.faces))
            total_verts += int(len(g.vertices))
            bodies.append({
                "id": i, "name": str(name),
                "bbox_size": _round3(ext),
                "volume_mm3": vol,
                "watertight": wt,
                "color": _body_color(g),
            })
        n_solids = len(geoms)
        valid = watertight_all and n_solids > 0
    else:
        m = loaded
        # split into connected components so a multi-body STL still rows out
        try:
            parts = m.split(only_watertight=False)
        except Exception:
            parts = [m]
        if not len(parts):
            parts = [m]
        bbox_min, bbox_max = m.bounds[0], m.bounds[1]
        for i, g in enumerate(parts):
            ext = g.extents if g.extents is not None else np.zeros(3)
            try:
                vol = round(float(g.volume), 3)
            except Exception:
                vol = None
            wt = bool(g.is_watertight)
            watertight_all = watertight_all and wt
            total_faces += int(len(g.faces))
            total_verts += int(len(g.vertices))
            bodies.append({
                "id": i, "name": f"body_{i}",
                "bbox_size": _round3(ext),
                "volume_mm3": vol,
                "watertight": wt,
                "color": _body_color(g),
            })
        n_solids = len(parts)
        valid = bool(m.is_watertight) and n_solids >= 1

    counts = {"solids": n_solids, "faces": total_faces, "vertices": total_verts}
    return topology_schema(
        units=units, bbox_min=bbox_min, bbox_max=bbox_max, counts=counts,
        bodies=bodies, watertight=watertight_all, valid=valid,
        source_hash=source_hash)


def _glb_loadable(glb):
    """True if trimesh can load the GLB into a non-empty scene/mesh. Used to
    decide whether to KEEP an agent-authored GLB or synthesize a replacement —
    a nonzero-but-corrupt file must not be kept as the render artifact."""
    import trimesh
    try:
        s = trimesh.load(glb)
    except Exception:
        return False
    if isinstance(s, trimesh.Scene):
        return len(s.geometry) > 0
    return getattr(s, "faces", None) is not None and len(s.faces) > 0


def synthesize_glb(stl, glb_path):
    """Write a single-body GLB from an STL with a neutral PBR material, so the
    render gate (ADR-0010) always has a GLB sibling. A colored multi-body GLB
    that the generated code wrote itself is NOT overwritten (the caller checks
    existence first)."""
    import trimesh
    m = trimesh.load(stl, force="mesh")
    try:
        m.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                baseColorFactor=[140, 145, 158, 255],
                metallicFactor=0.2, roughnessFactor=0.5))
    except Exception:
        pass  # a GLB with default material is still fine for the renderer
    scene = trimesh.Scene()
    scene.add_geometry(m, node_name="part", geom_name="part")
    scene.export(glb_path)
    return glb_path


def emit(stl, *, step=None, out_dir=None, stem=None, emit_glb=True,
         source_hash=None, units="mm"):
    """Emit <stem>.glb (unless one already exists or emit_glb is False) and
    <stem>.topology.json next to the STL. Returns {topology, glb}.

    The GLB is resolved FIRST so the topology sidecar can describe the richest
    artifact: when an agent-authored (possibly colored, multi-body) GLB exists,
    the sidecar measures THAT (per-body color + count) rather than the unioned
    single-body STL. Falls back to the STL for the sidecar if no GLB."""
    out_dir = out_dir or os.path.dirname(os.path.abspath(stl))
    stem = stem or os.path.splitext(os.path.basename(stl))[0]

    glb_path = os.path.join(out_dir, f"{stem}.glb")
    glb_out = None
    if emit_glb:
        # Keep an agent-authored (possibly colored) GLB only if it actually LOADS —
        # a nonzero-but-corrupt part.glb would otherwise be kept as the render
        # artifact (review LOW finding). A 0-byte or unloadable file -> synthesize.
        if os.path.exists(glb_path) and os.path.getsize(glb_path) > 0 and _glb_loadable(glb_path):
            glb_out = glb_path
        else:
            if os.path.exists(glb_path) and os.path.getsize(glb_path) > 0:
                print(f"[export] existing {os.path.basename(glb_path)} did not load "
                      "— synthesizing a replacement", file=sys.stderr)
            try:
                glb_out = synthesize_glb(stl, glb_path)
            except Exception as e:
                print(f"[export] GLB synthesis failed: {e!r}", file=sys.stderr)
                glb_out = None

    # Authoritative geometry from the STL: counts/bbox/valid/watertight here MATCH
    # what cad_measure gates and what gets printed. We deliberately do NOT take
    # these from the GLB — a CadQuery-exported GLB tessellates PER FACE, so a
    # single valid solid explodes into many non-watertight face fragments (12
    # fragments + valid=False for a 2-body assembly), which would misreport both
    # the body count and validity of a perfectly good part.
    topo = measure_topology(stl, units=units, source_hash=source_hash)
    topo["meshSource"] = os.path.basename(stl)

    # Color manifest from the GLB (when present): the per-body name+color the
    # vision gate correlates against the render. Grouped by color so face-fragment
    # explosion collapses back to the distinct materials the agent assigned.
    if glb_out:
        topo["render_bodies"] = _render_color_manifest(glb_out)

    topo_path = os.path.join(out_dir, f"{stem}.topology.json")
    with open(topo_path, "w") as f:
        json.dump(topo, f, indent=2)

    return {"topology": topo_path, "glb": glb_out}


def _render_color_manifest(glb):
    """Distinct render bodies (name-stem + color) from a GLB, for the vision gate.

    CadQuery's GLTF export fragments each solid into per-face meshes named
    base/base_1/base_2…; we collapse by the leading name stem AND color so the
    manifest lists the DISTINCT materials the agent assigned (e.g. base=blue,
    knob=red), not every triangle group. Best-effort; [] on any failure."""
    import trimesh
    try:
        s = trimesh.load(glb)
    except Exception:
        return []
    if not isinstance(s, trimesh.Scene):
        return []
    seen = {}
    order = []
    for name, g in s.geometry.items():
        stem_name = str(name).rsplit("_", 1)[0] if "_" in str(name) else str(name)
        color = _body_color(g)
        key = (stem_name, tuple(color) if color else None)
        if key not in seen:
            seen[key] = {"name": stem_name, "color": color}
            order.append(key)
    return [seen[k] for k in order]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("--step")
    ap.add_argument("--no-glb", action="store_true")
    ap.add_argument("--source-hash")
    ap.add_argument("--units", default="mm")
    a = ap.parse_args(argv)

    try:
        res = emit(a.stl, step=a.step, emit_glb=not a.no_glb,
                   source_hash=a.source_hash, units=a.units)
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        sys.exit(2)

    glb = res.get("glb")
    print(f"[export] topology={os.path.basename(res['topology'])} "
          f"glb={os.path.basename(glb) if glb else 'none'}", file=sys.stderr)
    print(res["topology"])  # stdout last line = sidecar path


if __name__ == "__main__":
    main()
