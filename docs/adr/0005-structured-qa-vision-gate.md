# 5. Structured Yes/No-Q&A vision gate (CADCodeVerify), free-form behind a flag

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (Wave 2.1)

## Context and Problem Statement

The vision gate (`scatter_review.py`) currently asks each model for a free-form
critique with an ad-hoc JSON verdict. CADCodeVerify (ICLR 2025) shows that having
the VLM **generate spec-derived Yes/No questions, answer each against the render
with chain-of-thought, and turn the "No" answers into the fix list** is more
reliable than free-form grading. We want that, while keeping our cross-family
edge and not throwing away the working free-form mode.

## Considered Options

1. **Two-call** per model: call 1 generates questions, call 2 answers them.
2. **Single-call** per model: prompt the model to emit questions AND answer them
   (with CoT) in one structured JSON response.
3. **Centralized questions**: one model generates questions, all models answer
   the same set.

## Decision Outcome

Chosen: **Option 2 (single-call structured)**, with the existing free-form mode
preserved behind `--mode free` (default `--mode qa`).

Single-call halves latency/cost vs two-call and keeps each model's questions
grounded in its own reading of the image. Centralized questions would reintroduce
a single-model bias we explicitly avoid (cross-family is our edge). Each reviewer
returns:

```json
{ "questions": ["Is there a through-hole on the top face?", ...],
  "answers": [ {"q_index":0,"reasoning":"...CoT...","answer":"no"}, ... ],
  "verdict": "matches|needs_fixes|cant_tell",
  "must_fix": ["Add the missing top-face through-hole", ...] }
```

`must_fix` is derived from "no" answers. Cross-family convergence is unchanged:
a finding that ≥2/3 reviewers flag is a hard must-fix. The parser tolerates
fenced ```json blocks and minor drift; on parse failure that reviewer is recorded
as `cant_tell` (never silently dropped). `core.review` parses each reviewer to
`{questions, answers, must_fix}` and aggregates a convergent `must_fix`.

### Consequences

- Good: matches the CADCodeVerify finding; failures are explicit, spec-anchored
  questions, not vibes. Cross-family convergence preserved.
- Good: free-form mode retained for cheap/simple parts (`--mode free`).
- Bad: more output tokens per call (CoT). Acceptable for a gate.
- Neutral: questions are model-generated, so two reviewers may ask different
  questions — convergence is computed on `must_fix` semantics, not question text.
