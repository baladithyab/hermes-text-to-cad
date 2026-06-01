// OpenSCAD parametric part skeleton — text-to-cad skill (OPTIONAL backend).
// Render headless (requires openscad binary/AppImage):
//   openscad -o part.stl part.scad
//   openscad -o view.png --camera=0,0,0,55,0,25,140 --imgsize=600,600 part.scad
// Edit the PARAMS block to iterate.

/* [PARAMS] */
length = 40;   // X (mm)
width  = 30;   // Y (mm)
height = 20;   // Z (mm)
wall   = 2;    // wall thickness (mm)
hole_d = 8;    // hole diameter (mm)

$fn = 64;      // circle facet count — raise for smoother holes

// ============================ MODEL ============================
module part() {
    difference() {
        // hollow box, open top
        difference() {
            cube([length, width, height], center = true);
            translate([0, 0, wall])
                cube([length - 2*wall, width - 2*wall, height], center = true);
        }
        // hole through the top
        translate([0, 0, 0])
            cylinder(h = height * 2, d = hole_d, center = true);
    }
}

part();
// ===============================================================
// Pitfall reminder: OpenSCAD has weak fillets (use minkowski() sparingly — slow).
// For real fillets/chamfers or STEP export, prefer the CadQuery template.