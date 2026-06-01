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
      "max_shells": 1              # require a single connected body
    }
"""
import sys, os, json, argparse


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
    return {
        "file": os.path.basename(path),
        "bbox_mm": [round(x, 3) for x in ext],
        "bbox_sorted": [round(x, 3) for x in sorted(ext)],
        "watertight": bool(m.is_watertight),
        "volume_mm3": round(float(m.volume), 3),
        "area_mm2": round(float(m.area), 3),
        "center_mass": [round(x, 3) for x in m.center_mass.tolist()],
        "n_faces": int(len(m.faces)),
        "n_vertices": int(len(m.vertices)),
        "n_shells": n_shells,
    }


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
    if "max_shells" in spec and meas["n_shells"] is not None:
        add("max_shells", meas["n_shells"] <= spec["max_shells"],
            {"n_shells": meas["n_shells"], "max": spec["max_shells"]})
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