# 4. Headless render precedence: display → OSMesa/EGL → Xvfb → matplotlib

- Status: accepted
- Date: 2026-05-31
- Deciders: autonomous deep-work loop (Wave 3.1)

## Context and Problem Statement

The vision gate needs a real z-buffered render. Today render.py tries VTK via a
display and falls back to matplotlib. But probing the stock `vtk` 9.3.1 wheel
showed it ships **only** `vtkXOpenGLRenderWindow` (GLX/X11) — no
`vtkOSOpenGLRenderWindow` (OSMesa), no `vtkEGLRenderWindow`. With `DISPLAY`
unset, `rw.Render()` **SIGABRTs the process** (uncatchable C++ abort, exit 134),
so the matplotlib fallback is *unreachable* on a truly headless box. We need a
headless path that produces a real VTK render, and we must never let an
uncatchable abort kill render.py.

## Considered Options

1. **In-process OSMesa/EGL VTK** — requires a `vtk-osmesa` build in the venv.
2. **Auto-launch a private Xvfb** + render through GLX with Mesa `swrast`
   software GL (both present on the box), then tear down.
3. **Probe GL in a sub-subprocess first**, then pick the path that won't abort.
4. **Keep matplotlib-only headless** — no real occlusion.

## Decision Outcome

Chosen: a **precedence chain** combining 1–3, never 4 blindly:

1. `DISPLAY` already set (WSLg `:0`, real X) → VTK GLX (current behavior).
2. No display, but an OSMesa/EGL-capable VTK is importable (class present) →
   in-process software window. This is the "real" Wave 3.1 path when the venv has
   `vtk-osmesa`; it needs no Xvfb.
3. No display, X11-only VTK, but `Xvfb` on PATH → auto-launch a private Xvfb on a
   free display, set `DISPLAY`, render via GLX (Mesa swrast), tear Xvfb down.
4. None of the above → matplotlib Agg (lower fidelity, last resort).

**Crucial safety rule:** because a wrong choice SIGABRTs, render.py determines
VTK GL viability **without** risking the main process — it runs the actual VTK
render in a **child subprocess** (the existing `_render_vtk` already runs inside
the `render.py` process which is itself a subprocess of core; we add an internal
guard so a GL abort is contained and reported as a clean fallback, not a crash of
the montage step). The display-selection logic chooses Xvfb/OSMesa *before*
touching GL.

`cad doctor` gains a `render_headless_gl` check reporting which path is available
(`display` / `osmesa` / `xvfb` / `matplotlib-only`).

### Consequences

- Good: acceptance criterion met — DISPLAY unset + no WSLg socket yields a real
  z-buffered VTK montage via OSMesa (if the venv has it) or Xvfb (stock wheel).
- Good: an uncatchable GL abort degrades to matplotlib instead of killing render.
- Bad: the Xvfb path spawns/reaps a process per render on stock-wheel headless
  boxes (small cost; cached display reused within one render run).
- WSL caveat: on this dev box WSLg owns `/tmp/.X11-unix`, so a second Xvfb can't
  bind there — but WSLg already provides `:0`, so path (1) is used. The Xvfb path
  is for real headless servers where that dir is writable. Documented in doctor.
