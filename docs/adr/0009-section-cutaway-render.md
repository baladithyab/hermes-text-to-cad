# 9. Section/cutaway render for internal features

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (Wave 3.2)

## Context and Problem Statement

Internal channels/bores are invisible in the standard front/top/iso views, so the
vision gate can't assess them. PLAN Wave 3.2 wants a clipping-plane render that
reveals hidden features when the spec mentions internal geometry.

## Considered Options

1. **VTK clipping plane** (`vtkClipPolyData` / a cutting plane through the centroid)
   added as a 4th montage panel when a section is warranted.
2. **trimesh `section`** → 2D cross-section polyline, rendered with matplotlib.
3. **Always section everything** — extra cost on parts with no internal features.

## Decision Outcome

Chosen: **Option 1 (VTK clip plane)**, **conditionally** — only when a section is
warranted, with an explicit `--section` override.

render.py gains a `--section [axis]` flag (axis ∈ x|y|z, default the longest
axis) that adds a clipped iso panel to the montage, revealing internal geometry
with real occlusion. core decides *whether* to request a section from the spec:
when `derive_spec`/the prompt indicates internal features (the spec carries a new
`internal_features: true` hint, set when the prompt mentions
channel/bore/cavity/hollow/internal/passage), `core.render(stl, section=True)`
passes `--section`. The matplotlib fallback gets a coarse section too (single
clipped view) so headless-without-GL still shows *something*.

`derive_spec` is extended with `internal_features` (bool) using a small keyword
set (`channel`, `bore`, `cavity`, `hollow`, `internal`, `passage`, `duct`). This
is additive and does not change any existing spec key.

### Consequences

- Good: hidden internal features become visible to the vision gate; opt-in so
  simple parts don't pay for an extra panel.
- Good: reuses the existing montage pipeline (just one more image).
- Bad: a section plane through the centroid may miss an off-center feature;
  documented — the axis is overridable and defaults to the longest axis where
  through-features usually run.
- Neutral: `internal_features` is a heuristic flag; false positives only add a
  harmless extra panel.
