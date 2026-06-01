#!/usr/bin/env python3
"""render.py — headless multi-view PNG render of a CAD mesh, for the vision gate.

Render-backend precedence (ADR-0004):
  1. an already-set DISPLAY (WSLg :0, a real X session) -> VTK GLX
  2. no display + an OSMesa/EGL-capable VTK build         -> in-process software GL
  3. no display + X11-only VTK + Xvfb on PATH             -> private Xvfb + GLX swrast
  4. none of the above                                    -> matplotlib Agg (last resort)

CRITICAL: the stock pip VTK wheel ships ONLY vtkXOpenGLRenderWindow; with no
reachable display rw.Render() SIGABRTs the WHOLE process (uncatchable C++
abort) — so we NEVER call it in this process until we've confirmed a usable GL
context. The confirmation runs in a disposable CHILD subprocess (`--gl-probe`)
that may abort harmlessly; only then does the parent render in-process.

Usage:
    DISPLAY=:0 python render.py PART.stl [OUT_MONTAGE.png] [--section [axis]]
    python render.py PART.stl                 # auto-detects the best backend
    python render.py PART.stl --gl-probe      # internal: exit 0 iff GL works

Outputs: <part>_front.png, <part>_top.png, <part>_iso.png (+ _section), montage.
Prints the montage path on the last stdout line (machine-readable); the chosen
backend is logged to stderr as `backend=<name>`.
"""
import os
import sys
import math
import shutil
import subprocess

# Ensure the sibling pbr.py is importable however render.py is invoked
# (as a script, sys.path[0] is already this dir; belt-and-suspenders for the
# venv-gated integration loader and the --gl-probe child).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


# ---- GL context probing (crash-safe) ----------------------------------------

def _osmesa_capable():
    """True if the installed VTK exposes an OSMesa/EGL offscreen render window
    class (i.e. a software-GL build that needs no X display)."""
    try:
        import vtk
    except Exception:
        return False
    return any(hasattr(vtk, c) for c in
               ("vtkOSOpenGLRenderWindow", "vtkEGLRenderWindow"))


def _gl_probe():
    """Tiny in-process VTK render. Exit 0 on success, non-zero on a caught
    error. May SIGABRT — that's WHY this runs as a child subprocess: the abort
    kills only the probe, and the parent reads the (non-zero) exit code."""
    try:
        import vtk
        ren = vtk.vtkRenderer()
        src = vtk.vtkConeSource(); src.Update()
        mp = vtk.vtkPolyDataMapper(); mp.SetInputConnection(src.GetOutputPort())
        ac = vtk.vtkActor(); ac.SetMapper(mp); ren.AddActor(ac)
        rw = vtk.vtkRenderWindow(); rw.SetOffScreenRendering(1)
        rw.AddRenderer(ren); rw.SetSize(32, 32)
        rw.Render()
        return 0
    except Exception:
        return 1


def _gl_works(env):
    """Run the GL probe in a child subprocess under `env`. True iff it exits 0.
    Crash-safe: a SIGABRT in the child surfaces as a non-zero returncode here."""
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--gl-probe"],
                           env=env, capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


# ---- Xvfb (virtual framebuffer) for headless software GL ---------------------

def _find_free_display():
    """Pick a display number whose X11 socket file is free."""
    for n in range(80, 120):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return n
    return None


