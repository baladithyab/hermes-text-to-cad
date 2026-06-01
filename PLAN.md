# hermes-text-to-cad — Implementation Plan & Backlog

> This document is the executable backlog. It is written to be handed to an autonomous coding agent (Claude Code / Codex) for implementation. Every item has: rationale, research grounding, concrete steps, acceptance criteria, and rollback notes. Work the waves in order; items within a wave are independent and parallelizable.

## Context (read first)

`hermes-text-to-cad` is a Hermes Agent plugin that does **verified** text-to-CAD via a closed loop with two accept gates (numeric + vision). v0.1.0 ships a functional core: `cad_generate` / `cad_render` / `cad_measure` / `cad_review` tools, a `hermes cad` CLI (`setup`/`doctor`), proven scripts in `scripts/`, and a dedicated CAD venv at `~/.venvs/cad` invoked as a subprocess (keeps the heavy CAD stack out of the Hermes venv).

**Architecture is validated by the literature** — see `references/` and the SOTA note. The field's key finding (CADTests, arXiv 2605.07807): *test-based geometric feedback OUTPERFORMS vision-only feedback*. So the highest-ROI work is making the numeric gate richer and **prompt-derived**, not piling on more vision.

**Repo layout:**
```
plugin.yaml              manifest
__init__.py              register(ctx) — 4 tools + `cad` CLI
hermes_text_to_cad/
  __init__.py
  core.py                loop engine (generate/render/measure/review/doctor/setup)
scripts/                 render.py, measure.py, scatter_review.py  (run in CAD venv)
references/              cadquery + openscad cheatsheets, review-rubric, phase0-spike
templates/               part.py (CadQuery), part.scad (OpenSCAD)
tests/                   (to be built — see Wave 0)
```

**Hard rules for the implementing agent:**
- The CAD stack (cadquery/vtk/trimesh) runs ONLY in `~/.venvs/cad` via subprocess. Never `import cadquery` from the Hermes-venv plugin code.
- Tool handlers return dicts (loader JSON-encodes). Async handlers must be awaited by dispatch — add a test if any handler becomes async.
- `requires_env: []` stays empty (the install-prompt hostile-UX trap). Surface optional creds at `cad doctor` time.
- Use lazy log formatting (`logger.info("x %s", v)`), never f-strings in logs.
- Run the pre-push smoke test (below) before every commit.

---

## Wave 0 — Test harness & CI (do FIRST, everything depends on it)

**0.1 — Plugin-contract tests.** `tests/test_plugin_contract.py`: manifest parses, `register()` against a mock ctx wires exactly the 4 tools + `cad` CLI, every tool schema is valid JSON-schema. (Adapt the smoke test in the README/plugin-authoring ref.)
- *Accept:* `pytest tests/test_plugin_contract.py` green.

**0.2 — Core unit tests (mocked subprocess).** `tests/test_core.py`: mock `subprocess.run` and assert `generate/render/measure/review` build the right argv, parse stdout correctly, and surface `gate_pass` from exit codes (0=pass, 1=fail, both `success`). Test `doctor()` check shapes and `_has_openrouter_key` env+dotenv parsing.
- *Accept:* green; no real CAD venv needed (all subprocess mocked).

**0.3 — Integration test (gated on venv).** `tests/test_integration.py` marked `@pytest.mark.skipif(not venv_ready())`: real generate→render→measure on a fixture bracket; assert bbox within tol and `gate_pass`.
- *Accept:* green when `~/.venvs/cad` exists; skipped cleanly otherwise.

**0.4 — CI workflow.** `.github/workflows/ci.yml`: matrix py3.10–3.12, `pip install -e '.[dev]'` (catches the packages-list drift), run contract+core tests (integration skipped in CI unless a CAD venv is cached). Add a job that runs the pre-push smoke test.
- *Accept:* CI green on push.

---

## Wave 1 — The highest-ROI research findings (HIGH)

