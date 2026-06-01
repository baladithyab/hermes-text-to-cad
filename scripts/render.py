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

def _render_vtk(stl, outdir, stem, section_axis=None):
    import vtk
    reader = vtk.vtkSTLReader(); reader.SetFileName(stl); reader.Update()
    poly = reader.GetOutput()
    b = poly.GetBounds()
    ctr = ((b[0]+b[1])/2, (b[2]+b[3])/2, (b[4]+b[5])/2)
    diag = math.sqrt((b[1]-b[0])**2 + (b[3]-b[2])**2 + (b[5]-b[4])**2) or 1.0
    d = diag * 1.6

    mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(reader.GetOutputPort())
    actor = vtk.vtkActor(); actor.SetMapper(mapper)
    p = actor.GetProperty()
    p.SetColor(0.48, 0.65, 0.84)
    p.SetEdgeVisibility(1); p.SetEdgeColor(0.16, 0.29, 0.42); p.SetLineWidth(0.6)
    p.BackfaceCullingOn()

    ren = vtk.vtkRenderer(); ren.SetBackground(1, 1, 1); ren.AddActor(actor)
    ren.AutomaticLightCreationOn()
    rw = vtk.vtkRenderWindow(); rw.SetOffScreenRendering(1); rw.AddRenderer(ren); rw.SetSize(600, 600)

    cams = {
        "front": ((0, -d, d*0.12), (0, 0, 1)),
        "top":   ((d*0.25, -d*0.25, d), (0, 1, 0)),
        "iso":   ((d*0.7, -d*0.7, d*0.6), (0, 0, 1)),
    }
    written = []
    for name, (off, up) in cams.items():
        cam = ren.GetActiveCamera()
        cam.SetFocalPoint(*ctr)
        cam.SetPosition(ctr[0]+off[0], ctr[1]+off[1], ctr[2]+off[2])
        cam.SetViewUp(*up)
        ren.ResetCamera()                 # auto-fit so nothing clips off-frame
        cam.Zoom(1.1)
        ren.ResetCameraClippingRange()
        rw.Render()
        w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(rw); w2i.Update()
        out = os.path.join(outdir, f"{stem}_{name}.png")
        wr = vtk.vtkPNGWriter(); wr.SetFileName(out); wr.SetInputConnection(w2i.GetOutputPort()); wr.Write()
        written.append(out)

    if section_axis:
        written.append(_render_vtk_section(reader, ctr, d, outdir, stem, section_axis))
    return written


def _render_vtk_section(reader, ctr, d, outdir, stem, axis):
    """A clipping-plane cutaway iso view that reveals internal features (ADR-0009).
    Clips the solid through its centroid along `axis` so hidden bores/channels
    become visible to the vision gate."""
    import vtk
    normals = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}
    n = normals.get(axis, (1, 0, 0))
    plane = vtk.vtkPlane(); plane.SetOrigin(*ctr); plane.SetNormal(*n)
    clipper = vtk.vtkClipPolyData(); clipper.SetInputConnection(reader.GetOutputPort())
    clipper.SetClipFunction(plane); clipper.SetValue(0); clipper.Update()

    mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(clipper.GetOutputPort())
    actor = vtk.vtkActor(); actor.SetMapper(mapper)
    pr = actor.GetProperty()
    pr.SetColor(0.84, 0.55, 0.48)   # warm tone to distinguish the cut
    pr.SetEdgeVisibility(1); pr.SetEdgeColor(0.42, 0.20, 0.16); pr.SetLineWidth(0.6)

    ren = vtk.vtkRenderer(); ren.SetBackground(1, 1, 1); ren.AddActor(actor)
    ren.AutomaticLightCreationOn()
    rw = vtk.vtkRenderWindow(); rw.SetOffScreenRendering(1); rw.AddRenderer(ren); rw.SetSize(600, 600)
    cam = ren.GetActiveCamera()
    cam.SetFocalPoint(*ctr)
    cam.SetPosition(ctr[0]+d*0.7, ctr[1]-d*0.7, ctr[2]+d*0.6)
    cam.SetViewUp(0, 0, 1)
    ren.ResetCamera(); cam.Zoom(1.1); ren.ResetCameraClippingRange()
    rw.Render()
    w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(rw); w2i.Update()
    out = os.path.join(outdir, f"{stem}_section.png")
    wr = vtk.vtkPNGWriter(); wr.SetFileName(out); wr.SetInputConnection(w2i.GetOutputPort()); wr.Write()
    return out


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

    if not args:
        print("usage: render.py PART.stl [OUT_MONTAGE.png] [--section [x|y|z]]", file=sys.stderr)
        sys.exit(2)
    stl = args[0]
    outdir = os.path.dirname(os.path.abspath(stl))
    stem = os.path.splitext(os.path.basename(stl))[0]
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
                backend = "vtk-osmesa" if strategy == "osmesa" else "vtk"
            else:
                print("[render] GL probe failed; falling back to matplotlib", file=sys.stderr)
                paths = _render_matplotlib(stl, outdir, stem, section_axis=section_axis)
                backend = "matplotlib"
        else:
            paths = _render_matplotlib(stl, outdir, stem, section_axis=section_axis)
            backend = "matplotlib"
    except Exception as e:
        print(f"[render] {backend} failed ({e}); falling back to matplotlib", file=sys.stderr)
        paths = _render_matplotlib(stl, outdir, stem, section_axis=section_axis)
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
    print(f"[render] backend={backend} views={len(paths)}{sect}", file=sys.stderr)
    print(mp)  # stdout last line = montage path


if __name__ == "__main__":
    main()
