#!/usr/bin/env python3
"""placement.py — PURE placement/symmetry math for the geometry-sanity gate.

The hard-won lesson behind this module: a numeric PASS (right bbox, watertight,
right volume) does NOT mean the part is correctly PLACED. A mirrored bracket
whose holes drifted off the symmetry plane, or a body that landed at the wrong
centroid, sails through a bbox/volume gate. These helpers turn measured
centroids + bounding-box bounds into the residuals/matches that measure.gate()
asserts against.

PURE on purpose: stdlib + plain math only (NO trimesh / numpy). That keeps the
module importable — and unit-testable — in a plain python with no CAD stack,
exactly like core.py. measure.gate() imports these to do the symmetry /
expect_centroid / expect_bodies checks.
"""
from __future__ import annotations

import math


def centroid_offset(center_mass, bbox_min, bbox_max):
    """Per-axis offset of the centre-of-mass from the bounding-box centre.

    Returns ``[dx, dy, dz] = center_mass - (bbox_min + bbox_max) / 2``. For a
    part that is geometrically symmetric about an axis-plane the centroid lands
    on that plane, so the corresponding component is ~0; a non-zero component is
    the signed drift of the mass away from where the bbox says the centre is.
    """
    return [
        float(cm) - (float(lo) + float(hi)) / 2.0
        for cm, lo, hi in zip(center_mass, bbox_min, bbox_max)
    ]


_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def symmetry_residual(center_mass, bbox_min, bbox_max, axes):
    """Absolute centroid-vs-bbox-centre residual for each requested axis.

    ``axes`` is a list of "x"/"y"/"z". A part symmetric about an axis-plane has
    its centroid ON that plane, so residual ~ 0; the gate fails the symmetry
    check when any residual exceeds the placement tolerance. Returns a dict
    ``{axis: abs_residual}`` containing only the requested axes (unknown axis
    labels are ignored).
    """
    offset = centroid_offset(center_mass, bbox_min, bbox_max)
    out = {}
    for ax in axes:
        idx = _AXIS_INDEX.get(str(ax).lower())
        if idx is None:
            continue
        out[ax] = abs(offset[idx])
    return out


def _dist(a, b):
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def match_bodies(measured_centroids, expected):
    """Greedily match each expected centroid to its nearest measured centroid.

    ``measured_centroids`` and ``expected`` are lists of ``[x, y, z]``. Greedy
    nearest-neighbour: over all (expected, measured) pairs sorted by distance,
    assign the closest pair first, then the next closest using only
    still-unused bodies, and so on — each measured centroid is consumed at most
    once. Returns one entry per expected centroid::

        [{"expected_index": i, "measured_index": j|None, "dist": float|None}, ...]

    An expected centroid left without a free measured body gets
    ``measured_index=None`` / ``dist=None``. The caller (gate's "expect_bodies"
    check) then compares each ``dist`` to that expected body's ``tol``; a None
    means unmatched and so a guaranteed failure.
    """
    n_exp = len(expected)
    n_meas = len(measured_centroids)

    # All candidate pairs, closest first. Stable on ties via the index tuple.
    pairs = []
    for ei in range(n_exp):
        for mi in range(n_meas):
            pairs.append((_dist(expected[ei], measured_centroids[mi]), ei, mi))
    pairs.sort(key=lambda p: (p[0], p[1], p[2]))

    assigned_exp = {}     # expected_index -> (measured_index, dist)
    used_measured = set()
    for dist, ei, mi in pairs:
        if ei in assigned_exp or mi in used_measured:
            continue
        assigned_exp[ei] = (mi, dist)
        used_measured.add(mi)
        if len(assigned_exp) == n_exp or len(used_measured) == n_meas:
            # nothing more can be matched once either side is exhausted
            if len(assigned_exp) == n_exp:
                break

    result = []
    for ei in range(n_exp):
        if ei in assigned_exp:
            mi, dist = assigned_exp[ei]
            result.append({"expected_index": ei, "measured_index": mi, "dist": dist})
        else:
            result.append({"expected_index": ei, "measured_index": None, "dist": None})
    return result
