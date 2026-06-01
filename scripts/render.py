#!/usr/bin/env python3
"""render.py — headless multi-view PNG render of a CAD mesh, for the vision-review gate.

Proven on WSL+WSLg (2026-05-31): VTK offscreen via the WSLg X display (:0) gives
real z-buffered occlusion. If no display is reachable, falls back to matplotlib Agg
(pure software, lower quality but never fails).

Usage:
    DISPLAY=:0 python render.py PART.stl [OUT_MONTAGE.png]
    python render.py PART.stl              # auto-tries :0 then falls back

Outputs: <part>_front.png, <part>_top.png, <part>_iso.png, and a montage.
Prints the montage path on the last line (machine-readable).
"""
import os, sys, math


def _render_vtk(stl, outdir, stem):
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
    return written


def _render_matplotlib(stl, outdir, stem):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import trimesh
    m = trimesh.load(stl)
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
    return written


def _montage(paths, out):
    from PIL import Image
    imgs = [Image.open(p) for p in paths]
    W = sum(i.width for i in imgs); H = max(i.height for i in imgs)
    canvas = Image.new("RGB", (W, H), "white"); x = 0
    for i in imgs:
        canvas.paste(i, (x, 0)); x += i.width
    canvas.save(out)
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: render.py PART.stl [OUT_MONTAGE.png]", file=sys.stderr); sys.exit(2)
    stl = sys.argv[1]
    outdir = os.path.dirname(os.path.abspath(stl))
    stem = os.path.splitext(os.path.basename(stl))[0]
    montage_out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(outdir, f"{stem}_montage.png")

    # Try VTK with the WSLg display if DISPLAY is unset.
    if not os.environ.get("DISPLAY") and os.path.exists("/tmp/.X11-unix/X0"):
        os.environ["DISPLAY"] = ":0"
    backend = "vtk"
    try:
        paths = _render_vtk(stl, outdir, stem)
    except Exception as e:
        print(f"[render] VTK failed ({e}); falling back to matplotlib", file=sys.stderr)
        paths = _render_matplotlib(stl, outdir, stem)
        backend = "matplotlib"
    mp = _montage(paths, montage_out)
    print(f"[render] backend={backend} views={len(paths)}", file=sys.stderr)
    print(mp)  # stdout last line = montage path


if __name__ == "__main__":
    main()