**1.1 — Prompt-derived geometric tests (CADTests pattern). ★ TOP PRIORITY ★**
Research: CADTests (arXiv 2605.07807) shows executable per-prompt geometric assertions beat vision feedback. Today `cad_measure` checks a hand-written fixed spec.
- Build a `cad_spec_from_prompt` tool (or a `core.derive_spec(prompt)` step) that turns an NL prompt into a structured, machine-checkable spec: dimensional assertions (`hole_d == 8 ± 0.1`), topological (`through_holes == 1`, `is_watertight`), feature counts (`barb_count == 3`), bbox, wall-thickness mins. Extend `measure.py`'s gate vocabulary to evaluate them (it already does bbox/watertight/volume/shells — add: feature-count via trimesh, wall-thickness sampling, hole-diameter detection where feasible).
- *Accept:* given a prompt + model, the derived spec catches a deliberately-wrong dimension that the old bbox-only gate missed; unit test with a fixture prompt+STL pair.
- *Rollback:* the richer gate is additive; old fixed-spec path still works.
- *DONE (v0.2.0):* `core.derive_spec(prompt)` + `cad_spec_from_prompt` tool (deterministic NL→spec, unit-tested offline); `measure.py` gate vocabulary extended with `through_holes` (mesh **genus** via Euler characteristic), exact `genus`, exact `shells`, and `min/max_solidity` (convex-hull ratio). Acceptance proven end-to-end on committed fixtures: `holed_box` and `solid_box` share an identical 40×30×20 bbox, but the prompt-derived `through_holes==1` PASSES the holed box and FAILS the solid box — the exact case bbox-only gating misses.
- *Deferred (feasibility):* measured hole-**diameter** and reliable wall-**thickness** assertions are NOT gated. They require ray-cast / section-polygon backends (`rtree`/`embree`/`shapely`/`networkx`) absent from the minimal `~/.venvs/cad`; voxel-EDT wall thickness measured ~30% low (pitch-dependent), too noisy to gate strictly. `solidity` is the dependency-free proxy shipped instead. Revisit if those backends are added to the CAD venv.
- *Scope of `through_holes` (genus):* defined only for a **single watertight body** (`n_shells==1`); `genus`/`through_holes` are `None` otherwise (the `(2-euler)/2` formula goes negative for multi-body meshes, so the gate skips rather than compares garbage). For one body, `genus == handle count`, which precisely counts open tunnels but would also count a fully-enclosed cavity's contribution — however a *sealed* cavity manifests as an extra shell (`n_shells>1`), so the single-body guard keeps blind/sealed voids out of the through-hole count. Distinguishing an open tunnel from a blind pocket exactly needs the same deferred ray/section backend.

**1.2 — ReAct error-feedback loop.** When CadQuery raises (bad fillet radius, kernel error, non-manifold), capture the traceback and feed it back as a structured observation for the next `cad_generate` iteration. Add `core.generate` already returns `stderr`; build a thin `core.iterate(prompt, history, last_error)` contract and document the loop in SKILL/README so the agent retries with the error in context.
- *Accept:* a part that fails on first generate (e.g. fillet radius too large) gets auto-corrected within the iteration cap in an integration test.

---

## Wave 2 — Verification depth (MED) — ✅ DONE (v0.3.0)

**2.1 — Structured-Q&A vision gate (CADCodeVerify pattern).** Upgrade `scatter_review.py` from free-form critique to: VLM generates 2–5 Yes/No questions derived from the spec → answers each against the render with chain-of-thought → failures become the `must_fix` list. Keep cross-family (our edge over the single-model papers). Preserve the existing free-form mode behind a flag.
- *Accept:* on a part with a known intent error (e.g. hole on wrong face), ≥2/3 reviewers' Q&A flags it; structured output parses to `{questions, answers, must_fix}`.
- *DONE ([ADR-0005](docs/adr/0005-structured-qa-vision-gate.md)):* `scatter_review.py --mode qa|free` (default qa); `parse_reviewer()` tolerant JSON, derives `must_fix` from "no" answers, unparseable→`cant_tell` (never drops a reviewer). `core.review(mode=)` + `core.aggregate_reviews()` cluster cross-family `must_fix` by token-set **Jaccard ≥0.4** (so paraphrases from different families converge → hard must-fix; `gate_pass` only when no convergent fix AND ≥1 reviewer judged). **Proven end-to-end on a real network call:** Gemini 3.1 + Opus 4.8 + GPT-5.5 all independently flagged a solid box's missing top-face through-hole, the three paraphrases converged, and the gate failed. The Jaccard clustering replaced exact-string match precisely because the real e2e test showed models paraphrase the same fix differently.

