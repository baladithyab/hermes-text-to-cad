# 7. Optional CoT plan step (cad_plan), deterministic structured JSON

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (Wave 2.3)

## Context and Problem Statement

CAD-Coder shows an explicit chain-of-thought modeling plan (primitives →
operations → features → export) measurably improves codegen validity. We want an
optional `cad_plan` step that emits a structured plan before `cad_generate`. The
question is whether `core` should make a model call (it must not — the loop's
LLM-free invariant) and what the plan's shape is.

## Considered Options

1. **Deterministic scaffold in core** — `core.plan(prompt)` builds a structured
   plan skeleton from the derived spec + light NL heuristics; the agent (caller)
   fills/expands it. No model call in core.
2. **Model call in core** — `core.plan` calls a VLM/LLM to author the plan.
3. **Schema-only** — ship just a JSON schema and let the agent produce the plan
   entirely, core does nothing.

## Decision Outcome

Chosen: **Option 1 (deterministic scaffold)**, consistent with how
`derive_spec`/`iterate` already keep `core` LLM-free and unit-testable offline.

`core.plan(prompt)` returns a structured, JSON-serializable plan:

```json
{ "prompt": "...",
  "spec": { ...derive_spec output... },
  "primitives": ["box 40x30x20"],        // seeded from bbox/keywords
  "operations": [],                        // agent appends booleans/shell/fillet
  "features":   ["through_hole x1"],       // seeded from spec.through_holes
  "export": ["part.stl", "part.step"],
  "notes": "Agent: expand operations/features into concrete CadQuery calls." }
```

It is a **scaffold + checklist**, not a finished plan: the deterministic part is
what we can derive with certainty (bbox primitive, declared hole/shell features,
the mandatory export step); the agent expands `operations`/`features` into real
CadQuery before calling `cad_generate`. This keeps the tool testable (pure,
offline) while still giving the model the CoT structure CAD-Coder rewards.
`cad_plan` is **optional** in the loop — generate works without it.

### Consequences

- Good: deterministic, unit-testable, no key/network; reuses derive_spec.
- Good: gives the agent a structured starting point that ties the plan to the
  same spec the numeric gate will assert (plan ↔ gate coherence).
- Bad: the scaffold is intentionally incomplete — it does not itself produce
  runnable code; documented as an agent-assist, not an autonomous planner.
- Neutral: registered as `cad_plan`; integration test shows the plan's `export`
  list and spec feeding a subsequent `cad_generate`.
