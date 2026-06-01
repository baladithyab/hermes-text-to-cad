#!/usr/bin/env python3
"""compare.py — Chamfer-Distance similarity gate for the CAD loop (ADR-0006).

For "make it like THIS" tasks: score how close a GENERATED mesh is to a REFERENCE
mesh (STL/STEP/OBJ/PLY/3MF). Symmetric Chamfer Distance via uniform surface
sampling + nearest-neighbour (scipy cKDTree), straight from CAD-Coder's geometric
reward. CD ~ 0 for identical meshes, grows monotonically with deformation.

    CD(A,B) = mean_{a in S_A} min_{b in S_B} ||a-b||
            + mean_{b in S_B} min_{a in S_A} ||a-b||

with S_A, S_B = N points sampled uniformly by area from each surface. A fixed
seed makes the score reproducible run-to-run.

Usage:
    python compare.py GENERATED.stl REFERENCE.stl [--samples N] [--seed S]

Prints a JSON report to stdout:
    {chamfer_distance, forward, backward, normalized, samples, seed,
     bbox_diag_ref}
exit 0 on success, 2 on a load/compute error (kept distinct from a gate fail —
compare has no pass/fail, just a score).

NOTE: CD as shipped compares meshes in their OWN coordinate frames — no ICP
alignment (documented limitation). A correct part in a different pose scores
high. "normalized" = chamfer_distance / reference-bbox-diagonal, a unitless
size-comparable score.
"""
import sys
import json
import argparse


def _validate_samples(n):
    """Sample count must be a positive int — 0/negative would sample no points
    and yield an inf/nan distance. Raise so callers fail loudly (exit 2)."""
    n = int(n)
    if n <= 0:
        raise ValueError(f"--samples must be a positive integer, got {n}")
    return n


def _sample_surface(mesh, n, seed):
    """N points sampled uniformly by area from a mesh surface (seeded)."""
    import numpy as np
    import trimesh
    # trimesh.sample.sample_surface is area-weighted; seed via numpy's global
    # state for reproducibility. Clamp the seed into numpy's valid uint32 range
    # (seed+1 in chamfer_distance could otherwise exceed 2**32-1 and raise).
    np.random.seed(int(seed) % 2**32)
    pts, _ = trimesh.sample.sample_surface(mesh, n)
    return np.asarray(pts, dtype=float)


def chamfer_distance(mesh_a, mesh_b, samples=4096, seed=0):
    """Symmetric Chamfer Distance between two trimesh meshes.

    Returns (cd, forward, backward) where cd = forward + backward, forward =
    mean nearest-neighbour distance from A's samples to B's samples, backward
    the reverse. Reproducible for a fixed seed.
    """
    from scipy.spatial import cKDTree
    import numpy as np

    samples = _validate_samples(samples)
    a = _sample_surface(mesh_a, samples, seed)
    b = _sample_surface(mesh_b, samples, (seed + 1) % 2**32)  # decorrelate the two draws

    tree_b = cKDTree(b)
    tree_a = cKDTree(a)
    fwd = float(np.mean(tree_b.query(a, k=1)[0]))   # A -> B
    bwd = float(np.mean(tree_a.query(b, k=1)[0]))   # B -> A
    return fwd + bwd, fwd, bwd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("generated")
    ap.add_argument("reference")
    ap.add_argument("--samples", type=int, default=4096)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    try:
        import trimesh
        import numpy as np
        gen = trimesh.load(a.generated, force="mesh")
        ref = trimesh.load(a.reference, force="mesh")
        cd, fwd, bwd = chamfer_distance(gen, ref, samples=a.samples, seed=a.seed)
        ref_diag = float(np.linalg.norm(ref.bounding_box.extents)) or 1.0
        report = {
            "chamfer_distance": round(cd, 6),
            "forward": round(fwd, 6),
            "backward": round(bwd, 6),
            "normalized": round(cd / ref_diag, 6),
            "bbox_diag_ref": round(ref_diag, 4),
            "samples": a.samples,
            "seed": a.seed,
        }
    except Exception as e:  # load/compute failure — distinct exit, never a fake score
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        sys.exit(2)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
