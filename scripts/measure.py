#!/usr/bin/env python3
"""measure.py — quantitative gate for the CAD loop.

Loads a mesh (STL/3MF/OBJ/PLY), measures geometry, and (optionally) asserts it
against a spec JSON. Prints a JSON report to stdout. Exit code is 0 if the gate
passes (or no spec given), 1 if any assertion fails — so callers can branch on it.

Usage:
    python measure.py PART.stl                       # measure only
    python measure.py PART.stl --spec spec.json      # measure + gate
    python measure.py PART.stl --bbox 40,30,20 --tol 0.1   # inline bbox gate

spec.json (all keys optional):
    {
      "bbox_mm": [40, 30, 20],     # sorted-compared, within tol
      "tol_mm": 0.1,
      "watertight": true,          # require printable/manifold
      "min_volume_mm3": 100,
      "max_volume_mm3": 100000,
      "max_shells": 1,             # upper bound on connected bodies

      # --- topological / feature-count vocabulary (CADTests pattern) ---
      "shells": 1,                 # EXACT connected-body count
      "through_holes": 1,          # # of holes that pass through the body (genus,
                                   #   single-body parts). Catches a missing hole
                                   #   that a bbox-only gate can't see — identical
                                   #   bbox, different topology.
      "genus": 1,                  # exact topological genus (handles/tunnels)
      "min_solidity": 0.3,         # volume / convex-hull volume (>= )
      "max_solidity": 1.0          # volume / convex-hull volume (<= )
    }

Measured fields added for the above: euler_number, genus, through_holes, solidity.
NOTE on feasibility: measured hole-DIAMETER and reliable wall-THICKNESS need
ray/section backends (rtree/embree/shapely) not present in the minimal CAD venv,
so they are intentionally not gated here — see PLAN.md Wave 1.1 deferral note.
"""
import sys, os, json, argparse

