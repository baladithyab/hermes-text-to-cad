#!/usr/bin/env python3
"""cad_helpers.py — first-party parametric geometry helpers (Wave 3, Builder 3b).

A small clean-room library of *correct-by-construction* geometry generators so the
common parametric forms (self-supporting holes, lofts, sweeps, revolves, safe
fillets, shells, emblems, two-body buttons) are not re-derived per-part by an LLM
and don't come out deformed. Every helper:

  * takes explicit, NAMED float params (no magic constants buried in the body);
  * defaults its origin to the part centre (so downstream symmetry/placement gates
    line up with the spec contract's coordinate_frame);
  * returns a cadquery ``Workplane`` (a single watertight solid) — except
    :func:`disc_in_ring_button`, which returns a labelled 2-body ``Compound``;
  * VALIDATES its own output via :func:`_assert_solid` — it tessellates to a mesh
    (lazy ``trimesh`` through a temp STL) and asserts watertight + winding-consistent,
    raising a clear :class:`ValueError` on a degenerate / non-manifold result rather
    than silently returning broken geometry.

CLEAN-ROOM: original cadquery 2.7. No BOSL2 / reference code was vendored or copied;
the maths (teardrop tangent geometry, radius clamping, wall-thickness guard) is
derived here from first principles.

Testability split (matches measure.py / render.py / pbr.py):
  * heavy imports (``cadquery`` / ``trimesh``) are LAZY — done INSIDE each function —
    so the MODULE imports in a plain python for signature-presence + param-validation
    tests, and only the integration tests touch the CAD venv;
  * pure parameter validation raises ``ValueError`` BEFORE any CAD import, so the
    "bad args reject without the stack" tests pass in a bare interpreter.
"""
from __future__ import annotations

import math
import os
import tempfile
from typing import Any, Callable

__all__ = [
    "teardrop",
    "loft_profiles",
    "sweep_along",
    "revolve_profile",
    "safe_fillet",
    "shell_solid",
    "flush_emblem",
    "disc_in_ring_button",
]

# Tessellation tolerances for the validation export — tight enough that a real
# non-manifold result is not masked by coarse faceting, loose enough to stay fast.
_VALIDATE_LINEAR_TOL = 0.05
_VALIDATE_ANGULAR_TOL = 0.2


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _assert_solid(shape: Any, label: str = "solid") -> Any:
    """Tessellate ``shape`` and assert it is a watertight, winding-consistent mesh.

    ``shape`` may be a cadquery ``Workplane``, ``Solid``, or ``Compound``. Returns
    the input unchanged on success; raises :class:`ValueError` with a clear message
    on a degenerate / non-manifold / empty result. Lazy imports so the module loads
    bare.
    """
    import cadquery as cq  # lazy
    import trimesh  # lazy

    # First, OCC's own cheap validity check when the shape exposes it (catches many
    # malformed solids without a tessellation round-trip).
    try:
        val = shape.val() if isinstance(shape, cq.Workplane) else shape
        is_valid = getattr(val, "isValid", None)
        if callable(is_valid) and not val.isValid():
            raise ValueError(f"{label}: OCC reports an invalid shape (isValid=False)")
    except ValueError:
        raise
    except Exception:
        # isValid not available for this shape kind — fall through to the mesh check.
        pass

    fd, path = tempfile.mkstemp(suffix=".stl", prefix="cad_helpers_validate_")
    os.close(fd)
    try:
        cq.exporters.export(
            shape, path,
            tolerance=_VALIDATE_LINEAR_TOL, angularTolerance=_VALIDATE_ANGULAR_TOL,
        )
        mesh = trimesh.load(path, force="mesh")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if mesh is None or len(getattr(mesh, "faces", [])) == 0:
        raise ValueError(f"{label}: tessellation produced an empty mesh")
    if not bool(mesh.is_watertight):
        raise ValueError(f"{label}: result is not watertight (non-manifold geometry)")
    if not bool(mesh.is_winding_consistent):
        raise ValueError(f"{label}: result has inconsistent face winding")
    return shape


def _require_positive(**kwargs: float) -> None:
    """Raise ValueError unless every named value is a finite, strictly-positive float.

    Pure (no CAD import) so callers' param validation runs in a bare interpreter.
    """
    for name, value in kwargs.items():
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number, got {value!r}")
        if not math.isfinite(v):
            raise ValueError(f"{name} must be finite, got {value!r}")
        if v <= 0.0:
            raise ValueError(f"{name} must be > 0, got {v}")


_AXES = ("x", "y", "z")


