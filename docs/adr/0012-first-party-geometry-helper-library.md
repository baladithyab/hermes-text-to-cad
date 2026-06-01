# 12. First-party parametric geometry-helper library (scripts/cad_helpers.py)

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (reference-uplift Wave 3)

## Context and Problem Statement

Common organic/parametric forms — teardrops, lofts, sweeps, revolves, fillets,
shells, flush emblems, disc-in-ring buttons — are re-derived per part by the
generating model, and they are exactly where output comes out *deformed*: an
oversized fillet that crashes the OCC kernel, a twisted loft between mismatched
profiles, a revolve whose profile crosses the axis and self-intersects, a shell
whose wall exceeds half the local span. The clean-room study (§D) found that
both references lean on a reusable parametric vocabulary (CADAM routes organic/
threaded shapes to library primitives rather than stacking cubes). We want the
same leverage, but the reference vocabularies are all-rights-reserved (CADAM's is
OpenSCAD/BOSL2), so we must build our own.

## Considered Options

1. **Vendor / port a library (BOSL2-style).** Fast vocabulary, but BOSL2 is GPL
   and OpenSCAD-only, and the references are all-rights-reserved — a license
   non-starter for an MIT repo.
2. **First-party MIT helpers in cadquery/build123d**, each self-validating
   (returns only a watertight, winding-consistent solid or raises). Original
   authorship; integrates with the existing CadQuery backend and STEP export.
3. **No helper library** — keep relying on the model to author every form inline.
   Status quo; leaves the dominant deformation sources unaddressed.

## Decision Outcome

Chosen: **option 2** — `scripts/cad_helpers.py`, first-party MIT, clean-room
(no reference code or BOSL2 vendored). Eight helpers — `teardrop`,
`loft_profiles`, `sweep_along`, `revolve_profile`, `safe_fillet`, `shell_solid`,
`flush_emblem`, `disc_in_ring_button` — each taking explicit named float params,
defaulting the origin to part center, and **validating its own output**: a
private `_assert_solid` runs OCC `isValid` + a tessellate-to-mesh watertight/
winding check and raises `ValueError` on a degenerate/non-manifold result rather
than returning broken geometry. The anti-deformation guards are baked in:
`safe_fillet` clamps the radius to a fraction of the shortest selected edge and
retries smaller on OCC failure; `shell_solid` rejects a wall ≥ half the min span;
`revolve_profile` asserts the profile stays on one side of the axis;
`disc_in_ring_button` asserts `disc_d < ring_id − 2·gap` and coaxiality. Heavy
imports (cadquery/trimesh) are lazy inside each function so the module loads in a
bare interpreter for signature/param-validation tests (the subprocess-isolation
invariant).

### Consequences

- **Positive**: the dominant deformation/crash sources are handled once, with
  validation, instead of being re-derived (and re-broken) per part — directly
  serves "non-deformed" output.
- **Positive**: original MIT authorship keeps the repo's license clean; no
  all-rights-reserved or GPL code enters.
- **Positive**: every helper either returns a proven-watertight solid or raises a
  clear error — no silent broken geometry reaches the gate.
- **Negative**: the validation tessellates each helper's output (a temp STL +
  trimesh check), adding per-call cost — acceptable for generation, but callers
  building many helpers in a tight loop pay it each time.
- **Negative**: the vocabulary is small (8 forms) and cadquery-specific; forms
  outside it still fall to inline model authoring, and the helpers don't yet
  compose into a single assembly/STEP-with-colors (that is Wave 4's contract).
- **Neutral**: `disc_in_ring_button` returns a 2-body compound (order is the
  contract since `makeCompound` drops labels); callers must know index 0 = ring,
  1 = disc.
