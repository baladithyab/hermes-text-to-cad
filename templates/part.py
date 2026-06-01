"""CadQuery parametric part skeleton — text-to-cad skill.
Run with: ~/.venvs/cad/bin/python part.py
Edit the PARAMS block to iterate. Exports STL (print/gate) + STEP (manufacturing).
"""
import cadquery as cq
import os

OUTDIR = os.environ.get("CAD_OUT", ".")
STEM = "part"

# ============================ PARAMS (edit these) ============================
LENGTH = 40.0   # X (mm)
WIDTH  = 30.0   # Y (mm)
HEIGHT = 20.0   # Z (mm)
WALL   = 2.0    # shell thickness (mm)
HOLE_D = 8.0    # top hole diameter (mm)
# =============================================================================

# ============================ MODEL (edit this) ==============================
part = (
    cq.Workplane("XY")
    .box(LENGTH, WIDTH, HEIGHT)             # solid block
    .faces(">Z").shell(-WALL)               # hollow it, open top, WALL-thick walls
    .faces(">Z").workplane().hole(HOLE_D)   # hole through the top rim
    # Common ops you may want:
    # .edges("|Z").fillet(2)                # round vertical edges
    # .faces(">Z").workplane().rect(L,W,forConstruction=True).vertices().cboreHole(d,cb_d,cb_depth)
)
# =============================================================================

stl = os.path.join(OUTDIR, f"{STEM}.stl")
step = os.path.join(OUTDIR, f"{STEM}.step")
cq.exporters.export(part, stl)
cq.exporters.export(part, step)
print(stl)
print(step)