def _normalize_axis(axis: str) -> str:
    a = str(axis).strip().lower()
    if a not in _AXES:
        raise ValueError(f"axis must be one of {_AXES}, got {axis!r}")
    return a


# ---------------------------------------------------------------------------
# teardrop — self-supporting hole / peg profile
# ---------------------------------------------------------------------------
def teardrop(radius: float, length: float, angle_deg: float = 45.0, axis: str = "z") -> Any:
    """A self-supporting teardrop solid (FDM-printable horizontal hole / peg profile).

    A circle of ``radius`` with a tangent-line peak so the top never exceeds the
    ``angle_deg`` overhang (45° is the classic self-supporting limit), extruded
    ``length`` along ``axis`` and centred on the origin along that axis. The two
    upper tangent lines meet at an apex; below the centre it is a plain circular arc.

    The peak geometry is derived from first principles: a line tangent to the circle
    leaving the contact point at ``angle_deg`` above horizontal meets its mirror at
    the apex ``radius / sin(angle_deg)`` above centre.

    Returns a single watertight cadquery ``Workplane``. Raises ``ValueError`` for
    non-positive sizes or an ``angle_deg`` outside (0, 90).
    """
    _require_positive(radius=radius, length=length)
    a = float(angle_deg)
    if not (0.0 < a < 90.0):
        raise ValueError(f"angle_deg must be in (0, 90), got {angle_deg}")
    axis = _normalize_axis(axis)

    import cadquery as cq  # lazy

    r = float(radius)
    ang = math.radians(a)
    # Tangent contact point on the upper-right of the circle and the shared apex.
    cx = r * math.cos(ang)
    cy = r * math.sin(ang)
    apex = r / math.sin(ang)

    # Build the 2D profile in the working plane, then extrude. The profile is
    # centred on its own (0,0); extrude with ``both=True`` at half-length so the
    # solid is centred on the origin along the extrusion axis (both=True goes +/-,
    # so half-length each way == the requested total length).
    base_plane = {"z": "XY", "x": "YZ", "y": "XZ"}[axis]
    solid = (
        cq.Workplane(base_plane)
        .moveTo(cx, cy)
        .lineTo(0.0, apex)
        .lineTo(-cx, cy)
        .threePointArc((0.0, -r), (cx, cy))
        .close()
        .extrude(float(length) / 2.0, both=True)
    )
    return _assert_solid(solid, "teardrop")


# ---------------------------------------------------------------------------
# loft — stacked profiles with consistent winding
# ---------------------------------------------------------------------------
def loft_profiles(profiles: list[tuple[float, Callable[[Any], Any]]], ruled: bool = False) -> Any:
    """Loft an ordered stack of 2D profiles into a single solid.

    ``profiles`` is an ordered list of ``(z, wire_factory)`` pairs. Each
    ``wire_factory(wp)`` receives a fresh cadquery ``Workplane`` already positioned
    at the right Z offset and must return that workplane with exactly one CLOSED
    wire drawn on it (e.g. ``lambda wp: wp.circle(10)``). Profiles are lofted in the
    given order; ``ruled=False`` gives a smooth (B-spline) skin, ``ruled=True`` a
    straight-ruled one.

    Winding is normalised by cadquery's loft kernel, which re-orders each section's
    seam; we additionally validate the result is a single watertight solid.

    Returns a single watertight cadquery ``Workplane``. Raises ``ValueError`` if
    fewer than two profiles are given or a factory does not yield a closed wire.
    """
    if not isinstance(profiles, (list, tuple)) or len(profiles) < 2:
        raise ValueError("loft_profiles needs at least 2 (z, wire_factory) profiles")
    zs = []
    for i, item in enumerate(profiles):
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError(f"profile[{i}] must be a (z, wire_factory) pair, got {item!r}")
        z, factory = item
        try:
            float(z)
        except (TypeError, ValueError):
            raise ValueError(f"profile[{i}] z must be a number, got {z!r}")
        if not callable(factory):
            raise ValueError(f"profile[{i}] wire_factory must be callable, got {factory!r}")
        zs.append(float(z))
    if len(set(zs)) != len(zs):
        raise ValueError(f"loft_profiles z offsets must be distinct, got {zs}")

    import cadquery as cq  # lazy

    # Draw every section onto ONE workplane chain at its own Z so loft sees them as
    # an ordered stack. Each factory contributes one pending wire.
    wp = cq.Workplane("XY")
    base_z = zs[0]
    for z, factory in profiles:
        section = factory(cq.Workplane("XY").workplane(offset=float(z) - base_z))
        wire = section.ctx.pendingWires
        if not wire:
            raise ValueError("each loft wire_factory must produce a closed wire")
        wp.ctx.pendingWires.extend(wire)
    solid = wp.loft(ruled=bool(ruled), combine=True)
    return _assert_solid(solid, "loft_profiles")