**2.2 — Chamfer-Distance similarity scoring.** For "make it like THIS" tasks where the user supplies a reference STL/STEP, add a `cad_compare` tool computing Chamfer Distance (trimesh sampling) between generated and reference meshes — straight from CAD-Coder's geometric reward. Not applicable to pure-text gen (no GT); document that.
- *Accept:* CD≈0 for identical meshes, grows monotonically with deformation; unit test.
- *DONE ([ADR-0006](docs/adr/0006-chamfer-distance-compare.md)):* `scripts/compare.py` symmetric CD via area-weighted `trimesh.sample.sample_surface` + scipy `cKDTree`, **seeded** (reproducible); reports raw + bbox-normalized scores; exit 2 (not a gate fail) on load error. `core.compare` + `cad_compare` tool. Identical-mesh CD is a tiny sampling-noise floor (∝ inter-sample spacing), proven monotonic with deformation (identical 1.11 → +2 mm 1.69 → +20 mm 7.94). *Deferred (documented):* no ICP alignment — compares in-frame; a correct part in a different pose scores high. ICP is the follow-up.

**2.3 — Explicit CoT plan step.** Add an optional `cad_plan` tool / loop step that emits a structured modeling plan (primitives → operations → features → export) before codegen. CAD-Coder shows CoT measurably helps validity.
- *Accept:* plan is structured JSON; integration test shows codegen consuming it.
- *DONE ([ADR-0007](docs/adr/0007-cad-plan-cot-step.md)):* `core.plan(prompt)` → `{prompt, spec, primitives, operations, features, export, notes}`, deterministic and seeded from `derive_spec` so the plan carries the **same spec the numeric gate asserts** (plan↔gate coherence); `operations` is the agent's to fill. `cad_plan` tool. End-to-end: the plan's spec feeds a real `cad_generate` that passes the gate derived from the same prompt.

## 🔒 SECURITY wave — ✅ DONE (ship-blocker, done first)

`cad_generate` executes model-authored Python. Threat model + trust assumptions in [`SECURITY.md`](SECURITY.md); decisions in ADRs [0001](docs/adr/0001-scrubbed-subprocess-env.md)/[0002](docs/adr/0002-opt-in-bubblewrap-sandbox.md)/[0003](docs/adr/0003-ast-denylist-defense-in-depth.md). Three defense layers:
- **(a) Scrubbed env (ADR-0001):** `core._scrubbed_env()` allowlist; `OPENROUTER_API_KEY` + all secrets dropped from every CAD subprocess. *Proven:* a sentinel secret in the parent env is invisible to generated code dumping `os.environ`.
- **(c+d) AST denylist + size cap (ADR-0003):** `safety.check_code()` rejects banned imports / `eval`/`exec` / `os.system`/`os.open`/`os.rename` / writes (incl. `..` traversal) outside `CAD_OUT`, pre-exec; 100 KB cap; `HERMES_CAD_NO_AST_CHECK=1` override. *Proven:* an `import socket` blob is rejected without running; legit cadquery passes.
- **(b) Opt-in sandbox (ADR-0002):** `HERMES_CAD_SANDBOX=1` wraps generate in bubblewrap (no net, ro-FS except `CAD_OUT`, secret dirs/files masked); firejail fallback at parity; degrade-with-warning if absent. *Proven:* under bwrap, generated code's network call is blocked and `~/.hermes/.env` + `~/.netrc` are unreadable, STL still builds.
- **(e)** `SECURITY.md` + README trust statement; `cad doctor` reports `sandbox_available`.
- *Hardening:* a concurrent cross-model adversarial review (6 confirmed / 33 refuted) found and fixed: file-type secrets (`.netrc`) were unmasked, the AST write-denylist missed `os.open`/move/`..`, and the firejail fallback lacked FS confinement.

---

## Wave 3 — Portability & coverage (MED/LOW) — ✅ 3.1–3.3 DONE (v0.3.0), 3.4 deferred

