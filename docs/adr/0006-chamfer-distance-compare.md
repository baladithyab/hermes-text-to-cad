# 6. Chamfer-Distance similarity via trimesh surface sampling

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (Wave 2.2)

## Context and Problem Statement

For "make it like THIS" tasks the user supplies a reference STL/STEP. We want a
`cad_compare` tool that scores how close a generated mesh is to the reference —
CAD-Coder's geometric reward. The metric must be ~0 for identical meshes and grow
monotonically with deformation, scale/translation-aware per the use case, and use
only what's in the minimal CAD venv (trimesh + scipy + numpy).

## Considered Options

1. **Symmetric Chamfer Distance** via `trimesh.sample.sample_surface` on both
   meshes + nearest-neighbor (scipy `cKDTree`) both directions, mean of both.
2. **Hausdorff distance** (max NN) — more sensitive to single outliers, less
   stable as a similarity score.
3. **Volumetric IoU** — needs watertight meshes + voxelization; brittle on open
   meshes and scale-dependent.

## Decision Outcome

Chosen: **Option 1 (symmetric Chamfer Distance)**. It is the field-standard CAD
similarity metric, robust to tessellation differences (surface sampling, not
vertex-to-vertex), and needs no extra deps. Definition:

```
CD(A,B) = mean_{a in S_A} min_{b in S_B} ||a-b||  +  mean_{b in S_B} min_{a in S_A} ||a-b||
```

with `S_A`, `S_B` = N points sampled uniformly by area from each surface
(default N=4096, configurable). Implementation notes:
- scipy `cKDTree` for O(N log N) nearest neighbors each direction.
- Sampling uses a **fixed seed** so the score is reproducible run-to-run.
- We report `chamfer_distance` (the symmetric sum), plus the two one-directional
  means, units = mm (mesh units). Optional `normalize` divides by the reference
  bounding-box diagonal → a unitless score comparable across part sizes.
- Optional pre-alignment is **out of scope** for v1 (documented): CD as shipped
  compares meshes in their own coordinate frames. "Make it like this" assumes the
  generated part targets the same frame; ICP alignment is a future add.
- Not applicable to pure-text gen (no ground truth) — documented; the tool simply
  requires both a generated and a reference mesh.

### Consequences

- Good: CD≈0 for identical meshes, monotonically increasing with deformation
  (proven by a unit test that scales/translates a mesh and asserts ordering).
- Good: dependency-free, reproducible (seeded), size-comparable (normalized).
- Bad: no alignment → a correct part in a different pose scores high; documented
  as a known limitation with ICP as the follow-up.
- Neutral: runs in the CAD venv via a new `scripts/compare.py`, same subprocess
  pattern as measure/render; core gets a `compare()` wrapper + `cad_compare` tool.