def _start_xvfb(disp_num):
    """Launch a private Xvfb on :disp_num with GLX. Returns the Popen, or None if
    it didn't come up (e.g. WSL, where WSLg owns /tmp/.X11-unix)."""
    try:
        proc = subprocess.Popen(
            ["Xvfb", f":{disp_num}", "-screen", "0", "1024x768x24",
             "+extension", "GLX", "+render", "-noreset"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    sock = f"/tmp/.X11-unix/X{disp_num}"
    # wait up to ~3s for the socket; if the process dies, bail
    import time
    for _ in range(60):
        if proc.poll() is not None:
            return None
        if os.path.exists(sock):
            return proc
        time.sleep(0.05)
    if not os.path.exists(sock):
        proc.terminate()
        return None
    return proc


# ---- VTK / matplotlib renderers ----------------------------------------------

def _load_into_renderer(src, renderer, render_window):
    """Load a mesh source into a renderer as PBR actors (ADR-0010).

    A .glb is imported with vtkGLTFImporter — one actor PER BODY, each keeping its
    glTF base color + metallic/roughness, so colored multi-body assemblies render
    faithfully. Any other format (.stl/.obj/.ply/.3mf) loads as a single body with
    a synthesized neutral studio material. Returns (is_glb, n_actors)."""
    import vtk
    ext = os.path.splitext(src)[1].lower()
    if ext == ".glb" or ext == ".gltf":
        imp = vtk.vtkGLTFImporter()
        imp.SetFileName(src)
        imp.SetRenderWindow(render_window)
        imp.Update()
        from pbr import apply_pbr_to_actors
        n = apply_pbr_to_actors(renderer, synth=False)
        # vtkGLTFImporter logs an error but does NOT raise on a corrupt/truncated
        # file — it just yields zero actors, which would otherwise render three
        # BLANK montage panels the vision gate would trust. Raise so the caller's
        # try/except falls back to matplotlib (or the STL sibling).
        if n == 0:
            raise RuntimeError(f"GLTF import produced no actors from {src!r} "
                               "(corrupt/unsupported .glb?)")
        return True, n
    # mesh formats VTK can read directly; STL is the common case
    reader = vtk.vtkSTLReader() if ext == ".stl" else None
    if reader is None:
        # generic fallback via trimesh -> vtkPolyData
        import trimesh
        m = trimesh.load(src, force="mesh")
        poly = _trimesh_to_polydata(m)
        mapper = vtk.vtkPolyDataMapper(); mapper.SetInputData(poly)
    else:
        reader.SetFileName(src); reader.Update()
        mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(reader.GetOutputPort())
    actor = vtk.vtkActor(); actor.SetMapper(mapper)
    renderer.AddActor(actor)
    from pbr import apply_pbr_to_actors
    apply_pbr_to_actors(renderer, synth=True)
    return False, 1


def _trimesh_to_polydata(m):
    import vtk
    import numpy as np
    pts = vtk.vtkPoints()
    for v in m.vertices:
        pts.InsertNextPoint(float(v[0]), float(v[1]), float(v[2]))
    cells = vtk.vtkCellArray()
    for f in m.faces:
        cells.InsertNextCell(3)
        for idx in f:
            cells.InsertCellPoint(int(idx))
    poly = vtk.vtkPolyData(); poly.SetPoints(pts); poly.SetPolys(cells)
    nrm = vtk.vtkPolyDataNormals(); nrm.SetInputData(poly); nrm.SplittingOff(); nrm.Update()
    return nrm.GetOutput()


def _render_vtk(stl, outdir, stem, section_axis=None):
    """PBR studio render of a mesh into a 3-view montage (+ optional section).

    `stl` may be a .glb (per-body color preserved) or an STL/other mesh. Uses the
    pbr module's image-based lighting + 3-point studio rig + soft shadows + filmic
    tone mapping (ADR-0010). Studio framing uses ResetCamera (auto-fit) + a small
    zoom; the seven conventional view directions are reused from the reference."""
    import vtk
    from pbr import apply_studio

    rw = vtk.vtkRenderWindow()
    rw.SetOffScreenRendering(1)
    rw.SetMultiSamples(0)   # MUST be 0 with shadow passes (ADR-0010)
    rw.SetSize(640, 640)
    ren = vtk.vtkRenderer()
    rw.AddRenderer(ren)

    is_glb, n = _load_into_renderer(stl, ren, rw)

    b = ren.ComputeVisiblePropBounds()
    ctr = ((b[0]+b[1])/2, (b[2]+b[3])/2, (b[4]+b[5])/2)
    apply_studio(ren, ctr, b)

    # Three conventional views. Each = (view direction from center, view-up).
    # The "top" view-up is +Y (so looking down -Z reads naturally); front/iso use
    # +Z up. Directions are kept clear of their view-up to avoid the degenerate
    # parallel-normal case.
    views = {
        "front": ((0.0, -1.0, 0.05), (0, 0, 1)),
        "top":   ((0.0, 0.0, 1.0), (0, 1, 0)),
        "iso":   ((0.7, -0.7, 0.55), (0, 0, 1)),
    }
    written = []
    for name, (dir3, up) in views.items():
        _frame_camera(ren, ctr, dir3, up)
        rw.Render()
        written.append(_grab(rw, outdir, f"{stem}_{name}"))

    if section_axis:
        written.append(_render_vtk_section(stl, outdir, stem, section_axis))
    return written


def _frame_camera(renderer, center, direction, up, zoom=1.05):
    """Point the camera along `direction` toward `center` with `up`, then auto-fit.

    Establishes the projection direction + view-up FIRST, then a single
    ResetCamera() (which preserves direction/up and only sets the distance to fit
    the bounds) — the robust VTK idiom that avoids the double-reset framing bug and
    the parallel view-up degeneracy. zoom>1 tightens the 12%-style padding."""
    cam = renderer.GetActiveCamera()
    cam.SetFocalPoint(*center)
    cam.SetViewUp(*up)
    # any nonzero standoff along the direction establishes the projection vector;
    # ResetCamera then corrects the distance to fit.
    cam.SetPosition(center[0] + direction[0], center[1] + direction[1],
                    center[2] + direction[2])
    renderer.ResetCamera()
    cam.Zoom(zoom)
    renderer.ResetCameraClippingRange()


def _grab(render_window, outdir, name):
    import vtk
    w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(render_window); w2i.Update()
    out = os.path.join(outdir, f"{name}.png")
    wr = vtk.vtkPNGWriter(); wr.SetFileName(out); wr.SetInputConnection(w2i.GetOutputPort()); wr.Write()
    return out


def _render_vtk_section(stl, outdir, stem, axis):
    """A clipping-plane cutaway iso view that reveals internal features (ADR-0009),
    PBR-shaded and color-preserving (each body keeps its material; ADR-0010)."""
    import vtk
    from pbr import apply_studio, clip_actors

    rw = vtk.vtkRenderWindow()
    rw.SetOffScreenRendering(1); rw.SetMultiSamples(0); rw.SetSize(640, 640)
    ren = vtk.vtkRenderer(); rw.AddRenderer(ren)
    _load_into_renderer(stl, ren, rw)

    b = ren.ComputeVisiblePropBounds()
    ctr = ((b[0]+b[1])/2, (b[2]+b[3])/2, (b[4]+b[5])/2)
    # Clip BEFORE studio so the cut surfaces are lit. Keeps the +normal half to
    # match the matplotlib section (final-review parity, ADR-0009).
    clip_actors(ren, ctr, axis)
    apply_studio(ren, ctr, b)

    _frame_camera(ren, ctr, (0.7, -0.7, 0.55), (0, 0, 1))
    rw.Render()
    return _grab(rw, outdir, f"{stem}_section")


def _render_matplotlib(stl, outdir, stem, section_axis=None):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import numpy as np
    import trimesh
    m = trimesh.load(stl, force="mesh")
    tris = m.vertices[m.faces]
    mins, maxs = m.vertices.min(0), m.vertices.max(0)
    ctr = (mins + maxs) / 2; r = (maxs - mins).max() / 2 * 1.1
    written = []
    for name, elev, azim in [("front", 5, -90), ("top", 75, -90), ("iso", 30, 45)]:
        fig = plt.figure(figsize=(6, 6)); ax = fig.add_subplot(111, projection="3d")
        ax.add_collection3d(Poly3DCollection(tris, alpha=0.9, facecolor="#7aa6d6",
                                             edgecolor="#2a4a6a", linewidths=0.15))
        ax.set_xlim(ctr[0]-r, ctr[0]+r); ax.set_ylim(ctr[1]-r, ctr[1]+r); ax.set_zlim(ctr[2]-r, ctr[2]+r)
        ax.view_init(elev=elev, azim=azim); ax.set_box_aspect([1, 1, 1]); ax.grid(alpha=0.2)
        out = os.path.join(outdir, f"{stem}_{name}.png")
        fig.savefig(out, dpi=90, bbox_inches="tight"); plt.close(fig)
        written.append(out)
    if section_axis:
        written.append(_render_matplotlib_section(m, ctr, r, outdir, stem, section_axis))
    return written


def _render_matplotlib_section(m, ctr, r, outdir, stem, axis):
    """Coarse matplotlib section: keep only the half of the mesh on one side of
    the centroid plane, so a headless-without-GL render still shows the cavity."""
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import numpy as np
    ai = {"x": 0, "y": 1, "z": 2}.get(axis, 0)
    face_ctr = m.vertices[m.faces].mean(axis=1)        # centroid of each tri
    # Keep the POSITIVE half (coord >= centroid) to MATCH the VTK clip plane,
    # which uses vtkClipPolyData with InsideOut off and so retains value>0 (the
    # +normal side). Keeping opposite halves would show a feature in one backend
    # and hide it in the other (final-review finding).
    keep = face_ctr[:, ai] >= ctr[ai]                  # positive half — matches VTK
    tris = m.vertices[m.faces][keep]
    fig = plt.figure(figsize=(6, 6)); ax = fig.add_subplot(111, projection="3d")
    if len(tris):
        ax.add_collection3d(Poly3DCollection(tris, alpha=0.9, facecolor="#d68f7a",
                                             edgecolor="#6a342a", linewidths=0.15))
    ax.set_xlim(ctr[0]-r, ctr[0]+r); ax.set_ylim(ctr[1]-r, ctr[1]+r); ax.set_zlim(ctr[2]-r, ctr[2]+r)
    ax.view_init(elev=30, azim=45); ax.set_box_aspect([1, 1, 1]); ax.grid(alpha=0.2)
    out = os.path.join(outdir, f"{stem}_section.png")
    fig.savefig(out, dpi=90, bbox_inches="tight"); plt.close(fig)
    return out


def _montage(paths, out):
    from PIL import Image
    imgs = [Image.open(p) for p in paths]
    W = sum(i.width for i in imgs); H = max(i.height for i in imgs)
    canvas = Image.new("RGB", (W, H), "white"); x = 0
    for i in imgs:
        canvas.paste(i, (x, 0)); x += i.width
    canvas.save(out)
    return out


# ---- backend selection (pure, testable) --------------------------------------

def choose_backend(has_display, osmesa_ok, xvfb_path, x11_dir_writable):
    """Decide the render strategy from environment facts (ADR-0004 precedence).

    Returns one of: 'display', 'osmesa', 'xvfb', 'matplotlib'. Pure — no I/O — so
    the precedence logic is unit-testable without a GPU/display.
    """
    if has_display:
        return "display"
    if osmesa_ok:
        return "osmesa"
    if xvfb_path and x11_dir_writable:
        return "xvfb"
    return "matplotlib"


def _x11_dir_writable():
    """Can we create a new X11 socket? False on WSL (WSLg owns /tmp/.X11-unix)."""
    d = "/tmp/.X11-unix"
    return os.path.isdir(d) and os.access(d, os.W_OK) and _find_free_display() is not None


def resolve_render_source(path, glb_exists):
    """Prefer a sibling .glb over the given mesh path (ADR-0010).

    A colored multi-body GLB renders faithfully (per-body color) whereas an STL is
    a single mono-color body. If the caller passes part.stl and part.glb exists
    beside it, render the GLB. Pass a .glb directly to use it as-is. Pure (no I/O):
    glb_exists is the precomputed existence of the sibling, so this is testable
    offline. Returns the path to actually render.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        return path
    sibling = os.path.splitext(path)[0] + ".glb"
    if glb_exists:
        return sibling
    return path


# ---- main --------------------------------------------------------------------

def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--gl-probe":
        sys.exit(_gl_probe())

    args = [a for a in sys.argv[1:]]
    section_axis = None
    if "--section" in args:
        i = args.index("--section")
        # optional axis token after --section
        if i + 1 < len(args) and args[i + 1] in ("x", "y", "z"):
            section_axis = args[i + 1]
            del args[i:i + 2]
        else:
            section_axis = "auto"
            del args[i]

    # --no-glb: render the given mesh as-is, never prefer a sibling .glb (lets a
    # caller force the STL path, e.g. for a numeric-only check).
    prefer_glb = True
    if "--no-glb" in args:
        prefer_glb = False
        args.remove("--no-glb")

    if not args:
        print("usage: render.py PART.stl [OUT_MONTAGE.png] [--section [x|y|z]] [--no-glb]", file=sys.stderr)
        sys.exit(2)
    given = args[0]
    # GLB-preferred input (ADR-0010): a sibling part.glb renders with per-body
    # color; the given STL is the fallback. Stem/montage name track the GIVEN path
    # so output naming is stable regardless of which source was rendered.
    sibling_glb = os.path.splitext(given)[0] + ".glb"
    glb_exists = prefer_glb and os.path.exists(sibling_glb)
    stl = resolve_render_source(given, glb_exists)
    outdir = os.path.dirname(os.path.abspath(given))
    stem = os.path.splitext(os.path.basename(given))[0]
    montage_out = args[1] if len(args) > 1 else os.path.join(outdir, f"{stem}_montage.png")

    # resolve "auto" section axis to the longest bbox axis (where through-features
    # usually run) — needs trimesh, done lazily and tolerant of failure.
    if section_axis == "auto":
        try:
            import trimesh
            ext = trimesh.load(stl, force="mesh").bounding_box.extents
            section_axis = ["x", "y", "z"][int(max(range(3), key=lambda i: ext[i]))]
        except Exception:
            section_axis = "x"

    # WSLg: adopt the :0 display if present and DISPLAY unset.
    if not os.environ.get("DISPLAY") and os.path.exists("/tmp/.X11-unix/X0"):
        os.environ["DISPLAY"] = ":0"

    has_display = bool(os.environ.get("DISPLAY"))
    osmesa_ok = _osmesa_capable()
    xvfb_path = shutil.which("Xvfb")
    strategy = choose_backend(has_display, osmesa_ok, xvfb_path, _x11_dir_writable())

    xvfb_proc = None
    backend = "vtk"
    paths = None
    rendered_src = stl   # the source actually rendered (may change on fallback)
    try:
        if strategy == "xvfb":
            disp = _find_free_display()
            xvfb_proc = _start_xvfb(disp) if disp is not None else None
            if xvfb_proc is not None:
                os.environ["DISPLAY"] = f":{disp}"
                os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
                has_display = True
                strategy = "display"
            else:
                strategy = "matplotlib"   # Xvfb refused to start (e.g. WSL)

        if strategy in ("display", "osmesa"):
            # Crash-safe gate: confirm GL actually works in a child before we
            # call rw.Render() in-process (stock X11-only VTK SIGABRTs otherwise).
            if _gl_works(dict(os.environ)):
                paths = _render_vtk(stl, outdir, stem, section_axis=section_axis)
                # PBR studio path (ADR-0010); -glb suffix when per-body color came
                # from a GLB so the backend label tells the gate what it's seeing.
                base = "vtk-osmesa-pbr" if strategy == "osmesa" else "vtk-pbr"
                backend = base + ("-glb" if os.path.splitext(stl)[1].lower() in (".glb", ".gltf") else "")
            else:
                print("[render] GL probe failed; falling back to matplotlib", file=sys.stderr)
                paths = _render_matplotlib(stl, outdir, stem, section_axis=section_axis)
                backend = "matplotlib"
        else:
            paths = _render_matplotlib(stl, outdir, stem, section_axis=section_axis)
            backend = "matplotlib"
    except Exception as e:
        print(f"[render] {backend} failed ({e}); falling back to matplotlib", file=sys.stderr)
        # If we'd PREFERRED a sibling .glb and it's the thing that failed (e.g. a
        # corrupt GLB), retry the fallback on the ORIGINAL given mesh — the valid
        # STL — rather than re-feeding the bad GLB to matplotlib (which would also
        # crash). This makes a bad sibling .glb degrade to the STL, not to nothing.
        fallback_src = given if (stl != given and not os.path.splitext(given)[1].lower()
                                 in (".glb", ".gltf")) else stl
        try:
            paths = _render_matplotlib(fallback_src, outdir, stem, section_axis=section_axis)
        except Exception as e2:
            print(f"[render] matplotlib on {os.path.basename(fallback_src)} also "
                  f"failed ({e2})", file=sys.stderr)
            raise
        rendered_src = fallback_src
        backend = "matplotlib"
    finally:
        if xvfb_proc is not None:
            xvfb_proc.terminate()
            try:
                xvfb_proc.wait(timeout=5)
            except Exception:
                xvfb_proc.kill()

    mp = _montage(paths, montage_out)
    sect = f" section={section_axis}" if section_axis else ""
    # report the source ACTUALLY rendered (differs from `given` when a sibling
    # .glb was preferred, or when a bad .glb degraded back to the given STL).
    src = f" src={os.path.basename(rendered_src)}" if rendered_src != given else ""
    print(f"[render] backend={backend} views={len(paths)}{sect}{src}", file=sys.stderr)
    print(mp)  # stdout last line = montage path


if __name__ == "__main__":
    main()