# Ensure the sibling placement.py is importable however measure.py is loaded
# (as a script, as the --spec CLI, or via the test module loader which does
# spec_from_file_location without adding scripts/ to sys.path). placement is
# pure stdlib so this import never drags in the CAD stack.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def measure(path):
    import trimesh
    m = trimesh.load(path, force="mesh")
    ext = m.bounding_box.extents.tolist()
    # connected-components count (robust): prefer graph-based body_count, fall back to split()
    n_shells = None
    try:
        n_shells = int(m.body_count)  # trimesh: # of connected components via face adjacency
    except Exception:
        try:
            parts = m.split(only_watertight=False)
            n_shells = int(len(parts)) if parts is not None else None
        except Exception:
            n_shells = None

    # Topology: for ONE closed orientable surface, genus = (2 - euler) / 2 and
    # equals its handle (tunnel) count — i.e. the # of through-holes. This is the
    # CADTests centerpiece: a solid box (genus 0) and a box with a through-hole
    # (genus 1) have IDENTICAL bounding boxes but different genus, so a bbox-only
    # gate can't tell them apart but this can.
    #
    # The (2 - euler)/2 formula is ONLY valid for a single connected watertight
    # body. For S separate bodies trimesh reports euler = 2S, so (2 - euler)/2 =
    # 1 - S goes negative (e.g. two boxes -> -1) — garbage. So we only define
    # genus/through_holes for the single-body watertight case and leave both None
    # otherwise (the gate then skips those checks rather than comparing nonsense).
    # NOTE: genus counts handles, which for a single body also includes any fully
    # ENCLOSED cavity's contribution; a sealed internal void instead shows up as
    # an extra shell (n_shells > 1), so the single-body guard keeps a sealed
    # hollow part out of the through-hole count. Detecting an open tunnel vs a
    # blind pocket precisely needs a ray/section backend (deferred — see header).
    genus = None
    through_holes = None
    try:
        if m.is_watertight and n_shells == 1:
            euler = int(m.euler_number)
            genus = int(round((2 - euler) / 2.0))
            through_holes = genus
    except Exception:
        genus = None
        through_holes = None

    # Solidity: volume / convex-hull volume. ~1.0 for convex/filled parts; drops
    # as material is removed (pockets, holes, lattices). A cheap shape signal.
    solidity = None
    try:
        ch_vol = float(m.convex_hull.volume)
        if ch_vol > 0:
            solidity = round(float(m.volume) / ch_vol, 4)
    except Exception:
        solidity = None

    # ---- placement / manifold sanity (Wave 3.x) -----------------------------
    # The hard-won lesson: a numeric PASS (bbox/volume/watertight) does NOT mean
    # the part is correctly PLACED or even manifold. These fields let the gate
    # check centroid placement, symmetry, and degeneracy on top of the numbers.

    # UNSORTED per-axis bbox bounds. bbox_mm/bbox_sorted above are the SIZE
    # (sorted) — useless for axis-aligned placement, so we ALSO expose the raw
    # min/max corners. trimesh m.bounds is [[xmin,ymin,zmin],[xmax,ymax,zmax]].
    bbox_min = None
    bbox_max = None
    try:
        b = m.bounds
        bbox_min = [round(float(x), 3) for x in b[0].tolist()]
        bbox_max = [round(float(x), 3) for x in b[1].tolist()]
    except Exception:
        bbox_min = None
        bbox_max = None

    # Per connected-component centroid (NOT only_watertight, so open shells still
    # report a centroid). Each is the component's centre-of-mass; [] on failure.
    body_centroids = []
    try:
        parts = m.split(only_watertight=False)
        if parts is not None:
            for p in parts:
                try:
                    body_centroids.append([round(float(x), 3) for x in p.center_mass.tolist()])
                except Exception:
                    continue
    except Exception:
        body_centroids = []

    # Winding consistency: every shared edge traversed in opposite directions by
    # its two faces — a prerequisite for a sane (orientable, sign-consistent)
    # surface. trimesh exposes is_winding_consistent.
    is_winding_consistent = None
    try:
        is_winding_consistent = bool(m.is_winding_consistent)
    except Exception:
        is_winding_consistent = None

    # Degenerate faces: ~zero-area triangles (collinear/coincident verts). They
    # bloat the mesh and confuse downstream booleans/slicers without changing the
    # bbox or (much) the volume — exactly the kind of rot a numeric gate misses.
    degenerate_faces = None
    try:
        areas = m.area_faces  # per-face area, numpy array
        # tolerance relative to the mean face area so it scales with part size;
        # falls back to a small absolute floor for tiny/empty meshes.
        try:
            mean_area = float(areas.mean()) if len(areas) else 0.0
        except Exception:
            mean_area = 0.0
        thresh = max(mean_area * 1e-8, 1e-12)
        degenerate_faces = int((areas <= thresh).sum())
    except Exception:
        degenerate_faces = None

    # is_volume: trimesh's manifold/printable signal — watertight AND
    # winding-consistent AND positive finite volume. The single boolean a gate
    # can hang "manifold": true on.
    is_volume = None
    try:
        is_volume = bool(m.is_volume)
    except Exception:
        is_volume = None

    return {
        "file": os.path.basename(path),
        "bbox_mm": [round(x, 3) for x in ext],
        "bbox_sorted": [round(x, 3) for x in sorted(ext)],
        "bbox_min": bbox_min,
        "bbox_max": bbox_max,
        "watertight": bool(m.is_watertight),
        "volume_mm3": round(float(m.volume), 3),
        "area_mm2": round(float(m.area), 3),
        "center_mass": [round(x, 3) for x in m.center_mass.tolist()],
        "n_faces": int(len(m.faces)),
        "n_vertices": int(len(m.vertices)),
        "n_shells": n_shells,
        "euler_number": _safe_int(lambda: m.euler_number),
        "genus": genus,
        "through_holes": through_holes,
        "solidity": solidity,
        "body_centroids": body_centroids,
        "is_winding_consistent": is_winding_consistent,
        "degenerate_faces": degenerate_faces,
        "is_volume": is_volume,
    }


