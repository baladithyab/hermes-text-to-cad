# Vision Review Rubric & Scatter Recipe

The qualitative gate. The render montage (front/top/iso) is the input. Goal: catch **intent mismatches** that the numeric gate can't — wrong shape, wrong orientation, missing/extra features, bad proportions, parts in the wrong place.

## What reviewers grade (per the spec)
1. **Identity** — is this recognizably the requested object? (a mug vs a cube)
2. **Feature presence** — every feature in the spec visible? (the hole, the slot, the mounting tabs)
3. **Feature placement** — features in the RIGHT location/face/orientation? (hole on top, not the side)
4. **Proportions** — do relative dimensions look right? (the numeric gate has exact dims; vision checks they read correctly)
5. **Orientation** — is the part right-way-up / not mirrored?
6. **Obvious defects** — disconnected bits, intersecting/overlapping solids, paper-thin walls, gaps.
7. **Manufacturability eyeball** — overhangs (print), tiny features, sharp internal corners (machining). Soft signal only.

Reviewers should answer per-criterion PASS/FAIL/UNSURE with a one-line reason, then a verdict: **matches intent / needs fixes (list) / can't tell (need different render)**.

## Solo vs scatter — cost discipline
- **Solo `vision_analyze`** for simple/low-stakes parts (bracket, spacer, washer, plate-with-holes). One call, cheap.
- **Cross-family scatter** for non-trivial geometry, assemblies, organic shapes, or when the numeric gate is borderline.

## Scatter recipe (from [[model-roster]] + [[parallel-critique]])
Embed the montage path; give each reviewer `toolsets:[file]` so it can load the image. **Always include Gemini — strongest vision reviewer.** Never two from the same family.

```python
delegate_task(tasks=[
  {"goal": "Vision-review this CAD render montage against the spec. <rubric>. Spec: <spec>. Montage: /path/montage.png",
   "model": "google/gemini-3.1-pro-preview", "provider": "openrouter", "toolsets": ["file","vision"]},
  {"goal": "<same>", "model": "anthropic/claude-opus-4.8", "provider": "openrouter", "toolsets": ["file","vision"]},
  {"goal": "<same>", "model": "openai/gpt-5.5",            "provider": "openrouter", "toolsets": ["file","vision"]},
])
```
- **Convergent (>=2/3) findings = must-fix.** Apply in the iterate step.
- **Solo findings**: sanity-check against the render yourself before acting (solo-P0 false-positive discipline from `parallel-critique`).
- Verify `result.model` per task matches what you requested (route-fidelity check from `model-roster`).

## Render limitations to keep in mind
- Vision is good at GROSS errors, weak at sub-mm precision — **precision is the numeric gate's job, not vision's**. Don't ask reviewers to judge exact dimensions from pixels.
- matplotlib-fallback renders (~5/10) have depth artifacts; if reviewers say "can't tell," get a VTK render (need a display) before trusting a FAIL.
- If a feature is hidden in all 3 standard views (e.g. internal channel), add a section/cutaway render before review.