# Deep Work Log

## Run 2026-05-31 — SECURITY wave + Waves 2 & 3 (started at 2a5d2cc)

Vision: harden `hermes-text-to-cad` for wide install and add verification depth
+ portability. Worked the SECURITY wave first (ship-blocker), then Wave 2
(verification depth), then Wave 3 (portability), with a concurrent adversarial
review track feeding findings back into the backlog.

### Phases
- **Investigate/architect:** read the full repo + PLAN backlog; ran empirical
  environment probes (env-inheritance leak confirmed; bwrap 0.11.2 verified;
  stock VTK is X11-only and SIGABRTs headless; Xvfb blocked on WSL by WSLg).
  Recorded 9 MADR ADRs (`docs/adr/`) grounded in those probes.
- **Execute (TDD, red→green per item):**
  - SECURITY (a) scrubbed env, (c+d) AST denylist + size cap, (b) opt-in
    bwrap/firejail sandbox, (e) SECURITY.md + README.
  - Wave 2.3 `cad_plan`, 2.2 `cad_compare` (Chamfer), 2.1 structured-Q&A gate.
  - Wave 3.1 crash-safe headless render, 3.2 section/cutaway, 3.3 OpenSCAD.
- **Concurrent review:** two adversarial cross-model review workflows.
  - SECURITY review: 6 confirmed / 33 refuted → hardened (file-secret masking
    bug, AST `os.open`/move/`..` gaps, firejail FS parity, SyntaxError hint).
  - Wave 2/3 review: 7 confirmed / 16 refuted → all fixed: a HIGH gate-integrity
    bug (a model's reply text could inject a fake `REVIEWS_JSON` block to spoof
    the vision gate FAIL→PASS — now line-anchored to the genuine last marker);
    Chamfer `samples<=0`→inf/nan and non-finite CD passing the success check;
    seed+1 uint32 overflow; the 'opening'/'hole' synonym convergence gap; and
    the section backends keeping opposite halves.

### Result
- 258 tests pass + 1 skip (full CAD venv, integration runs); 236 pass + 23 skip
  (clean venv, integration auto-skips cleanly). Pre-push smoke test green in the
  Hermes venv (plugin loads, 7 tools, v0.3.0, requires_env empty).
- Every acceptance criterion proven by a real test or run, not a claim — secret
  invisible to generated code; socket blob rejected; bwrap blocks net + masks
  secrets; CD monotonic; cross-family Q&A flags a missing hole; dead-display
  render falls back without crashing; section adds a panel; openscad path.
- Tagged v0.3.0. ADRs: 0001–0009. Deferred: Wave 3.4 (Zoo/KCL) + Wave 4 polish.
- Commit range: 2a5d2cc..HEAD on `master`, pushed to origin.

Both review teams signed off; remaining backlog (3.4, Wave 4) is explicitly
lower-priority and documented in PLAN.md.

## Run 2026-05-31 — "Steal everything" reference-uplift loop (started at d0cb9fb)

Vision: level up the plugin toward CORRECTNESS + FAITHFULNESS + RENDER QUALITY
by clean-room studying two reference CAD repos (earthtojake/text-to-cad,
Adam-CAD/CADAM — both ALL-RIGHTS-RESERVED, so techniques/ideas only, original
authorship) and adopting their best techniques in our style.

- **Baseline:** clean tree at `d0cb9fb`; 266 passed + 1 skipped in ~/.venvs/cad.
- **Env probe:** CAD venv py3.11 (cadquery 2.7, build123d 0.10, vtk 9.3, trimesh
  4.12, matplotlib 3.10). node v22 present. NO pyrender/OSMesa/PyOpenGL (EGL
  path out). WSLg `:0` socket present (VTK GLX renders in-process). masterball
  fixture = 1.4 MB colored multi-body GLB at ~/masterball_final/.
- **Waves:** 1 study → 2 render quality (PBR) → 3 generation correctness (spec
  contract + self-check + geometry sanity + organic helpers) → 4 contract+pipeline.