def _safe_int(fn):
    try:
        return int(fn())
    except Exception:
        return None


def gate(meas, spec):
    checks = []
    def add(name, ok, detail): checks.append({"check": name, "PASS": bool(ok), "detail": detail})

    if "bbox_mm" in spec:
        tol = spec.get("tol_mm", 0.1)
        exp = sorted(float(x) for x in spec["bbox_mm"])
        got = meas["bbox_sorted"]
        ok = all(abs(a-b) <= tol for a, b in zip(exp, got))
        add("bbox", ok, {"expected_sorted": exp, "measured_sorted": got, "tol_mm": tol})
    if spec.get("watertight"):
        add("watertight", meas["watertight"], {"is": meas["watertight"]})
    if "min_volume_mm3" in spec:
        add("min_volume", meas["volume_mm3"] >= spec["min_volume_mm3"],
            {"volume": meas["volume_mm3"], "min": spec["min_volume_mm3"]})
    if "max_volume_mm3" in spec:
        add("max_volume", meas["volume_mm3"] <= spec["max_volume_mm3"],
            {"volume": meas["volume_mm3"], "max": spec["max_volume_mm3"]})
    if "max_shells" in spec and meas.get("n_shells") is not None:
        add("max_shells", meas["n_shells"] <= spec["max_shells"],
            {"n_shells": meas["n_shells"], "max": spec["max_shells"]})

    # ---- topological / feature-count vocabulary (CADTests pattern) ----------
    # Each check is additive and skipped when the measured field is absent, so
    # the old bbox/watertight/volume/shells path is never broken.

    if "shells" in spec and meas.get("n_shells") is not None:
        # exact connected-body count (vs max_shells which is an upper bound)
        add("shells", meas["n_shells"] == spec["shells"],
            {"n_shells": meas["n_shells"], "expected": spec["shells"]})

    if "through_holes" in spec and meas.get("through_holes") is not None:
        add("through_holes", meas["through_holes"] == spec["through_holes"],
            {"through_holes": meas["through_holes"], "expected": spec["through_holes"]})

    if "genus" in spec and meas.get("genus") is not None:
        add("genus", meas["genus"] == spec["genus"],
            {"genus": meas["genus"], "expected": spec["genus"]})

    if "min_solidity" in spec and meas.get("solidity") is not None:
        add("min_solidity", meas["solidity"] >= spec["min_solidity"],
            {"solidity": meas["solidity"], "min": spec["min_solidity"]})

    if "max_solidity" in spec and meas.get("solidity") is not None:
        add("max_solidity", meas["solidity"] <= spec["max_solidity"],
            {"solidity": meas["solidity"], "max": spec["max_solidity"]})

    # ---- placement / manifold sanity (Wave 3.x) -----------------------------
    # "numeric-PASS != correct placement" — these go beyond bbox/volume to assert
    # the part is manifold, free of degenerate faces, and actually CENTRED /
    # PLACED where the spec says. Each is ADDITIVE: skipped silently when the
    # spec key OR the needed measured field is absent, so the existing checks
    # (and tests/test_spec.py) are never touched. The placement math lives in the
    # pure scripts/placement.py so it stays trimesh-free and unit-testable.
    import placement  # pure stdlib; sibling dir is on sys.path (module top)

    # manifold: trimesh is_volume — watertight + winding-consistent + positive
    # volume. The single boolean that says "this is a real printable solid".
    if spec.get("manifold") and meas.get("is_volume") is not None:
        add("manifold", meas["is_volume"] is True, {"is_volume": meas["is_volume"]})

    if "max_degenerate_faces" in spec and meas.get("degenerate_faces") is not None:
        add("max_degenerate_faces",
            meas["degenerate_faces"] <= spec["max_degenerate_faces"],
            {"degenerate_faces": meas["degenerate_faces"],
             "max": spec["max_degenerate_faces"]})

    # placement tolerance: explicit placement_tol_mm, else reuse the bbox tol_mm,
    # else a forgiving 0.5 mm default.
    place_tol = spec.get("placement_tol_mm", spec.get("tol_mm", 0.5))

    # symmetry: for each requested axis, the centroid must lie on the symmetry
    # plane (= the bbox-centre on that axis) within tolerance. Needs the UNSORTED
    # bbox bounds so we check the RIGHT axis (never the sorted size).
    if ("symmetry" in spec
            and meas.get("center_mass") is not None
            and meas.get("bbox_min") is not None
            and meas.get("bbox_max") is not None):
        # Accept BOTH spec shapes: the bare list ["x","y"] AND the structured form
        # derive_spec actually emits, {"mirror": ["x","y"], "count": N}. Passing the
        # dict straight to symmetry_residual() would iterate its KEYS ('mirror',
        # 'count') — neither a valid axis — yielding an empty residual that ALWAYS
        # passes (the silent-vacuous bug an adversarial review caught: a part whose
        # centroid drifted off the symmetry plane sailed through). Extract the axes.
        sym = spec["symmetry"]
        axes = sym.get("mirror", []) if isinstance(sym, dict) else sym
        residual = placement.symmetry_residual(
            meas["center_mass"], meas["bbox_min"], meas["bbox_max"], axes)
        worst = max(residual.values()) if residual else 0.0
        # An empty axis list (e.g. {"mirror": []}) means "no symmetry asserted" —
        # skip rather than vacuously pass.
        if residual:
            add("symmetry", worst <= place_tol,
                {"residual": {k: round(v, 4) for k, v in residual.items()},
                 "worst": round(worst, 4), "tol_mm": place_tol})

    # expect_centroid: the whole-part centre-of-mass must be within tolerance
    # (Euclidean) of an expected point.
    if "expect_centroid" in spec and meas.get("center_mass") is not None:
        exp = [float(x) for x in spec["expect_centroid"]]
        dist = placement._dist(meas["center_mass"], exp)
        add("expect_centroid", dist <= place_tol,
            {"expected": exp, "measured": meas["center_mass"],
             "dist": round(dist, 4), "tol_mm": place_tol})

    # expect_bodies: greedily match each expected body centroid to a measured
    # body centroid; every expected body must find a free measured body within
    # its own tol. Catches a missing/extra/misplaced body that bbox can't see.
    if "expect_bodies" in spec and meas.get("body_centroids") is not None:
        expected = [list(b.get("centroid", [])) for b in spec["expect_bodies"]]
        tols = [float(b.get("tol", place_tol)) for b in spec["expect_bodies"]]
        matches = placement.match_bodies(meas["body_centroids"], expected)
        per_body = []
        ok = True
        for m_ in matches:
            ei = m_["expected_index"]
            d = m_["dist"]
            within = (d is not None) and (d <= tols[ei])
            ok = ok and within
            per_body.append({"expected_index": ei,
                             "measured_index": m_["measured_index"],
                             "dist": (round(d, 4) if d is not None else None),
                             "tol": tols[ei], "matched": within})
        add("expect_bodies", ok, {"bodies": per_body})

    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--spec")
    ap.add_argument("--bbox", help="inline expected bbox, e.g. 40,30,20")
    ap.add_argument("--tol", type=float, default=0.1)
    a = ap.parse_args()

    meas = measure(a.path)
    spec = {}
    if a.spec:
        spec = json.load(open(a.spec))
    if a.bbox:
        spec["bbox_mm"] = [float(x) for x in a.bbox.split(",")]
        spec["tol_mm"] = a.tol

    report = {"measured": meas}
    passed = True
    if spec:
        checks = gate(meas, spec)
        passed = all(c["PASS"] for c in checks)
        report["spec"] = spec
        report["gate"] = {"checks": checks, "PASS": passed}
    print(json.dumps(report, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()