# ---------------------------------------------------------------------------
# sweep — a section along a polyline path
# ---------------------------------------------------------------------------
def sweep_along(section_factory: Callable[[Any], Any],
                path_points: list[tuple[float, float, float]],
                keep_normal: bool = True) -> Any:
    """Sweep a 2D section along a 3D polyline path.

    ``section_factory(wp)`` receives a fresh cadquery ``Workplane`` and returns it
    with one closed wire (the cross-section). ``path_points`` is an ordered list of
    >= 2 ``(x, y, z)`` points defining the sweep spine (drawn as a smooth spline).
    ``keep_normal=True`` keeps the section perpendicular to the path tangent
    (cadquery ``isFrenet=True``); ``False`` keeps it parallel-transported.

    Returns a single watertight cadquery ``Workplane``. Raises ``ValueError`` for a
    bad section factory or a path with fewer than two distinct points.
    """
    if not callable(section_factory):
        raise ValueError("section_factory must be callable")
    if not isinstance(path_points, (list, tuple)) or len(path_points) < 2:
        raise ValueError("sweep_along needs at least 2 path points")
    pts = []
    for i, p in enumerate(path_points):
        if not (isinstance(p, (list, tuple)) and len(p) == 3):
            raise ValueError(f"path_points[{i}] must be an (x, y, z) triple, got {p!r}")
        pts.append(tuple(float(c) for c in p))
    if len({pts[i] for i in range(len(pts))}) < 2:
        raise ValueError("sweep_along path needs at least 2 distinct points")

    import cadquery as cq  # lazy

    path = cq.Workplane("XY").spline(pts) if len(pts) > 2 else cq.Workplane("XY").polyline(pts)
    section = section_factory(cq.Workplane("XY"))
    if not section.ctx.pendingWires:
        raise ValueError("section_factory must produce a closed wire")
    solid = section.sweep(path, isFrenet=bool(keep_normal))
    return _assert_solid(solid, "sweep_along")


# ---------------------------------------------------------------------------
# revolve — a profile around an axis
# ---------------------------------------------------------------------------
def revolve_profile(profile_factory: Callable[[Any], Any], axis: str = "z",
                    degrees: float = 360.0) -> Any:
    """Revolve a planar profile around an axis to make an axisymmetric solid.

    ``profile_factory(wp)`` receives a fresh cadquery ``Workplane`` on the plane that
    CONTAINS the revolution axis and must return it with one closed wire lying
    ENTIRELY on one side of the axis (a profile that crosses the axis self-intersects
    when revolved). ``axis`` is the revolution axis ("z" default); ``degrees`` is the
    sweep (0 < degrees <= 360).

    We assert the profile stays on one side of the axis (within tolerance) before
    revolving, so a crossing profile fails loudly instead of producing garbage.

    Returns a single watertight cadquery ``Workplane``. Raises ``ValueError`` for a
    bad factory, an out-of-range ``degrees``, or a profile that crosses the axis.
    """
    if not callable(profile_factory):
        raise ValueError("profile_factory must be callable")
    d = float(degrees)
    if not (0.0 < d <= 360.0):
        raise ValueError(f"degrees must be in (0, 360], got {degrees}")
    axis = _normalize_axis(axis)

    import cadquery as cq  # lazy

    # Workplane whose local plane contains the chosen revolution axis. For axis "z"
    # we draw on XZ (local X is radial, local Y is the world Z spin axis); the
    # revolve is about the local Y axis by default, which maps to world Z.
    plane = {"z": "XZ", "x": "ZY", "y": "XY"}[axis]
    section = profile_factory(cq.Workplane(plane))
    if not section.ctx.pendingWires:
        raise ValueError("profile_factory must produce a closed wire")

    # Guard: every vertex of the pending wire must lie on one side of the local X=0
    # line (the projected axis). A wire that straddles it self-intersects on revolve.
    tol = 1e-6
    xs: list[float] = []
    for wire in section.ctx.pendingWires:
        for v in wire.Vertices():
            xs.append(float(v.X))
    if xs:
        has_pos = any(x > tol for x in xs)
        has_neg = any(x < -tol for x in xs)
        if has_pos and has_neg:
            raise ValueError(
                "revolve_profile: profile crosses the revolution axis "
                f"(x spans {min(xs):.3f}..{max(xs):.3f}); keep it on one side"
            )

    solid = section.revolve(d)
    return _assert_solid(solid, "revolve_profile")


