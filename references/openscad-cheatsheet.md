# OpenSCAD Cheatsheet (optional backend)

OpenSCAD = CSG (constructive solid geometry). Not installed by default here (needs root or AppImage). Use only for CSG-style authoring or CADAM-style parameter sliders. **For fillets/chamfers/STEP, use CadQuery instead** — OpenSCAD is weak at all three.

## Headless render
```bash
openscad -o part.stl part.scad                      # STL export
openscad -o view.png --imgsize=600,600 \
  --camera=0,0,0,55,0,25,140 part.scad              # PNG (cam: tx,ty,tz,rx,ry,rz,dist)
openscad -o part.stl -D 'length=50' -D 'hole_d=6' part.scad   # override params via -D
```

## Primitives
```scad
cube([x,y,z], center=true);
sphere(r=5);          sphere(d=10);
cylinder(h=10, r=4);  cylinder(h=10, d1=8, d2=4);   // cone
polyhedron(points=[...], faces=[...]);
```

## Booleans (the core)
```scad
union()        { a(); b(); }
difference()   { base(); hole(); }   // first child MINUS the rest
intersection() { a(); b(); }
```

## Transforms
```scad
translate([x,y,z]) child();
rotate([rx,ry,rz]) child();
scale([sx,sy,sz]) child();
mirror([1,0,0]) child();
```

## Smoothness & modules
```scad
$fn = 64;                       // global facet count (circles/spheres)
cylinder(h=10, d=8, $fn=128);   // per-call override
module thing(p=1) { ... }       // parametric reusable module
for (i=[0:4]) translate([i*10,0,0]) thing();
```

## LLM failure modes to AVOID (from real text-to-OpenSCAD bug reports)
- **Spurious `rotate()` that tips the whole model** — the classic "abstract art shaped like regret" bug. Render and eyeball after every structural change.
- **Mixing up `difference()` child order** — the first child is the keep-body; everything after is subtracted. Wrong order = empty or inverted geometry.
- **Holes not long enough** — make subtracted cylinders longer than the body (`h = height*2, center=true`) so they fully pierce; a flush-length cylinder leaves a skin.
- **Off-by-center** — `cube()` default is corner-origin; `cube(center=true)` is centered. Mixing the two misaligns cuts. Pick one convention.
- **Weak fillets** — `minkowski()` with a sphere "rounds" but is very slow and balloons facet count. If the part needs real fillets, switch to CadQuery.
- **No STEP export** — OpenSCAD only does mesh (STL/OFF/AMF/3MF). Need STEP for machining? CadQuery.

## When OpenSCAD is actually the right call
- Pure CSG parts (brackets, enclosures, grids, lattices) that are naturally union/difference.
- You want CADAM-style auto-extracted parameter sliders (the `/* [Group] */` + `// comment` param syntax drives the customizer).
- 3D-print-only, no manufacturing/STEP need.