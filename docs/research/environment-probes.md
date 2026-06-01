# Environment probes — Waves 2/3/SECURITY (2026-05-31)

Empirical findings that ground the wave designs. Run on the dev box
(WSL2 + WSLg, `~/.venvs/cad` = uv venv, Python 3.11, VTK 9.3.1).

## SECURITY wave — env inheritance is the hole (CONFIRMED)

`core._run()` builds `env = dict(os.environ)`; `core.generate()` builds
`env = dict(os.environ, CAD_OUT=...)`. Both inherit the **full** parent
environment into the subprocess — including `OPENROUTER_API_KEY` and every other
secret in the Hermes process. The venv-subprocess design isolated the heavy CAD
*stack* (imports) but NOT the *secrets*. `cad_generate` runs model-authored
Python with that full env → a generated `print(os.environ)` exfiltrates the key.

### bubblewrap is available and works (CONFIRMED)

- `bwrap` 0.11.2 at `/home/linuxbrew/.linuxbrew/bin/bwrap`. No `firejail`.
- Verified one invocation does all of: `--unshare-net` (network blocked —
  `socket.create_connection` raises), `--ro-bind / /` (root read-only —
  `touch /etc/evil` → "Read-only file system"), `--bind CAD_OUT CAD_OUT`
  (writable), `--tmpfs $HOME/.hermes` (masks the dir holding `.env` inside the
  sandbox while the host copy stays intact).
- Conclusion: an opt-in `HERMES_CAD_SANDBOX=1` bwrap wrapper is viable and
  degrades cleanly (detect `shutil.which("bwrap")`; warn + run unsandboxed if
  absent). firejail is the documented fallback but not installable here.

## WAVE 3.1 — headless rendering: stock pip VTK is X11-only (CRITICAL)

The stock `vtk` 9.3.1 PyPI wheel ships **only** `vtkXOpenGLRenderWindow`
(GLX/X11). It has **no** `vtkOSOpenGLRenderWindow` (OSMesa) and **no**
`vtkEGLRenderWindow` class:

```
vtkOSOpenGLRenderWindow -> False
vtkEGLRenderWindow      -> False
vtkXOpenGLRenderWindow  -> True
```

With `DISPLAY` unset, `rw.Render()` does NOT raise a catchable exception — it
**SIGABRTs the whole process** ("bad X server connection. DISPLAY=. Aborting.",
exit 134). This is a C++ `abort()`, so render.py's `try/except → matplotlib`
fallback is **unreachable** on a truly headless box: the interpreter dies before
the except runs.

Implications for the OSMesa/EGL ask:

- A true in-process OSMesa/EGL software-GL path requires a **different VTK
  build** (`vtk-osmesa` wheel, or a source build with `VTK_OPENGL_HAS_OSMESA`).
  The minimal `~/.venvs/cad` does not have it, and the venv is created by `uv`
  with **no pip** inside it — swapping the wheel is a `cad setup` concern, and
  modifying that shared venv is out of scope for this repo's code.
- The portable, dependency-light headless path that works with the *existing*
  wheel is **Xvfb** (virtual framebuffer) + Mesa `swrast` software GL, both
  present here (`/usr/bin/Xvfb`, `/usr/lib/x86_64-linux-gnu/dri/swrast_dri.so`).
  render.py can auto-launch a private Xvfb, point `DISPLAY` at it, render via
  the existing GLX window with software GL, and tear it down.
- **WSL caveat:** on THIS box Xvfb can't bind because WSLg owns
  `/tmp/.X11-unix` and a second server fails to create a listener there (`-listen
  tcp -nolisten unix` also aborts under WSLg's nvidia-egl-gbm). That's a
  WSL-specific conflict, not a headless-server problem — on a real headless box
  `/tmp/.X11-unix` is writable and Xvfb starts normally. On WSL we already have
  WSLg's `:0`, so the Xvfb path is for the headless-server case the acceptance
  criterion targets.

### Design decision for 3.1

Add a **dedicated software-GL render path** with this precedence in render.py:
1. An already-set `DISPLAY` (WSLg `:0`, a real X session) → use it (current
   behavior).
2. No display, but an OSMesa/EGL-capable VTK build is importable → use the
   in-process software window (`vtkOSOpenGLRenderWindow` / `SetOffScreenRendering`
   picks it). This is the "real" Wave 3.1 path when the venv has `vtk-osmesa`.
3. No display and X11-only VTK, but `Xvfb` on PATH → auto-launch a private Xvfb,
   render through it, tear down. Portable software-GL with the stock wheel.
4. None of the above → matplotlib Agg (current fallback), but only reached if we
   can PROVE VTK would abort — so we must **probe for a usable GL context in a
   sub-subprocess** (which can SIGABRT harmlessly) before ever calling
   `rw.Render()` in-process. Never let an uncatchable abort kill render.py.

The acceptance criterion ("DISPLAY unset, no WSLg socket → z-buffered VTK
montage, not matplotlib") is met by paths (2) or (3). `cad doctor` must report
which headless GL path is available.

## WAVE 2 — vision gate, Chamfer, plan: no new deps needed

- Structured-Q&A (2.1): pure prompt/parse change in scatter_review.py +
  core.review parsing. No new dep.
- Chamfer (2.2): trimesh already in the CAD venv. `trimesh.sample.sample_surface`
  + scipy `cKDTree` (scipy present) → symmetric CD. No new dep.
- CoT plan (2.3): pure stdlib JSON in core. No new dep.

## Versioning note

Bump to 0.2.0 (waves 1–2) then 0.3.0 (wave 3) per PLAN's definition-of-done.
Current files say 0.1.0 in pyproject.toml, plugin.yaml, README.
