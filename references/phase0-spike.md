# Phase-0 Spike — verified CAD foundation (2026-05-31, WSL)

The de-risk that proves the closed loop works in userspace with no root. Reproduce this first before building anything on top.

## Environment facts that drove backend choice

- **No passwordless sudo** (`sudo -n true` → fails) → OpenSCAD `apt install` is blocked.
- FUSE present (`/dev/fuse` exists, `fusermount`/`fusermount3` on PATH) → OpenSCAD AppImage *would* work, but unproven and unnecessary.
- `uv` available; uv-created venvs do **not** ship `pip` — use `uv pip install --python <venv>/bin/python ...`, not `<venv>/bin/python -m pip`.

## Install (pure userspace, no root)

```bash
uv venv /tmp/cadtest --python 3.11
uv pip install --python /tmp/cadtest/bin/python cadquery trimesh numpy
/tmp/cadtest/bin/python -c "import cadquery as cq, trimesh; print(cq.__version__, trimesh.__version__)"
# → cadquery 2.7.0  trimesh 4.12.2   (OpenCascade bundled in the cadquery wheel, ~200MB)
```

For a durable capability use a dedicated venv (e.g. `~/.venvs/cad`), not the gateway venv.

## The runnable loop (spike.py)

```python
import cadquery as cq
import trimesh, json, os
OUT = "/tmp/cadtest/out"; os.makedirs(OUT, exist_ok=True)

# SPEC
spec = {"L":40.0,"W":30.0,"H":20.0,"wall":2.0,"hole_d":8.0}

# GENERATE (B-rep, parametric)
part = (cq.Workplane("XY")
        .box(spec["L"], spec["W"], spec["H"])
        .faces(">Z").shell(-spec["wall"])               # hollow, open top, 2mm walls
        .faces(">Z").workplane().hole(spec["hole_d"]))   # 8mm hole in top rim

# EXPORT both formats
cq.exporters.export(part, f"{OUT}/part.stl")
cq.exporters.export(part, f"{OUT}/part.step")            # manufacturing-grade B-rep

# MEASURE (quantitative gate)
m = trimesh.load(f"{OUT}/part.stl")
bbox = m.bounding_box.extents.tolist()
exp = sorted([spec["L"], spec["W"], spec["H"]]); got = sorted(bbox)
gate_pass = all(abs(a-b) < 0.1 for a,b in zip(exp, got))
print(json.dumps({"bbox": [round(x,3) for x in bbox],
                  "watertight": bool(m.is_watertight),
                  "volume_mm3": round(float(m.volume),2),
                  "n_faces": int(len(m.faces)),
                  "quant_gate_PASS": gate_pass}, indent=2))
```

## Verified output

```
bbox: [40.0, 30.0, 20.0]   ← exact match to spec
watertight: true           ← printable, manifold
volume_mm3: 7051.51
n_faces: 536
quant_gate_PASS: true
part.stl  = 26884 bytes
part.step = 35112 bytes    ← STEP export works, no extra deps
```

## Takeaways

1. **CadQuery is the pragmatic primary backend** — installs without root, exports STEP, gives shells/fillets. This inverts the naive "OpenSCAD is the default" assumption.
2. **The numeric gate needs no display** — trimesh loads STL directly. The quantitative half of the loop works on any headless box today.
3. **Still unproven**: offscreen PNG rendering for the vision gate (needs `vtk` + `xvfb` or VTK offscreen). Prove that next; it's the gate on the multi-model review half.

## CadQuery idioms worth remembering

- `.faces(">Z")` selects the top face; `<Z`, `>X` etc. by direction. Selectors survive parameter changes (design-intent, unlike click-a-point).
- `.shell(-t)` hollows with wall thickness `t`; negative = inward. Apply after selecting the face(s) to leave open.
- `.workplane().hole(d)` drills through from the current face.
- `cq.exporters.export(obj, "x.step")` / `"x.stl"` — format inferred from extension.
