# CadQuery Cheatsheet (primary backend)

CadQuery 2.7 on `~/.venvs/cad`. B-rep kernel (OpenCascade via OCP). Exports STEP/STL/3MF/SVG/DXF.

## Mental model
You build a `Workplane`, push/pop geometry, and **select** faces/edges/vertices to operate on. Selectors survive parameter changes (the killer feature vs "click that point").

## Starting a part
```python
import cadquery as cq
r = cq.Workplane("XY")          # planes: XY, YZ, XZ, "front","top","right",...
```

## Primitives & 2D->3D
```python
.box(L, W, H)                   # centered box
.cylinder(h, radius)
.sphere(r)
.circle(r).extrude(h)           # 2D sketch -> solid
.rect(w, h).extrude(h)
.polygon(nSides, diameter).extrude(h)   # diameter = CIRCUMSCRIBED circle (corner-to-corner), NOT across-flats! see pitfalls
.text("Hi", 8, 2)               # extruded text
```

## Selectors (the important part)
```python
.faces(">Z")        # the face with the max Z (top).  "<Z"=bottom, ">X","<Y"...
.faces("|Z")        # faces whose normal is parallel to Z
.edges(">Z")        # top edges
.edges("|Z")        # vertical edges
.edges("#Z")        # edges perpendicular to Z
.vertices(">XY")
.faces(">Z").workplane()   # set a new workplane ON the selected face
```

## Modifying ops
```python
.shell(-2)                      # hollow; negative = inward wall. Select open face(s) first:
.faces(">Z").shell(-2)          #   hollow with open top, 2mm walls
.fillet(3)                      # round selected edges (B-rep — REAL fillets, unlike OpenSCAD)
.chamfer(1)
.hole(d)                        # simple through hole at workplane center
.cboreHole(d, cbore_d, cbore_depth)   # counterbore
.cskHole(d, csk_d, csk_angle)         # countersink
.cutThruAll()                   # cut a sketch through everything
.cutBlind(-depth)
.union(other) / .cut(other) / .intersect(other)
```

## Patterns / transforms
```python
.faces(">Z").workplane().rect(40, 20, forConstruction=True).vertices().hole(3)  # 4 corner holes
.pushPoints([(x1,y1),(x2,y2)]).hole(3)
.transformed(offset=(x,y,z), rotate=(rx,ry,rz))
.rotate((0,0,0),(0,0,1),45)     # rotate whole solid 45deg about Z
```

## Export (ALWAYS both for real parts)
```python
cq.exporters.export(part, "part.stl")    # mesh: render gate, measure gate, 3D printing
cq.exporters.export(part, "part.step")   # B-rep: manufacturing/machining, opens in any CAD
```

## Pitfalls
- **`.polygon(n, D)` D is the CIRCUMSCRIBED-circle (corner-to-corner) diameter, NOT across-flats.** For a hex specified by wrench size / across-flats `AF`, the corners stick out further, so the bbox/XY extent will be larger than `AF`. Convert: `D = AF / cos(180/n)`; for a hexagon `D = AF / 0.8660254`. Verified 2026-05-31 — a hex flange specced at 22mm across-flats measured 25.4mm in Y because `.polygon(6, 22)` was used directly; the numeric gate caught it. Always convert across-flats→circumscribed before passing to `.polygon`, and remember the bbox gate will measure corner-to-corner on the widest axis.
- **`.loft()` between two circles of different radius makes a cone (a barb ridge); a diameter mismatch between a body cylinder and the barb base leaves a visible step/ledge.** Match the barb base diameter to the body OD if you want a flush transition, or accept the ledge as a feature. (Cosmetic — vision gate flags it, numeric gate won't.)
- **`.shell()` needs the open face selected first**, else it hollows fully closed (no opening) or errors. `.faces(">Z").shell(-2)`.
- **Fillet radius too large for the edge** -> OCC kernel error. Reduce radius or fillet fewer edges.
- **STL is a mesh approximation** — measure gate dims are exact to ~3 decimals; tiny curved-surface volume drift is normal. Use STEP for true geometry.
- **`force="mesh"` on load** in measure.py handles scenes vs meshes; STL is always a single mesh.
- **Tessellation tolerance**: default STL export is fine for gates; for smooth printing pass `tolerance=0.01, angularTolerance=0.1` to `export`.
- Heavy boolean chains can be slow — build incrementally and render to check.