# ---------------------------------------------------------------------------
# safe_fillet — radius-clamped, retrying fillet
# ---------------------------------------------------------------------------
def safe_fillet(part: Any, selector: str, radius: float,
                fallback_ratio: float = 0.4) -> tuple[Any, float]:
    """Fillet selected edges, clamping the radius so OCC does not blow up.

    A fillet radius larger than (roughly) half the shortest adjacent edge makes OCC
    raise ``StdFail_NotDone``. We first clamp ``radius`` to ``fallback_ratio`` of the
    shortest SELECTED edge, then attempt the fillet; on an OCC failure we retry at
    progressively smaller radii (0.5x each step) until it succeeds or the radius
    becomes negligible.

    ``part`` is a cadquery ``Workplane``; ``selector`` is a cadquery edge selector
    string (e.g. ``"|Z"``). Returns ``(filleted_workplane, applied_radius)``. Raises
    ``ValueError`` for a non-positive radius/ratio, an empty selection, or if no
    positive radius succeeds.
    """
    _require_positive(radius=radius)
    if not (0.0 < float(fallback_ratio) <= 1.0):
        raise ValueError(f"fallback_ratio must be in (0, 1], got {fallback_ratio}")
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError("selector must be a non-empty cadquery edge-selector string")

    edges = part.edges(selector)
    edge_vals = edges.vals()
    if not edge_vals:
        raise ValueError(f"safe_fillet: selector {selector!r} matched no edges")

    shortest = min(float(e.Length()) for e in edge_vals)
    # Clamp so the requested radius never exceeds fallback_ratio of the shortest edge.
    applied = min(float(radius), float(fallback_ratio) * shortest)

    last_exc: Exception | None = None
    while applied > 1e-4:
        try:
            result = part.edges(selector).fillet(applied)
            return _assert_solid(result, "safe_fillet"), round(applied, 4)
        except Exception as exc:  # OCC StdFail_NotDone et al — retry smaller
            last_exc = exc
            applied *= 0.5
    raise ValueError(
        f"safe_fillet: could not fillet {selector!r} at any positive radius "
        f"(shortest edge {shortest:.3f} mm); last error: {last_exc}"
    )


# ---------------------------------------------------------------------------
# shell_solid — hollow with a guarded wall thickness
# ---------------------------------------------------------------------------
def shell_solid(part: Any, open_face_selector: str, wall: float) -> Any:
    """Hollow ``part`` to a ``wall``-thick shell, opening the selected face(s).

    Validates ``wall < 0.5 * min_span`` (the smallest bounding-box dimension) BEFORE
    offsetting — a wall at/over half the thinnest span leaves no cavity and makes
    OCC's shell offset fail or self-intersect. ``open_face_selector`` is a cadquery
    face selector (e.g. ``">Z"``) naming the face(s) to leave open.

    Returns a single watertight cadquery ``Workplane`` (the open face means the
    *solid* is a closed thin-walled cup; the mesh of a proper shell is still
    watertight). Raises ``ValueError`` for a non-positive wall, an empty face
    selection, or a wall too thick for the part.
    """
    _require_positive(wall=wall)
    if not isinstance(open_face_selector, str) or not open_face_selector.strip():
        raise ValueError("open_face_selector must be a non-empty cadquery face-selector string")

    bb = part.val().BoundingBox()
    min_span = min(bb.xlen, bb.ylen, bb.zlen)
    if float(wall) >= 0.5 * float(min_span):
        raise ValueError(
            f"shell_solid: wall {wall} too thick — must be < 0.5 * min span "
            f"({0.5 * min_span:.3f} mm) or no cavity remains"
        )

    faces = part.faces(open_face_selector)
    if not faces.vals():
        raise ValueError(f"shell_solid: face selector {open_face_selector!r} matched no faces")

    # cadquery's shell offsets inward when given a negative thickness.
    shelled = part.faces(open_face_selector).shell(-float(wall))
    return _assert_solid(shelled, "shell_solid")