**3.1 — Headless-portable rendering (OSMesa VTK).** Today the render gate needs a display (WSLg `:0` here; matplotlib fallback elsewhere). Add an OSMesa/EGL software-GL path so VTK renders with real occlusion on a headless server with no display. Detect and prefer in `render.py`; document in `cad doctor`.
- *Accept:* on a box with no `DISPLAY` and no WSLg socket, `cad_render` still produces a z-buffered VTK montage (not the matplotlib fallback).
- *DONE ([ADR-0004](docs/adr/0004-headless-render-precedence.md)):* `render.py` precedence display → OSMesa/EGL → auto private **Xvfb** (software GLX) → matplotlib. **Critical:** the stock pip `vtk` wheel is X11-only and `rw.Render()` SIGABRTs the whole process headless (uncatchable C++ abort), so the old try/except→matplotlib was unreachable on a real headless box. Now render.py probes GL in a **disposable child subprocess** (`--gl-probe`); the abort kills only the probe and the parent falls back cleanly. *Proven:* `DISPLAY=:99` (dead) → render.py exits 0 with a matplotlib montage, no crash. `cad doctor` `render_headless_gl` reports the live path. *Env note:* the OSMesa leg needs a `vtk-osmesa` build in the CAD venv (a `cad setup` concern — the stock wheel lacks it); the Xvfb leg covers the stock wheel on a real headless server (on WSL, WSLg owns `/tmp/.X11-unix` so Xvfb can't bind there, but WSLg already provides `:0`).

**3.2 — Section/cutaway renders for internal features.** When the spec mentions internal channels/bores, add a clipping-plane render (VTK supports it) so hidden features are visible to the vision gate.
- *Accept:* a part with an internal channel shows the channel in a section view; vision gate can assess it.
- *DONE ([ADR-0009](docs/adr/0009-section-cutaway-render.md)):* `render.py --section [x|y|z]` (default auto = longest bbox axis) adds a `vtkClipPolyData` cutaway panel; `core.render(section=)`; `derive_spec` flags `internal_features` (channel/bore/cavity/hollow/passage/duct) so the agent knows to request a section. *Proven:* an internal-bore part's section montage is one panel wider (4 vs 3). The two backends were aligned to keep the same half (final-review fix).

**3.3 — OpenSCAD backend path.** Wire the optional OpenSCAD backend (AppImage `--appimage-extract` for no-root installs) for CSG-style authoring + CADAM-style parameter sliders. `templates/part.scad` exists.
- *Accept:* `cad_generate` with `backend="openscad"` produces an STL via the openscad binary; `cad doctor` reports openscad availability.
- *DONE ([ADR-0008](docs/adr/0008-openscad-backend.md)):* `core.openscad_bin()` (HERMES_OPENSCAD_BIN → PATH → AppImage-extract paths); `generate(backend="openscad")` → STL (mesh-only, no STEP), AST check skipped (SCAD≠Python) but scrubbed env + sandbox applied; clean error (never spawns) when no binary; `cad doctor` `openscad_backend`. Unit-tested mocked; real-binary e2e skips when absent.

**3.4 — Zoo/KCL organic fallback.** For freeform surfaces CadQuery/OpenSCAD choke on, add a Zoo cloud path gated on `ZOO_API_TOKEN` (surfaced at doctor-time, not install-time).
- *Accept:* with a token set, an organic-surface prompt routes to Zoo and returns a mesh; gracefully degrades with a clear message when unset.
- *DEFERRED:* lower priority per the directive; not started. No Zoo token available to verify the live path, and the three shipped backends (CadQuery primary, OpenSCAD CSG, plus the verification depth of Waves 1–2) cover the mechanical-parts target. Pick up with Wave 4 polish.

---

## Wave 4 — Polish & release (LOW)

**4.1 — `cad quickstart` + `cad repair` CLI** (activation/diagnostics pattern): prescriptive checklist + idempotent local fixes (re-provision venv, etc.). Never self-restart the gateway from inside.

**4.2 — Failure-mode guards** from the literature: Chamfer-reward exploitation in thin walls (add wall-thickness assertions to the gate vocabulary — ties to 1.1), extrusion-vs-cut confusion (volume-sign sanity), multi-part alignment (feature-placement assertions).

**4.3 — Examples gallery.** `examples/` with 5–8 worked parts (bracket, enclosure w/ lid, gear, hose-barb, mounting plate) each with prompt + code + spec + expected gate output. Doubles as integration fixtures.

**4.4 — Docs site / GIF.** A short demo of the loop catching and fixing a bad part.

---

## Pre-push smoke test (run before EVERY commit)

```bash
cd <repo>
# 1. deps install cleanly (catches packages-list drift)
~/.hermes/hermes-agent/venv/bin/python3 -m pip install -e '.[dev]' 2>&1 | tail -5
# 2. plugin contract
python3 -c "import yaml,importlib.util,sys; from pathlib import Path; \
m=yaml.safe_load(open('plugin.yaml')); assert m['name']=='hermes-text-to-cad'; \
sys.path.insert(0,'.'); s=importlib.util.spec_from_file_location('p','__init__.py'); \
mod=importlib.util.module_from_spec(s); s.loader.exec_module(mod); \
print('register ok' if callable(mod.register) else 'FAIL')"
# 3. tests
pytest tests/ -q
```

## Definition of done (whole backlog)

- All waves' acceptance criteria met and tested.
- `hermes cad doctor` green on a fresh install (numeric path) and vision-green with a key.
- README install flow verified end-to-end on a clean machine.
- CI green. Tagged release v0.2.0 (waves 1–2) / v0.3.0 (wave 3) / v1.0.0 (wave 4).
- No item deferred without a one-line justification in this doc.

---

## ADDENDUM — concurrent cross-family review findings (2026-05-31)

Three independent reviewers (Gemini 3.1 Pro / GPT-5.5 / Grok 4.3) reviewed the v0.1.0 scaffold. P0s flagged on the original `register()` (wrong PluginContext API) were verified against live Hermes source and **already fixed** in `__init__.py` (correct `register_tool(name, toolset, schema, handler, ...)`, `register_cli_command(name, help, setup_fn, handler_fn)`, relative import, `args`-dict handlers). README install order, `generate()` cwd, STL-non-empty check, and pyproject heavy-deps→extras are also fixed. Remaining items folded into the waves below:

- **[Wave 0/1.2] measure exit-code disambiguation (Gemini P1):** `measure.py` exit 1 currently means BOTH "gate failed" and could mask a real crash. Standardize: `0`=gate pass, `2`=gate fail, `1`/other=script/kernel crash (so `core.measure` can distinguish a failed gate from a segfault). Update `core.measure`'s `gate_pass`/`success` mapping and add a test.
- **[Wave 1.1 — sharpen] CADTests = EXECUTABLE assertions, not just a static spec.json (Gemini P1, convergent with the paper):** the strongest form has the LLM emit *executable Python test functions* that run against the B-rep/mesh and assert topological/dimensional facts, not only a fixed-vocabulary JSON. Support BOTH: the JSON spec for common cases AND an optional `cad_test` tool that runs LLM-authored assertion functions in the CAD venv (sandboxed — see below). This is the real CADTests signal.
- **[Wave 0/4 — SECURITY P0, Grok+Gemini convergent] `cad_generate` runs arbitrary LLM Python with full env+FS+network.** Add to the backlog as a NAMED security wave (do NOT ship to wide install without it): (a) run generate/test subprocesses with a minimal scrubbed env (drop unrelated secrets — do NOT inherit full `os.environ`), (b) add an opt-in sandbox path (bubblewrap/firejail/`-I` no-network) gated by config, (c) a code-size cap and an AST denylist for `os.system`/`socket`/`subprocess`/`open(... 'w')` outside CAD_OUT as a cheap first line. Document the threat model in SECURITY.md. **Until sandboxing lands, README must state the trust assumption clearly.**
- **[Wave 0] async dispatch (Gemini P0-ish):** wrap the long subprocess calls so a 300s generate/setup doesn't block the agent loop. Either mark the tools `is_async=True` and use `asyncio.create_subprocess_exec`, or confirm Hermes dispatches sync tool handlers on a thread (check `model_tools.handle_function_call`); add a test for whichever path.
- **[Wave 3.1] scrubbed-env + no-network for render/measure too**, not just generate.
- **[Wave 4] profile-aware `~/.hermes/.env` resolution (GPT P2):** `_has_openrouter_key` and `scatter_review.py` hard-code `~/.hermes/.env`; honor the active Hermes profile's env path.
- **[Wave 4] Windows venv layout (GPT P2):** `core.setup`/`cad_venv_python` hardcode POSIX `bin/python`; either handle `Scripts\python.exe` or drop `windows` from `plugin.yaml platforms`.
- **[Wave 0] error-swallowing (Grok P1):** broad `except` in `measure`/`render` parsing must log at WARNING with context, never hide a crash as truncated raw output (per the plugin-authoring `logger.debug`-hides-bugs rule).

**Three things the reviewers confirmed RIGHT:** (1) the venv-subprocess isolation of the CAD stack, (2) the dual-gate closed loop (numeric precision + vision intent), (3) `requires_env: []` + doctor-time credential surfacing for graceful UX.
