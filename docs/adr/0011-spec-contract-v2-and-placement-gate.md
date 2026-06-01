# 11. Spec contract v2 (intent + frame + features + symmetry) and a placement/manifold gate

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (reference-uplift Wave 3)

## Context and Problem Statement

The user's core ask is models that are "not misinterpreted or wrong or deformed
but supposedly correct." The v0.3.0 numeric gate carried *precision* (bbox /
through-holes / shells / solidity) but had two blind spots the clean-room study
(`docs/research/reference-steal.md` §B/§C) and our own hard-won experience
flagged:

1. **Under-specified intent.** `derive_spec` extracted dimensions and hole
   counts but never recorded the *coordinate frame*, the *named features*, the
   *assumptions* made, or the user's *literal intent* — so generation could
   drift (wrong origin, embellished shape) and nothing caught it.
2. **Numeric-PASS ≠ correct placement.** A part can have the exact bbox, the
   right hole count, and be watertight, yet still be *deformed* or *mis-placed*
   — a mirrored bracket whose holes drifted off the symmetry plane, a body that
   landed at the wrong centroid, a non-manifold shell with the right envelope.
   The gate had no way to assert *where* mass/bodies sit.

## Considered Options

1. **LLM-authored spec + LLM self-critique.** Let the model emit the spec and
   judge its own placement. Powerful but non-deterministic, un-unit-testable,
   and it puts the gate inside the thing being gated.
2. **Deterministic spec contract v2 + deterministic placement gate.** Enrich the
   existing LLM-free `derive_spec`/`plan` with frame/intent/features/symmetry,
   and add dependency-free centroid/symmetry/manifold checks to the numeric gate.
3. **Vision-gate-only placement.** Rely on the cross-family VLM review to catch
   misplacement. Already in place (ADR-0005) but the CADTests finding is that
   *executable* geometric tests beat vision feedback for precision — and a VLM
   can't measure a 3 mm centroid drift.

## Decision Outcome

Chosen: **option 2**, keeping the whole contract deterministic and LLM-free
(the agent fills `operations`; `core` only asserts), consistent with ADR-0007.

**Spec contract v2** — `derive_spec` additively emits (only when warranted):
`units`, `intent` (the cleaned prompt — an *over-delivery* guard, "make a mug,
not an elaborate vessel"), `coordinate_frame` (origin chosen by part class:
plate/bracket/flange → footprint_center; cylinder/shaft/disc → axis; enclosure/
box/default → center), a structured `features` list (`{name,type,count}`),
`assumptions` (units + origin always; M3/M4/M5 → 3.4/4.5/5.5 mm clearance), and
`symmetry` (`{mirror:[axes], count}` when count>1 holes or a symmetry keyword).
`plan` gains a `coordinate_frame` echo, a per-feature *placement narrative*
(stated relative to the frame, *before* code — the anti-deformation lever), a
`claims_require` map ("never claim done unless the tool ran"), and
`repair_classes` (failure → smallest responsible fix).

**Placement/manifold gate** — `measure.py` additively measures `bbox_min`/
`bbox_max` (UNSORTED, so the gate checks the *right* axis, never the sorted
size), `body_centroids`, `is_winding_consistent`, `degenerate_faces`, and
`is_volume`; `gate()` gains `manifold`, `max_degenerate_faces`, `symmetry`
(centroid must lie on each mirror plane within tol), `expect_centroid`, and
`expect_bodies` (greedy body-centroid matching). The pure math lives in a new
trimesh-free `scripts/placement.py` (`centroid_offset`, `symmetry_residual`,
`match_bodies`) so it unit-tests offline. Every check is **additive**: it skips
silently when its spec key or measured field is absent, so the v0.3.0 gate path
is never broken.

### Consequences

- **Positive**: the gate now catches *placement* errors a bbox/volume gate
  can't — a mirrored part whose centroid drifts off the symmetry plane FAILS;
  a non-manifold result FAILS `manifold`; a wrong-centroid body FAILS
  `expect_centroid`/`expect_bodies`. Directly addresses "deformed but supposedly
  correct."
- **Positive**: `intent` + `coordinate_frame` + `assumptions` make
  *misinterpretation* visible and traceable; the plan's placement narrative makes
  the model declare frame + placement before emitting code.
- **Positive**: stays deterministic, LLM-free, stdlib-only in `core`; the spec an
  LLM would emit has the same shape but the *contract* is unit-tested offline.
- **Negative**: `derive_spec` keyword/part-class heuristics are necessarily
  approximate — an unusual phrasing may pick the wrong origin class or miss a
  symmetry; the fields are advisory unless a gate key asserts them. Mitigated by
  tests per class and by the additive-skip design (a wrong field only matters if
  a matching gate key is set).
- **Negative**: symmetry/placement checks assume the *declared* frame matches how
  the model actually built the part (origin at center, etc.); a part modeled in a
  shifted frame could false-FAIL symmetry. Documented; `placement_tol_mm` tunes
  strictness and the check skips when bounds are absent.
- **Neutral**: the spec dict is larger; the symmetry spec is a dict
  (`{mirror,count}`) — the gate accepts both that and a bare axis list (a
  cross-component mismatch here was caught by adversarial review and is now
  regression-tested at the `derive_spec → gate` seam).
