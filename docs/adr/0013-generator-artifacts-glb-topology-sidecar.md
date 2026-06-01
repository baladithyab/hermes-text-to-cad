# 13. Generator contract: emit GLB + topology sidecar alongside STL/STEP

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (reference-uplift Wave 4)

## Context and Problem Statement

The PBR render gate (ADR-0010) renders per-body color only when given a GLB, and
the clean-room study (`docs/research/reference-steal.md` §C) makes a topology
sidecar the machine-readable ground truth that closes the loop (the numeric gate
diffs expected vs recorded; the vision gate correlates the render against the
recorded bodies/colors). But `cad_generate` emitted only STL (mesh gate) + STEP
(B-rep) — so the render gate had no GLB to consume in the real loop, and there
was no queryable spec record of what was built. We want to converge the generator
toward the reference's clean `gen() → {shape, step_output}` contract that emits
STEP + GLB + a sidecar, without breaking the existing STL/STEP path or the
subprocess-isolation/security invariants.

## Considered Options

1. **Auto-emit on every successful generate (opt-out).** `cad_generate` writes
   `<stem>.glb` + `<stem>.topology.json` after a successful STL+STEP unless
   `emit_glb=false`. The whole pipeline works end-to-end with no extra agent step.
2. **A separate `cad_export` tool.** Keep `cad_generate` as-is; the agent calls a
   distinct tool to turn STL/STEP into GLB+sidecar. Explicit, no cost when unused,
   but one more loop step the agent must remember.
3. **Opt-in (`emit_glb=true` required).** Conservative default (no behavior
   change), but colored renders only happen when the agent opts in.

## Decision Outcome

Chosen: **option 1 (auto, opt-out)**. It makes the render gate's per-body color
and the sidecar "just work" end-to-end — the brief's goal — and the `emit_glb`
flag preserves an escape hatch (numeric-only checks). The export runs as a
**separate subprocess step** (`scripts/export_artifacts.py` in the CAD venv)
after a successful build, so it's decoupled, independently testable, and
best-effort (a failed export never sinks a good build).

Key design rule — **route each question to the artifact that answers it
honestly**:
- **GLB**: if the generated code wrote its own (colored, multi-body) `<stem>.glb`
  via a `cq.Assembly`, it is KEPT (per-body color preserved); otherwise a
  single-body GLB with a neutral PBR material is synthesized from the STL so the
  render gate always finds a GLB sibling.
- **Topology sidecar** (`<stem>.topology.json`, schemaVersion 1): records
  AUTHORITATIVE geometry from the **STL** — units, bbox{min,max,size}, counts,
  per-body table, watertight, valid, and a `sourceHash` (sha256 of the generator
  source, for provenance). It deliberately does NOT take counts/validity from the
  GLB, because CadQuery's glTF export tessellates PER FACE — a single valid solid
  explodes into many non-watertight face fragments (e.g. 12 fragments +
  valid=False for a 2-body assembly), which would misreport both body count and
  validity of a perfectly good part. A separate `render_bodies` field carries the
  DISTINCT materials (name-stem + color) recovered from the GLB for the vision
  gate to correlate.

### Consequences

- **Positive**: end-to-end colored pipeline — a `cq.Assembly` with `.color` →
  kept GLB → PBR render shows per-body color, with no extra agent step (proven on
  a blue-base/red-knob assembly).
- **Positive**: every successful build now carries a queryable spec record
  (bbox/counts/valid/sourceHash + render-color manifest) — the ground truth the
  numeric and vision gates can diff/correlate.
- **Positive**: authoritative geometry comes from the STL, so the sidecar's body
  count/validity match what `cad_measure` gates and what gets printed — the
  GLB's render-oriented face fragmentation can't corrupt the facts.
- **Negative**: every successful cadquery/openscad generate now pays a
  tessellation + GLB-write + measure cost (one extra ~1-2s subprocess). Mitigated
  by `emit_glb=false` for numeric-only runs.
- **Negative**: the synthesized single-body GLB is mono-color — true per-body
  color requires the generated code to author a colored `cq.Assembly` GLB itself
  (documented in the `cad_generate` tool description). The contract enables color
  but doesn't infer it.
- **Neutral**: the sidecar is emitted but not yet consumed by an automated gate
  check (the numeric gate still reads the STL directly); wiring a sidecar-diff
  gate is a follow-up. For now it serves the agent + vision gate as a manifest.