# ---------------------------------------------------------------------------
# flush_emblem — coplanar raised / engraved 2D motif on a host face
# ---------------------------------------------------------------------------
def flush_emblem(host: Any, emblem_2d_factory: Callable[[Any], Any], face_selector: str,
                 depth: float, mode: str = "raised") -> Any:
    """Add a flush 2D emblem to a host face — raised (fused) or engraved (cut).

    ``emblem_2d_factory(wp)`` receives a cadquery ``Workplane`` already on the
    selected host face and returns it with one closed 2D wire (the motif outline).
    ``mode="raised"`` extrudes the motif ``depth`` proud of the face and fuses it
    coplanarly; ``mode="engraved"`` cuts it ``depth`` into the face. ``face_selector``
    is a cadquery face selector (e.g. ``">Z"``).

    Returns a single watertight cadquery ``Workplane``. Raises ``ValueError`` for a
    non-positive depth, an unknown mode, an empty face selection, or an empty motif.
    """
    _require_positive(depth=depth)
    if not callable(emblem_2d_factory):
        raise ValueError("emblem_2d_factory must be callable")
    mode = str(mode).strip().lower()
    if mode not in ("raised", "engraved"):
        raise ValueError(f"mode must be 'raised' or 'engraved', got {mode!r}")
    if not isinstance(face_selector, str) or not face_selector.strip():
        raise ValueError("face_selector must be a non-empty cadquery face-selector string")

    if not host.faces(face_selector).vals():
        raise ValueError(f"flush_emblem: face selector {face_selector!r} matched no faces")

    motif = emblem_2d_factory(host.faces(face_selector).workplane())
    if not motif.ctx.pendingWires:
        raise ValueError("emblem_2d_factory must produce a closed 2D wire")

    if mode == "raised":
        # extrude() on a face workplane fuses with the existing solid (combine=True).
        result = motif.extrude(float(depth), combine=True)
    else:  # engraved
        result = motif.cutBlind(-float(depth))
    return _assert_solid(result, f"flush_emblem[{mode}]")


# ---------------------------------------------------------------------------
# disc_in_ring_button — coaxial 2-body compound (the only multi-body helper)
# ---------------------------------------------------------------------------
def disc_in_ring_button(ring_od: float, ring_id: float, disc_d: float,
                        height: float, gap: float) -> Any:
    """A coaxial 2-body button: an outer ring with a free-floating inner disc.

    Two SEPARATE bodies sharing the Z axis and centred on the origin:
      * a ring (annulus) of outer diameter ``ring_od``, inner diameter ``ring_id``;
      * a disc of diameter ``disc_d`` sitting inside the ring with a clearance
        ``gap`` all round, so it never touches the ring.
    Both are ``height`` tall. Asserts ``disc_d < ring_id - 2*gap`` (room for the gap
    on both sides) and ``ring_id < ring_od`` before building.

    Returns a labelled 2-body cadquery ``Compound`` (the solids carry ``label``
    ``"ring"`` and ``"disc"``) — the one helper that intentionally returns more than
    one body. Raises ``ValueError`` on any geometric impossibility.
    """
    _require_positive(ring_od=ring_od, ring_id=ring_id, disc_d=disc_d, height=height)
    g = float(gap)
    if not math.isfinite(g) or g < 0.0:
        raise ValueError(f"gap must be a finite, non-negative number, got {gap!r}")
    if float(ring_id) >= float(ring_od):
        raise ValueError(f"ring_id ({ring_id}) must be < ring_od ({ring_od})")
    if float(disc_d) >= float(ring_id) - 2.0 * g:
        raise ValueError(
            f"disc_d ({disc_d}) must be < ring_id - 2*gap "
            f"({float(ring_id) - 2.0 * g:.3f}) so the gap clears all round"
        )

    import cadquery as cq  # lazy

    ring = (
        cq.Workplane("XY")
        .circle(float(ring_od) / 2.0)
        .circle(float(ring_id) / 2.0)
        .extrude(float(height))
        .val()
    )
    disc = (
        cq.Workplane("XY")
        .circle(float(disc_d) / 2.0)
        .extrude(float(height))
        .val()
    )
    # Centre both on the origin in Z (extrude goes +Z from the XY plane).
    ring = ring.translate((0, 0, -float(height) / 2.0))
    disc = disc.translate((0, 0, -float(height) / 2.0))

    # Validate each body individually (a compound of two disjoint solids is itself
    # not "watertight" as one mesh, so _assert_solid on the compound would mis-fire).
    _assert_solid(cq.Workplane(obj=ring), "disc_in_ring_button[ring]")
    _assert_solid(cq.Workplane(obj=disc), "disc_in_ring_button[disc]")

    # makeCompound rebuilds its members and drops per-solid labels, so we keep the
    # bodies in a DETERMINISTIC order (ring first, the larger-radius body) and label
    # the compound itself. Callers identify the bodies by that order: index 0 = ring,
    # index 1 = disc.
    compound = cq.Compound.makeCompound([ring, disc])
    try:
        compound.label = "disc_in_ring_button"
    except Exception:
        pass  # label is a convenience; not all builds expose it
    if len(compound.Solids()) != 2:
        raise ValueError(
            f"disc_in_ring_button: expected a 2-body compound, got {len(compound.Solids())}"
        )
    return compound
