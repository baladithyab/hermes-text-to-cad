"""hermes_text_to_cad.core — the closed-loop engine.

Thin orchestration over the proven scripts (render.py / measure.py /
scatter_review.py), executed via a dedicated CAD venv so the heavy CAD stack
(cadquery/vtk/trimesh) never has to import into the Hermes venv.

Public surface (used by __init__.register and the CLI):
  cad_venv_python() -> str        # path to the venv interpreter
  generate(code, out_dir) -> dict # run user CadQuery code -> STL/STEP
  render(stl) -> dict             # -> montage PNG
  measure(stl, spec) -> dict      # numeric gate -> {report, gate_pass}
  review(montage, spec) -> dict   # multi-model vision gate (needs OPENROUTER_API_KEY)
  doctor() -> dict                # readiness check
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import safety

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN_DIR / "scripts"
DEFAULT_VENV = Path(os.path.expanduser("~/.venvs/cad"))

# ---- subprocess environment scrubbing (ADR-0001) ----------------------------
# cad_generate runs ARBITRARY model-authored Python in a subprocess. Inheriting
# the full os.environ would hand that code OPENROUTER_API_KEY and every other
# secret in the Hermes process. We pass ONLY an allowlist. This is default-deny:
# a denylist leaks any not-yet-patterned secret; an empty env breaks PATH/HOME/
# locale/render tooling. So: allow exactly what the CAD subprocesses need.
#
# Exact-match keys + LC_* prefix. HERMES_CAD_PYTHON is deliberately ABSENT — it's
# read in the parent to LOCATE the interpreter; the child never needs it. The
# vision gate (scatter_review.py) reads OPENROUTER_API_KEY from ~/.hermes/.env
# itself, so the key never needs to ride in any subprocess env either.
_ENV_ALLOWLIST = frozenset({
    # locating + running an interpreter
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "PWD",
    # locale (cadquery/OCC, font lookup, text rendering)
    "LANG", "LANGUAGE",
    # output dir (injected per-call via extra=)
    "CAD_OUT",
    # render / X / GL knobs the render path needs
    "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY",
    "LIBGL_ALWAYS_SOFTWARE", "GALLIUM_DRIVER", "MESA_GL_VERSION_OVERRIDE",
    "VTK_DEFAULT_OPENGL_WINDOW", "PYOPENGL_PLATFORM", "__GLX_VENDOR_LIBRARY_NAME",
    # python runtime hygiene (no behavior leak, but keep deterministic)
    "PYTHONUNBUFFERED", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE",
})


def _scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal subprocess env from an allowlist (ADR-0001).

    Returns only allowlisted keys (exact-match in _ENV_ALLOWLIST, or LC_*),
    never arbitrary secrets from the parent. ``extra`` is merged last so callers
    can inject/override (e.g. CAD_OUT for generate). HERMES_CAD_PYTHON and every
    *_API_KEY/*_TOKEN/*_SECRET are dropped by construction (they're not on the
    allowlist).
    """
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in _ENV_ALLOWLIST or k.startswith("LC_"):
            env[k] = v
    if extra:
        env.update(extra)
    return env


def cad_venv_python() -> str:
    """Path to the CAD venv python. Override with HERMES_CAD_PYTHON."""
    override = os.environ.get("HERMES_CAD_PYTHON")
    if override:
        return override
    return str(DEFAULT_VENV / "bin" / "python")


# Common no-root AppImage-extract locations (`openscad.AppImage --appimage-extract`
# leaves a squashfs-root tree). Checked after $PATH; HERMES_OPENSCAD_BIN wins.
_OPENSCAD_CANDIDATES = (
    "~/squashfs-root/usr/bin/openscad",
    "~/.local/bin/openscad",
    "~/openscad/squashfs-root/usr/bin/openscad",
    "/opt/openscad/squashfs-root/usr/bin/openscad",
)


def openscad_bin() -> str | None:
    """Resolve an OpenSCAD binary (ADR-0008): HERMES_OPENSCAD_BIN, then $PATH,
    then common AppImage-extract locations. None if not found — the optional
    backend degrades to a clear error rather than crashing."""
    override = os.environ.get("HERMES_OPENSCAD_BIN")
    if override and os.path.exists(override):
        return override
    found = shutil.which("openscad")
    if found:
        return found
    for cand in _OPENSCAD_CANDIDATES:
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return None


# ---- opt-in OS sandbox for generated code (ADR-0002) ------------------------
# When HERMES_CAD_SANDBOX=1, wrap the generate subprocess in bubblewrap (no
# network, read-only filesystem except CAD_OUT, secret dirs masked with tmpfs).
# firejail is the fallback. Opt-in: absent the flag, generate runs unsandboxed
# (the default trusted-use case). When the flag is set but no sandbox binary
# exists, we WARN and run unsandboxed (degrade, not fail) — a conscious choice
# documented in ADR-0002, surfaced as sandbox="requested-unavailable".

# Home-relative paths to mask inside the sandbox so generated code can't read
# secrets/keys even though the root is bind-mounted read-only. Masked at the
# RESOLVED realpath (a symlinked ~/.aws -> /mnt/c/... still gets masked).
# Directories are masked with --tmpfs; FILES need --ro-bind /dev/null (a tmpfs
# can't mount over a regular file) — keeping them separate fixes the bug where
# .netrc (a file) was silently skipped by an isdir() guard.
_SECRET_DIRS = (".hermes", ".ssh", ".aws", ".gnupg", ".azure",
                ".config/gcloud", ".config/git", ".kube", ".docker",
                ".local/share/keyrings")
_SECRET_FILES = (".netrc", ".git-credentials", ".gitconfig",
                 ".npmrc", ".pypirc", ".python_history", ".bash_history")


def sandbox_info() -> dict[str, Any]:
    """Detect an available sandbox tool. Prefers bwrap, falls back to firejail."""
    for tool in ("bwrap", "firejail"):
        path = shutil.which(tool)
        if path:
            return {"available": True, "tool": tool, "bin": path}
    return {"available": False, "tool": None, "bin": None}


def _wrap_sandbox(inner_argv: list[str], out_dir: str) -> list[str]:
    """Wrap inner_argv in a sandbox command (bwrap preferred, firejail fallback).

    bwrap policy (verified 2026-05-31): --unshare-net (no network), --ro-bind / /
    (read-only root, which transparently covers the uv-managed interpreter that
    lives outside ~/.venvs), a writable --bind of CAD_OUT, and a tmpfs mask over
    each EXISTING secret dir's realpath. Returns the original argv unchanged-ish
    if no sandbox is available (caller checks sandbox_info first).
    """
    info = sandbox_info()
    if not info["available"]:
        return list(inner_argv)
    home = os.path.expanduser("~")
    seen: set[str] = set()
    if info["tool"] == "bwrap":
        argv = [
            info["bin"],
            "--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        ]
        # mask secret DIRS with tmpfs (only existing dirs — bwrap errors on a
        # tmpfs target that doesn't exist in the new root)
        for d in _SECRET_DIRS:
            rp = os.path.realpath(os.path.join(home, d))
            if rp not in seen and os.path.isdir(rp):
                argv += ["--tmpfs", rp]
                seen.add(rp)
        # mask secret FILES by binding /dev/null over them (tmpfs can't cover a
        # regular file — this is the .netrc bug the review caught)
        for f in _SECRET_FILES:
            rp = os.path.realpath(os.path.join(home, f))
            if rp not in seen and os.path.isfile(rp):
                argv += ["--ro-bind", "/dev/null", rp]
                seen.add(rp)
        argv += ["--bind", out_dir, out_dir, "--chdir", out_dir]
        argv += list(inner_argv)
        return argv
    # firejail fallback — bring to parity: read-only root + blacklist secrets,
    # not just --net=none + whitelist (which leaves $HOME readable).
    argv = [
        info["bin"], "--quiet", "--net=none", "--private-tmp",
        "--read-only=/", f"--read-write={out_dir}", f"--whitelist={out_dir}",
    ]
    for d in _SECRET_DIRS:
        rp = os.path.realpath(os.path.join(home, d))
        if rp not in seen and os.path.isdir(rp):
            argv += [f"--blacklist={rp}"]
            seen.add(rp)
    for f in _SECRET_FILES:
        rp = os.path.realpath(os.path.join(home, f))
        if rp not in seen and os.path.isfile(rp):
            argv += [f"--blacklist={rp}"]
            seen.add(rp)
    argv += list(inner_argv)
    return argv


# ---- prompt-derived geometric spec (CADTests pattern) -----------------------
# Turn an NL prompt into a machine-checkable spec the numeric gate can evaluate.
# Deterministic on purpose: an LLM can emit the same JSON shape, but the
# *contract* is unit-testable offline and never blocks on a network/key. This is
# the executable-assertion idea from CADTests — geometric tests, not vision.

# "40x30x20", "40 x 30 x 20", "40mm x 30 mm x 20mm", "40 by 30 by 8"
_BBOX_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*(?:x|×|by)\s*"
    r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*(?:x|×|by)\s*"
    r"(\d+(?:\.\d+)?)\s*(?:mm)?",
    re.IGNORECASE,
)

# number-words 0..12 for "three holes" style counts
_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
}
_NUM_WORD_ALT = (r"a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
                 r"eleven|twelve")

# "through hole", "through-hole", "thru hole", "mounting hole(s)", "bore" — all
# imply a hole that passes through the body (adds genus). "blind hole" does NOT.
# The hole keyword is \b-anchored on BOTH sides so substrings like "whole" can
# never match; an optional count (digits or a number-word) may precede it.
_THROUGH_HOLE_RE = re.compile(
    r"(?:\b(\d+)|\b(" + _NUM_WORD_ALT + r"))?"
    r"[\s-]*"
    # optional size spec between the count and the hole noun: a metric screw
    # callout ("4 M3 mounting holes") or a bare diameter ("4 5mm holes",
    # "2 Ø6 bores"). Non-capturing so the count groups (1/2) are untouched.
    r"(?:(?:M\d+(?:x[\d.]+)?|ø\s*[\d.]+|[\d.]+\s*mm)[\s-]*)?"
    r"(?:through|thru|mounting|clearance|counterbored|countersunk|cbore|csk|bolt|drilled)?"
    r"[\s-]*(?:through[\s-]*|thru[\s-]*)?"
    r"\b(?:through[\s-]*hole|thru[\s-]*hole|bore|hole)s?\b",
    re.IGNORECASE,
)
_BLIND_RE = re.compile(r"\bblind\s+(?:hole|bore)s?\b", re.IGNORECASE)
# Negations: "hole-free", "holeless", "no holes", "without holes", "zero holes".
_NO_HOLE_RE = re.compile(
    r"\bhole[\s-]*free\b|\bholeless\b|"
    r"\b(?:no|without|zero)\s+(?:through[\s-]*|thru[\s-]*|mounting\s+|clearance\s+)?holes?\b",
    re.IGNORECASE,
)
_ASSEMBLY_RE = re.compile(
    r"\b(?:assembly|two[\s-]*part|multi[\s-]*part|snap[\s-]*fit|"
    r"(\d+)[\s-]*(?:part|piece)s?)\b",
    re.IGNORECASE,
)
# Internal geometry that the standard front/top/iso views can't show — warrants a
# section/cutaway render (ADR-0009). Word-anchored so "channel", "cavity" etc.
# match but "manifold"/unrelated words don't. "hollow" is included (shelled part).
_INTERNAL_RE = re.compile(
    r"\b(?:internal|inner|hollow(?:ed)?|cavity|cavities|bore|bores|"
    r"channel|channels|passage|passages|duct|ducts|cooling\s+(?:channel|passage)|"
    r"lumen|conduit)\b",
    re.IGNORECASE,
)

# ---- SPEC CONTRACT v2 keyword detection -------------------------------------
# Part-class -> coordinate-frame origin. The class decides where (0,0,0) sits so
# the model places features against a frame it declared up front (the guard
# against "supposedly correct but deformed" output):
#   plate/bracket/flange/...   -> footprint_center  (flat parts: centre the base)
#   cylinder/shaft/disc/...     -> axis              (axisymmetric: centre the axis)
#   enclosure/box/cube/default -> center             (volumetric: centre the body)
_PLATE_RE = re.compile(
    r"\b(?:plate|bracket|flange|gusset|tab|shim|panel|lid|cover|baseplate|"
    r"base[\s-]*plate|mounting[\s-]*plate)s?\b",
    re.IGNORECASE,
)
_AXIS_RE = re.compile(
    r"\b(?:cylinder|cylindrical|shaft|axle|rod|pin|disc|disk|ring|washer|"
    r"spacer|bushing|bush|sleeve|tube|pipe|pulley|axisymmetric|"
    r"revolved?|of\s+revolution)s?\b",
    re.IGNORECASE,
)
# A bare diameter (Ø / "diameter" / "dia") implies an axisymmetric part too.
_DIAMETER_RE = re.compile(r"\b(?:diameter|dia\.?|radius|ø)\b", re.IGNORECASE)

# Feature keywords for the structured `features` list + the plan narrative.
_SHELL_RE = re.compile(r"\b(?:shell(?:ed)?|hollow(?:ed)?|wall[\s-]*thickness)\b",
                       re.IGNORECASE)
_FILLET_RE = re.compile(r"\b(?:fillet(?:ed|s)?|round(?:ed)?(?:\s+(?:edge|corner)s?)?)\b",
                        re.IGNORECASE)
_CHAMFER_RE = re.compile(r"\b(?:chamfer(?:ed|s)?|bevel(?:led|ed|s)?)\b", re.IGNORECASE)

# Metric screw callouts -> clearance-hole diameter (a documented assumption).
_METRIC_SCREW_RE = re.compile(r"\bM(3|4|5)\b")
_M_CLEARANCE_MM = {"3": 3.4, "4": 4.5, "5": 5.5}

# Symmetry keywords that warrant a symmetry block even without a hole count.
_SYMMETRIC_RE = re.compile(
    r"\b(?:symmetric(?:al)?|mirrored|mirror|4[\s-]*corner|four[\s-]*corner)\b",
    re.IGNORECASE,
)

DEFAULT_TOL_MM = 0.5


def _part_class_origin(low: str) -> str:
    """Pick the coordinate-frame origin from the part class (SPEC CONTRACT v2).

    plate/bracket/flange -> 'footprint_center'; cylinder/shaft/disc/diameter ->
    'axis'; enclosure/box/cube/default -> 'center'. Plate wins over axis when
    both match (a flat 'disc plate' reads as a plate); axis wins over the
    default. Pure string check — deterministic, stdlib-only.
    """
    if _PLATE_RE.search(low):
        return "footprint_center"
    if _AXIS_RE.search(low) or _DIAMETER_RE.search(low):
        return "axis"
    return "center"


def derive_spec(prompt: str) -> dict[str, Any]:
    """Deterministically extract a machine-checkable spec from an NL prompt.

    Returns a dict consumable by measure.py's gate(). Keys are only included
    when the prompt warrants them (so the gate evaluates only what was asked):
      bbox_mm, tol_mm           — overall dimensions (sorted-compared, ± tol)
      through_holes             — count of holes that pass through the body
      watertight                — default True (printable/manifold)
      max_shells / shells       — connected-body count
      prompt                    — echoed for traceability/vision-gate context
    """
    text = prompt.strip()
    low = text.lower()
    spec: dict[str, Any] = {"prompt": text, "watertight": True, "tol_mm": DEFAULT_TOL_MM}

    m = _BBOX_RE.search(text)
    if m:
        spec["bbox_mm"] = [float(m.group(1)), float(m.group(2)), float(m.group(3))]

    # Through-holes. Skip entirely if the prompt NEGATES holes ("hole-free",
    # "no holes"). Blind holes add no genus, so STRIP blind-hole phrases first
    # (rather than skipping the whole branch) — that way "two blind holes and one
    # through hole" still counts the genuine through-hole. Then look for a
    # through/mounting hole phrase with an optional leading count
    # ("4 mounting holes", "three through-holes", "12-hole flange").
    if not _NO_HOLE_RE.search(low):
        low_through = _BLIND_RE.sub(" ", low)  # drop blind-hole phrases
        hm = _THROUGH_HOLE_RE.search(low_through)
        if hm:
            num, word = hm.group(1), hm.group(2)
            if num:
                count = int(num)
            elif word:
                count = _NUM_WORDS.get(word.lower(), 1)
            else:
                count = 1  # bare "hole" / "...with a hole"
            spec["through_holes"] = count

    # Multi-body intent relaxes the single-shell default. A numeric "N part(s)"
    # count is taken literally (so "1 part" stays single-body); the bare keyword
    # forms (assembly / two-part / multi-part / snap-fit) imply at least 2.
    am = _ASSEMBLY_RE.search(low)
    if am and am.group(1):
        spec["max_shells"] = max(1, int(am.group(1)))
    elif am:
        spec["max_shells"] = 2
    else:
        spec["max_shells"] = 1

    # Internal-feature hint (ADR-0009): a part with hidden internal geometry
    # warrants a section/cutaway render so the vision gate can see it.
    if _INTERNAL_RE.search(low):
        spec["internal_features"] = True

    # ---- SPEC CONTRACT v2 (additive) ----------------------------------------
    # units: a constant default the gate / agent can rely on being present.
    spec["units"] = "mm"

    # intent: the cleaned prompt — the core noun phrase the user asked for. This
    # is the over-delivery guard ("make a mug, not a vessel"): the model must
    # build exactly THIS, not a fancier superset. Deterministic = the prompt with
    # collapsed whitespace.
    intent = re.sub(r"\s+", " ", text).strip()
    spec["intent"] = intent

    # coordinate_frame: declare the frame BEFORE codegen so every feature is
    # placed against a known origin (prevents "looks right but deformed"). Origin
    # by part class; base_plane/up_axis are the CadQuery convention.
    origin = _part_class_origin(low)
    spec["coordinate_frame"] = {
        "origin": origin, "base_plane": "XY", "up_axis": "+Z",
    }

    # features: a STRUCTURED list derived from what was already detected, so the
    # plan and gate share one vocabulary. Each entry is {name, type, count}.
    features: list[dict[str, Any]] = []
    th = spec.get("through_holes")
    if th:
        features.append({"name": "through_holes", "type": "through_hole", "count": int(th)})
    if spec.get("internal_features"):
        features.append({"name": "internal_feature", "type": "internal_feature", "count": 1})
    if _SHELL_RE.search(low):
        features.append({"name": "shell", "type": "shell", "count": 1})
    if _FILLET_RE.search(low):
        features.append({"name": "fillet", "type": "fillet", "count": 1})
    if _CHAMFER_RE.search(low):
        features.append({"name": "chamfer", "type": "chamfer", "count": 1})
    spec["features"] = features

    # assumptions: the inferred defaults made explicit so the agent (and a human
    # reviewer) can see what was filled in. units + origin are always recorded;
    # metric-screw callouts add the clearance-hole diameter convention.
    assumptions: list[str] = [
        "units assumed mm",
        f"origin at {origin}",
    ]
    seen_screws: set[str] = set()
    for sm in _METRIC_SCREW_RE.finditer(text):
        size = sm.group(1)
        if size in seen_screws:
            continue
        seen_screws.add(size)
        assumptions.append(
            f"M{size} clearance hole = {_M_CLEARANCE_MM[size]:g} mm"
        )
    spec["assumptions"] = assumptions

    # symmetry: emit when count>1 holes OR an explicit symmetry keyword. `mirror`
    # lists the planes the part should be symmetric about; for a hole pattern a
    # rectangular footprint is mirror-symmetric about both X and Y.
    sym_keyword = bool(_SYMMETRIC_RE.search(low))
    if (th and th > 1) or sym_keyword:
        spec["symmetry"] = {
            "mirror": ["x", "y"],
            "count": int(th) if (th and th > 1) else 1,
        }

    return spec


def spec_from_prompt(prompt: str, out_path: str | None = None) -> dict[str, Any]:
    """Tool-facing wrapper around derive_spec.

    Returns {success, spec, spec_path?}. If out_path is given, the spec JSON is
    written there so it can be fed straight to cad_measure --spec.
    """
    spec = derive_spec(prompt)
    written = None
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(spec, indent=2) + "\n")
        written = str(p)
    return {"success": True, "spec": spec, "spec_path": written}


# ---- explicit CoT modeling plan (CAD-Coder pattern) -------------------------
# An optional, deterministic plan step before codegen: primitives -> operations
# -> features -> export. CAD-Coder shows an explicit CoT plan improves codegen
# validity. core.plan is a SCAFFOLD seeded from derive_spec (so the plan carries
# the SAME spec the numeric gate will assert — plan↔gate coherence); the agent
# expands operations/features into concrete CadQuery before cad_generate. Kept
# LLM-free and unit-testable offline, exactly like derive_spec / iterate.

def plan(prompt: str) -> dict[str, Any]:
    """Deterministic modeling plan scaffold from an NL prompt (ADR-0007).

    Returns {prompt, spec, primitives, operations, features, export, notes}.
    Deterministic by design — the agent fills in operations/features with real
    CadQuery calls. Reuses derive_spec so the plan and the numeric gate agree.
    """
    spec = derive_spec(prompt)

    primitives: list[str] = []
    bbox = spec.get("bbox_mm")
    if bbox:
        l, w, h = bbox
        primitives.append(
            f"box {l:g}x{w:g}x{h:g} mm "
            f"(cq.Workplane('XY').box({l:g}, {w:g}, {h:g}))"
        )
    else:
        primitives.append("base solid — dimensions unspecified; pick from the prompt")

    # Coordinate frame, echoed from the spec and stated up front: the model must
    # enumerate the frame + each feature's PLACEMENT relative to it BEFORE
    # emitting code. This ordering is what prevents output that's numerically
    # plausible but geometrically deformed/misplaced.
    frame = spec.get("coordinate_frame", {})
    origin = frame.get("origin", "center")

    features: list[str] = []
    th = spec.get("through_holes")
    if th:
        plural = "s" if th != 1 else ""
        # state HOW the holes sit relative to the declared frame.
        if th > 1:
            placement = (
                f"place the {th} hole{plural} as a symmetric pattern about the "
                f"{origin} (mirror across X and Y) so the part is balanced"
            )
        else:
            placement = f"place the hole on-axis through the body about the {origin}"
        features.append(
            f"through_hole x{th}: {th} hole{plural} passing fully through the body "
            f"(.faces(...).workplane().hole(d) or .cutThruAll()); {placement}; "
            f"numeric gate asserts through_holes == {th}"
        )
    if spec.get("max_shells", 1) > 1:
        features.append(
            f"multi-body: up to {spec['max_shells']} connected bodies "
            f"(assembly/snap-fit); gate asserts n_shells <= {spec['max_shells']}"
        )
    if spec.get("internal_features"):
        features.append(
            "internal feature (channel/bore/cavity) — request a SECTION render so "
            "the vision gate can see the hidden geometry"
        )
    for feat in spec.get("features", []):
        if feat["type"] in ("shell", "fillet", "chamfer"):
            features.append(
                f"{feat['type']}: realize relative to the {origin} frame "
                f"(keep wall/radius < the adjacent dimension or the OCC kernel fails)"
            )

    # claims_require: every claim the agent might make is bound to the TOOL whose
    # result legitimises it — the "never claim done unless the tool ran" rule.
    claims_require = {
        "watertight": "cad_measure",
        "bbox": "cad_measure",
        "through_holes": "cad_measure",
        "shells": "cad_measure",
        "manifold": "cad_measure",
        "symmetry": "cad_measure",
        "render_exists": "cad_render",
        "looks_right": "cad_review",
    }

    # repair_classes: map a LIKELY gate failure to the SMALLEST responsible fix,
    # so a failed gate routes to a targeted edit instead of a rewrite. Ordered,
    # deterministic.
    repair_classes = [
        {"failure": "bbox mismatch",
         "fix": "scale the offending dimension parameter (do not rebuild the part)"},
        {"failure": "missing through_hole",
         "fix": "add/keep the .cutThruAll() / .hole(d) for the missing hole"},
        {"failure": "non-manifold (not is_volume)",
         "fix": "fix the boolean / wall thickness so the solid is watertight"},
        {"failure": "over-large fillet/chamfer (OCC StdFail_NotDone)",
         "fix": "reduce the fillet/chamfer radius below the adjacent edge length"},
        {"failure": "too many shells",
         "fix": "union the disconnected bodies (or remove the stray solid)"},
        {"failure": "symmetry residual",
         "fix": "centre the feature pattern on the declared origin / mirror plane"},
    ]

    export = [
        "part.stl  (mesh — render + numeric gate + 3D printing)",
        "part.step (B-rep — manufacturing; CadQuery only, OpenSCAD is mesh-only)",
    ]

    notes = (
        "Agent: expand `operations` into concrete CadQuery calls (booleans, "
        "shell, fillet, patterns) that realize the `features`, then call "
        "cad_generate. Keep dimensions consistent with `spec` so the numeric "
        "gate passes. State the `coordinate_frame` + each feature's placement "
        "FIRST, never claim a property without the tool in `claims_require`, and "
        "on a gate failure apply the smallest `repair_classes` fix. "
        "`operations` is intentionally empty — it's yours to fill."
    )

    return {
        "prompt": prompt,
        "spec": spec,
        "coordinate_frame": frame,
        "primitives": primitives,
        "operations": [],   # agent fills: booleans / shell / fillet / patterns
        "features": features,
        "claims_require": claims_require,
        "repair_classes": repair_classes,
        "export": export,
        "notes": notes,
    }


def plan_from_prompt(prompt: str, out_path: str | None = None) -> dict[str, Any]:
    """Tool-facing wrapper around plan(). Optionally writes plan.json."""
    p = plan(prompt)
    written = None
    if out_path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(p, indent=2) + "\n")
        written = str(path)
    return {"success": True, "plan": p, "plan_path": written}


# ---- ReAct error-feedback loop ----------------------------------------------
# When CadQuery raises (bad fillet radius, OCC kernel error, non-manifold), the
# traceback is the most valuable signal for the next attempt. summarize_error
# turns it into a structured observation; iterate() assembles the next-attempt
# context; generate_with_retry() drives the loop. All LLM-free: the *agent* is
# the caller-supplied code_fn(observation) -> code, so the loop is deterministic
# and unit-testable without a network/key.

# A small map from a kernel/exception signature to an actionable hint. The OCC
# kernel reports nearly every B-rep failure as the same opaque message, so the
# triggering op matters more than the type.
_ERROR_HINTS = [
    ("StdFail_NotDone",
     "OCC kernel could not complete the operation. Common cause: a fillet/chamfer "
     "radius larger than the adjacent edge/face — reduce the radius or fillet fewer "
     "edges. Also check shell thickness vs wall size."),
    ("BRep_API: command not done",
     "OCC B-rep operation failed. Often a too-large fillet/chamfer radius or an "
     "invalid shell thickness. Reduce the radius/thickness and retry."),
    ("Standard_ConstructionError",
     "Invalid geometric construction (e.g. zero/negative dimension, degenerate "
     "sketch). Check that all dimensions are positive and the sketch is closed."),
    ("StdFail_NotDoneError",
     "OCC operation incomplete — usually an out-of-range fillet/chamfer radius."),
    ("no faces", "A selector matched no faces — check the .faces()/.edges() selector string."),
    ("ModuleNotFoundError",
     "A required module is missing from the CAD venv. Use only cadquery (and stdlib)."),
    ("NameError",
     "An undefined name — make sure to `import cadquery as cq` and define all variables."),
    ("SyntaxError", "The generated code has a Python syntax error — fix it and retry."),
]

_PY_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Fail[A-Za-z_]*)):?\s*(.*)$")


def summarize_error(stderr: str | None) -> dict[str, Any] | None:
    """Parse a Python/OCC traceback into a structured ReAct observation.

    Returns {error_type, message, hint, raw} or None when there's no error text.
    The hint maps opaque kernel failures (e.g. OCC's "BRep_API: command not
    done") to actionable guidance the next generate attempt can act on.
    """
    if not stderr or not stderr.strip():
        return None
    lines = [ln.rstrip() for ln in stderr.strip().splitlines() if ln.strip()]
    # The final non-empty line of a traceback is the exception line.
    last = lines[-1]

    error_type = "Error"
    message = last
    m = _PY_EXC_RE.match(last)
    if m:
        error_type = m.group(1)
        message = m.group(2) or last
        # Keep the dotted leaf for OCC types like OCP.OCP.StdFail.StdFail_NotDone
        if "." in error_type:
            error_type = error_type.split(".")[-1]
        # The full last line carries the kernel message ("BRep_API: command not done").
        if m.group(2):
            message = last.split(":", 1)[1].strip() if ":" in last else m.group(2)

    hint = ""
    haystack = stderr
    for needle, h in _ERROR_HINTS:
        if needle in haystack:
            hint = h
            break

    return {
        "error_type": error_type,
        "message": message,
        "hint": hint,
        "raw": stderr.strip()[-2000:],
    }


def iterate(prompt: str, history: list[dict] | None = None,
            last_error: Any = None) -> dict[str, Any]:
    """Assemble the structured observation for the NEXT cad_generate attempt.

    The thin ReAct contract: the agent calls this between attempts to get
    {attempt, spec, history, last_error, instruction}. last_error may be a raw
    stderr string (summarized here) or an already-summarized dict (passed
    through). Pure — no subprocess.
    """
    history = list(history or [])
    attempt = len(history) + 1

    if isinstance(last_error, str):
        err = summarize_error(last_error)
    else:
        err = last_error  # already-summarized dict, or None

    if err:
        instruction = (
            "The previous attempt FAILED. Fix the error below and regenerate the "
            "CadQuery code. Do not repeat the same mistake.\n"
            f"  error: {err.get('error_type')}: {err.get('message')}\n"
            f"  hint:  {err.get('hint')}"
        )
    else:
        instruction = (
            "Generate CadQuery code that satisfies the spec. Export <stem>.stl and "
            "<stem>.step into os.environ['CAD_OUT']."
        )

    return {
        "attempt": attempt,
        "prompt": prompt,
        "spec": derive_spec(prompt),
        "history": history,
        "last_error": err,
        "instruction": instruction,
    }


def generate_with_retry(code_fn, prompt: str, out_dir: str | None = None,
                        stem: str = "part", max_iters: int = 3) -> dict[str, Any]:
    """Drive the ReAct loop: regenerate with the error in context until success.

    code_fn(observation) -> CadQuery source is the agent: it receives the
    iterate() observation (including the summarized last_error) and returns the
    next code to try. Returns {success, attempts, history, result} where result
    is the final generate() dict.
    """
    history: list[dict] = []
    last_error: Any = None
    result: dict[str, Any] = {}

    for _ in range(max(1, max_iters)):
        obs = iterate(prompt, history=history, last_error=last_error)
        code = code_fn(obs)
        result = generate(code=code, out_dir=out_dir, stem=stem)
        record = {
            "attempt": obs["attempt"],
            "success": bool(result.get("success")),
            "error": summarize_error(result.get("stderr")) if not result.get("success") else None,
        }
        history.append(record)
        if result.get("success"):
            break
        last_error = result.get("stderr")
        # reuse the same out_dir across attempts so artifacts land together
        out_dir = out_dir or result.get("out_dir")

    return {
        "success": bool(result.get("success")),
        "attempts": len(history),
        "history": history,
        "result": result,
    }


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    # Scrubbed allowlist env (ADR-0001) — never inherit the full os.environ into
    # a CAD subprocess. render.py auto-sets DISPLAY=:0 from WSLg; DISPLAY is on
    # the allowlist so an already-set one passes through.
    env = _scrubbed_env()
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def venv_ready() -> bool:
    py = cad_venv_python()
    if not Path(py).exists():
        return False
    r = _run([py, "-c", "import cadquery,trimesh,scipy,vtk; print('ok')"], timeout=60)
    return r.returncode == 0 and "ok" in r.stdout


def export_artifacts(stl: str, step: str | None = None, out_dir: str | None = None,
                     stem: str = "part", emit_glb: bool = True,
                     source_hash: str | None = None) -> dict[str, Any]:
    """Emit the GLB + topology sidecar next to an STL (ADR-0013).

    Shells out to scripts/export_artifacts.py in the CAD venv: writes
    <stem>.topology.json (always) and <stem>.glb (unless an agent-authored one
    already exists, or emit_glb=False). Returns {success, glb, topology}. Never
    raises — a failed export degrades to {success: False} so it can't sink a
    successful generate.
    """
    py = cad_venv_python()
    script = SCRIPTS / "export_artifacts.py"
    if not Path(py).exists() or not script.exists():
        return {"success": False, "glb": None, "topology": None}
    args = [py, str(script), str(stl)]
    if step:
        args += ["--step", str(step)]
    if not emit_glb:
        args.append("--no-glb")
    if source_hash:
        args += ["--source-hash", source_hash]
    try:
        r = _run(args, timeout=180)
    except Exception:
        logger.warning("export_artifacts: subprocess failed", exc_info=True)
        return {"success": False, "glb": None, "topology": None}
    topo = r.stdout.strip().splitlines()[-1].strip() if (r.returncode == 0 and r.stdout.strip()) else None
    out_dir = out_dir or os.path.dirname(os.path.abspath(stl))
    # The script names artifacts from the STL basename — derive the GLB path the
    # SAME way (not from `stem`), so a caller passing a mismatched stem still finds
    # the file the script actually wrote (review LOW finding). In generate() stem
    # always equals the STL basename, so this is a no-op there.
    art_stem = os.path.splitext(os.path.basename(stl))[0]
    glb = os.path.join(out_dir, f"{art_stem}.glb")
    glb_ok = os.path.exists(glb) and os.path.getsize(glb) > 0
    return {
        "success": r.returncode == 0 and bool(topo) and os.path.exists(topo),
        "glb": glb if glb_ok else None,
        "topology": topo if (topo and os.path.exists(topo)) else None,
    }


def generate(code: str, out_dir: str | None = None, stem: str = "part",
             backend: str = "cadquery", emit_glb: bool = True) -> dict[str, Any]:
    """Execute user-supplied modeling code and export a mesh.

    backend="cadquery" (default): runs the code as Python in the CAD venv; it
    should export <stem>.stl and <stem>.step into CAD_OUT. backend="openscad":
    treats `code` as SCAD source and runs the openscad binary -> <stem>.stl
    (mesh-only, no STEP — ADR-0008). On a successful build, also emits a
    <stem>.glb (PBR render input) + <stem>.topology.json sidecar (numeric-gate
    ground truth) unless emit_glb=False (ADR-0013). Returns {success, backend,
    out_dir, stl, step, glb, topology, stdout, stderr}.
    """
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="cad_"))
    out.mkdir(parents=True, exist_ok=True)

    if backend == "openscad":
        return _generate_openscad(code, out, stem, emit_glb=emit_glb)
    if backend != "cadquery":
        return {
            "success": False, "backend": backend, "out_dir": str(out),
            "stl": None, "step": None, "stdout": "",
            "error": f"unknown backend {backend!r} (use 'cadquery' or 'openscad')",
            "stderr": f"unknown backend {backend!r}",
        }

    py = cad_venv_python()

    # Layer-1 static safety pre-check (ADR-0003): reject obvious exfil/abuse
    # BEFORE executing model-authored code. Defense-in-depth — the scrubbed env
    # (ADR-0001) and opt-in sandbox (ADR-0002) are the real boundaries. This is
    # Python-specific, so it only applies to the cadquery backend.
    check = safety.check_code(code)
    if not check["ok"]:
        logger.warning("cad_generate: rejected unsafe code: %s", check["violations"])
        return {
            "success": False,
            "backend": "cadquery",
            "rejected": True,
            "violations": check["violations"],
            "out_dir": str(out),
            "stl": None,
            "step": None,
            "stdout": "",
            "stderr": "rejected by AST safety pre-check: " + "; ".join(check["violations"]),
        }

    script = out / f"{stem}_gen.py"
    script.write_text(code)
    # Scrubbed allowlist env + CAD_OUT (ADR-0001). This is the chokepoint that
    # runs MODEL-AUTHORED code, so it must never see OPENROUTER_API_KEY or any
    # other parent secret. CAD_OUT is the one var we inject.
    env = _scrubbed_env({"CAD_OUT": str(out)})

    # Opt-in OS sandbox (ADR-0002). Off by default (trusted use). When
    # HERMES_CAD_SANDBOX=1, wrap in bwrap/firejail; if requested but no tool is
    # available, WARN and run unsandboxed (degrade, not fail).
    inner = [py, str(script)]
    sandbox_state = "off"
    argv = inner
    if os.environ.get("HERMES_CAD_SANDBOX"):
        info = sandbox_info()
        if info["available"]:
            argv = _wrap_sandbox(inner, out_dir=str(out))
            sandbox_state = info["tool"]
        else:
            logger.warning(
                "HERMES_CAD_SANDBOX=1 set but no sandbox tool (bwrap/firejail) "
                "found — running generated code UNSANDBOXED. Install bubblewrap "
                "for OS-level confinement of untrusted code."
            )
            sandbox_state = "requested-unavailable"

    # cwd=out so scripts that export with bare local names still land in CAD_OUT.
    r = subprocess.run(argv, capture_output=True, text=True,
                       timeout=300, env=env, cwd=str(out))
    stl = out / f"{stem}.stl"
    step = out / f"{stem}.step"
    stl_ok = stl.exists() and stl.stat().st_size > 0
    success = r.returncode == 0 and stl_ok
    result: dict[str, Any] = {
        "success": success,
        "backend": "cadquery",
        "sandbox": sandbox_state,
        "out_dir": str(out),
        "stl": str(stl) if stl_ok else None,
        "step": str(step) if (step.exists() and step.stat().st_size > 0) else None,
        "glb": None,
        "topology": None,
        "stdout": r.stdout[-2000:],
        "stderr": r.stderr[-3000:],
    }
    # Complete the generator contract (ADR-0013): emit GLB + topology sidecar from
    # the freshly-built STL. Best-effort — a failed export never sinks a good build.
    if success and emit_glb:
        art = export_artifacts(str(stl), step=result["step"], out_dir=str(out),
                               stem=stem, source_hash=_source_hash(code))
        result["glb"] = art.get("glb")
        result["topology"] = art.get("topology")
    return result


def _source_hash(code: str) -> str:
    """sha256 of the generator source — provenance for the topology sidecar so a
    stale sidecar can be detected (a tamper-evident model->artifact link)."""
    import hashlib
    return hashlib.sha256(code.encode("utf-8", "ignore")).hexdigest()


def _generate_openscad(code: str, out: Path, stem: str,
                       emit_glb: bool = True) -> dict[str, Any]:
    """OpenSCAD backend (ADR-0008): write .scad, run `openscad -o <stem>.stl`.

    Mesh-only (no STEP). The Python AST denylist does NOT apply (SCAD isn't
    Python); the scrubbed env (ADR-0001) and opt-in sandbox (ADR-0002) still do.
    On a successful build, also emits a GLB + topology sidecar (ADR-0013) unless
    emit_glb=False. Returns a clear error (no crash) when no openscad binary is found.
    """
    binary = openscad_bin()
    if not binary:
        msg = ("openscad backend requested but no binary found — set "
               "HERMES_OPENSCAD_BIN, put `openscad` on PATH, or extract an "
               "AppImage (`openscad.AppImage --appimage-extract`).")
        logger.warning("cad_generate(openscad): %s", msg)
        return {
            "success": False, "backend": "openscad", "out_dir": str(out),
            "stl": None, "step": None, "stdout": "", "error": msg, "stderr": msg,
        }

    scad = out / f"{stem}.scad"
    scad.write_text(code)
    stl = out / f"{stem}.stl"
    inner = [binary, "-o", str(stl), str(scad)]

    # same opt-in sandbox + scrubbed env as the cadquery path
    env = _scrubbed_env({"CAD_OUT": str(out)})
    sandbox_state = "off"
    argv = inner
    if os.environ.get("HERMES_CAD_SANDBOX"):
        info = sandbox_info()
        if info["available"]:
            argv = _wrap_sandbox(inner, out_dir=str(out))
            sandbox_state = info["tool"]
        else:
            logger.warning("HERMES_CAD_SANDBOX=1 set but no sandbox tool found — "
                           "running openscad UNSANDBOXED.")
            sandbox_state = "requested-unavailable"

    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=300, env=env, cwd=str(out))
    except Exception as e:
        logger.warning("cad_generate(openscad): subprocess failed: %s", e)
        return {"success": False, "backend": "openscad", "out_dir": str(out),
                "stl": None, "step": None, "stdout": "", "error": repr(e),
                "stderr": repr(e)}

    stl_ok = stl.exists() and stl.stat().st_size > 0
    success = r.returncode == 0 and stl_ok
    result: dict[str, Any] = {
        "success": success,
        "backend": "openscad",
        "sandbox": sandbox_state,
        "out_dir": str(out),
        "stl": str(stl) if stl_ok else None,
        "step": None,   # OpenSCAD is mesh-only (ADR-0008)
        "glb": None,
        "topology": None,
        "stdout": r.stdout[-2000:],
        "stderr": r.stderr[-3000:],
    }
    if success and emit_glb:
        art = export_artifacts(str(stl), step=None, out_dir=str(out),
                               stem=stem, source_hash=_source_hash(code))
        result["glb"] = art.get("glb")
        result["topology"] = art.get("topology")
    return result


def _parse_backend(stderr: str) -> str:
    """Lift `backend=<name>` from render.py's stderr (vtk / vtk-osmesa / matplotlib)."""
    import re as _re
    m = _re.search(r"backend=([\w-]+)", stderr)
    return m.group(1) if m else "?"


def _osmesa_capable() -> bool:
    """Ask the CAD venv whether its VTK has an OSMesa/EGL offscreen render window
    class (a software-GL build that renders headless with no display)."""
    py = cad_venv_python()
    if not Path(py).exists():
        return False
    probe = ("import vtk,sys; "
             "sys.exit(0 if any(hasattr(vtk,c) for c in "
             "('vtkOSOpenGLRenderWindow','vtkEGLRenderWindow')) else 1)")
    try:
        r = _run([py, "-c", probe], timeout=60)
        return r.returncode == 0
    except Exception:
        logger.warning("headless GL: osmesa capability probe failed", exc_info=True)
        return False


def headless_gl_info(libs_ok: bool = True) -> dict[str, Any]:
    """Report which headless render path is available (ADR-0004), for cad doctor.

    path is one of: 'display' (an X display is set/WSLg), 'osmesa' (VTK has a
    software-GL window), 'xvfb' (Xvfb present + /tmp/.X11-unix writable), or
    'matplotlib-only' (no real-GL path — renders work but at lower fidelity).
    """
    display = os.environ.get("DISPLAY") or (":0" if Path("/tmp/.X11-unix/X0").exists() else None)
    if display:
        return {"path": "display", "detail": f"X display ({display}) — VTK GLX"}
    if libs_ok and _osmesa_capable():
        return {"path": "osmesa", "detail": "VTK has OSMesa/EGL offscreen GL (no display needed)"}
    x11 = Path("/tmp/.X11-unix")
    xvfb = shutil.which("Xvfb")
    x11_writable = x11.is_dir() and os.access(str(x11), os.W_OK)
    if xvfb and x11_writable:
        return {"path": "xvfb",
                "detail": f"Xvfb ({xvfb}) + writable /tmp/.X11-unix — software GLX on demand"}
    detail = ("no real-GL path — renders fall back to matplotlib (lower fidelity). "
              "Install an OSMesa VTK build or Xvfb for z-buffered VTK headless")
    if xvfb and not x11_writable:
        detail += " (Xvfb present but /tmp/.X11-unix not writable — e.g. WSL)"
    return {"path": "matplotlib-only", "detail": detail}


def render(stl: str, section: bool | str | None = None) -> dict[str, Any]:
    """Render a 3-view montage PNG (headless). section=True adds a clipping-plane
    cutaway view (auto axis = longest bbox edge); section='x'|'y'|'z' picks the
    axis (ADR-0009). Backend precedence is handled in render.py (ADR-0004)."""
    py = cad_venv_python()
    args = [py, str(SCRIPTS / "render.py"), stl]
    if section:
        args.append("--section")
        if isinstance(section, str) and section in ("x", "y", "z"):
            args.append(section)
    r = _run(args, timeout=180)
    montage = r.stdout.strip().splitlines()[-1].strip() if r.returncode == 0 and r.stdout.strip() else None
    return {
        "success": r.returncode == 0 and bool(montage) and Path(montage).exists(),
        "montage": montage,
        "backend": _parse_backend(r.stderr),
        "stderr": r.stderr[-1500:],
    }


def measure(stl: str, spec_path: str | None = None) -> dict[str, Any]:
    py = cad_venv_python()
    args = [py, str(SCRIPTS / "measure.py"), stl]
    if spec_path:
        args += ["--spec", spec_path]
    r = _run(args, timeout=120)
    try:
        report = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        report = {"raw": r.stdout[-2000:], "stderr": r.stderr[-1000:]}
    return {
        "success": r.returncode in (0, 1),  # 0=gate pass, 1=gate fail (both are valid runs)
        "gate_pass": r.returncode == 0,
        "report": report,
    }


_FIX_STOPWORDS = {
    "a", "an", "the", "is", "are", "on", "of", "to", "add", "fix", "missing",
    "should", "be", "in", "it", "and", "with", "this", "that", "part", "from",
    "render", "please", "needs", "need", "make", "there", "no", "not", "for",
    "exactly", "one", "specified", "completely", "passing", "extending",
    "so", "its", "into", "onto", "at", "by", "as",
}

# Canonicalize common feature synonyms so reviewers from different families
# converge: 'add an opening' and 'add a hole' describe the same defect. NOTE
# 'opening' is NOT a stopword (it carries meaning) — it maps to 'hole'.
_FIX_SYNONYMS = {
    "opening": "hole", "openings": "hole", "bore": "hole", "bores": "hole",
    "cutout": "hole", "aperture": "hole", "perforation": "hole",
    "holes": "hole", "slots": "slot", "fillets": "fillet", "chamfers": "chamfer",
}


def _fix_tokens(text: str) -> set[str]:
    """Salient content-word set of a must-fix string, for convergence matching.

    Lowercase, drop punctuation and stopwords, then canonicalize feature
    synonyms (opening/bore -> hole). Two fixes converge when their token sets
    overlap enough (Jaccard) — robust to paraphrase, which real cross-family LLM
    output always is ('add a through hole on the top face' vs 'Add exactly one
    through hole opening on the top face', or 'add an opening' vs 'add a hole').
    """
    import re as _re
    t = _re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return {_FIX_SYNONYMS.get(w, w) for w in t.split()
            if w and w not in _FIX_STOPWORDS}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Two must-fixes from different reviewers are "the same finding" when their
# salient-token sets overlap at least this much. 0.4 clusters genuine paraphrases
# (top-face through-hole findings) without merging unrelated fixes (a hole fix vs
# an orientation fix share ~0 tokens). Erring toward clustering FAILS the gate,
# the safe direction for a verification gate.
_CONVERGE_JACCARD = 0.4


def aggregate_reviews(reviewers: list[dict]) -> dict[str, Any]:
    """Cross-family aggregation of structured reviewer verdicts (ADR-0005).

    A must-fix raised by >=2 reviewers (matched by token-set Jaccard, so
    paraphrases cluster) is CONVERGENT — a hard must-fix, our cross-family edge.
    gate_pass is True only when there are no convergent fixes AND at least one
    reviewer could actually judge (not all cant_tell).
    Returns {must_fix, convergent_must_fix, verdict_counts, gate_pass, n_reviewers}.
    """
    verdict_counts = {"matches": 0, "needs_fixes": 0, "cant_tell": 0}
    # greedy clusters: each is {text, tokens, models:set}
    clusters: list[dict[str, Any]] = []
    all_fixes: list[str] = []

    for rv in reviewers:
        v = rv.get("verdict", "cant_tell")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        model = rv.get("model", "?")
        seen_here: list[set] = []   # de-dup within one reviewer
        for fix in rv.get("must_fix", []):
            if not fix:
                continue
            all_fixes.append(fix)
            toks = _fix_tokens(fix)
            if any(_jaccard(toks, s) >= _CONVERGE_JACCARD for s in seen_here):
                continue
            seen_here.append(toks)
            # attach to the best-matching existing cluster, else start a new one
            best = None
            best_sim = _CONVERGE_JACCARD
            for c in clusters:
                sim = _jaccard(toks, c["tokens"])
                if sim >= best_sim:
                    best, best_sim = c, sim
            if best is not None:
                best["models"].add(model)
            else:
                clusters.append({"text": fix, "tokens": toks, "models": {model}})

    convergent = [c["text"] for c in clusters if len(c["models"]) >= 2]
    # unique must_fix list preserving first-seen order (Jaccard de-dup)
    unique_fixes: list[str] = []
    seen_sets: list[set] = []
    for f in all_fixes:
        toks = _fix_tokens(f)
        if any(_jaccard(toks, s) >= _CONVERGE_JACCARD for s in seen_sets):
            continue
        seen_sets.append(toks)
        unique_fixes.append(f)

    judged = verdict_counts["matches"] + verdict_counts["needs_fixes"]
    gate_pass = (not convergent) and judged > 0
    return {
        "must_fix": unique_fixes,
        "convergent_must_fix": convergent,
        "verdict_counts": verdict_counts,
        "gate_pass": gate_pass,
        "n_reviewers": len(reviewers),
    }


def _parse_reviews_block(stdout: str) -> dict | None:
    """Extract the genuine REVIEWS_JSON {mode, reviewers} block scatter_review emits.

    SECURITY: a reviewer's verbatim reply (printed in the human-readable summary
    BEFORE the genuine block) can itself contain a 'REVIEWS_JSON\\n{...}' to spoof
    the gate FAIL->PASS. scatter_review always prints the genuine marker as its
    own standalone line, LAST, with the JSON on the very next line. So we anchor
    on a line whose stripped value EQUALS the marker and take the LAST such line —
    a mid-line injection fails the equality, and an own-line injection appears
    earlier than the genuine one. (split-on-first is spoofable; rsplit is wrong
    too, since json.dumps embeds the raw reply verbatim into the genuine line.)
    """
    marker = "REVIEWS_JSON"
    lines = stdout.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == marker:
            idx = i  # keep the LAST standalone-marker line (the genuine block)
    if idx is None or idx + 1 >= len(lines):
        return None
    try:
        return json.loads(lines[idx + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def review(montage: str, spec_path: str, models: str | None = None,
           mode: str = "qa") -> dict[str, Any]:
    """Cross-family vision gate. mode='qa' (structured Yes/No-Q&A, CADCodeVerify)
    or 'free' (legacy free-form). Parses the structured reviewer block and
    aggregates cross-family convergence (ADR-0005)."""
    py = cad_venv_python()
    args = [py, str(SCRIPTS / "scatter_review.py"), montage, spec_path, "--mode", mode]
    if models:
        args += ["--models", models]
    r = _run(args, timeout=240)

    block = _parse_reviews_block(r.stdout)
    reviewers = block["reviewers"] if block else []
    aggregate = aggregate_reviews(reviewers) if reviewers else None
    return {
        "success": r.returncode == 0,
        "mode": mode,
        "reviewers": reviewers,
        "aggregate": aggregate,
        "reviews": r.stdout,          # full human-readable text (back-compat)
        "stderr": r.stderr[-1500:],
    }


def compare(generated: str, reference: str, samples: int | None = None,
            seed: int | None = None) -> dict[str, Any]:
    """Chamfer-Distance similarity between a generated and a reference mesh.

    Shells out to scripts/compare.py in the CAD venv. Returns {success,
    chamfer_distance, report}. chamfer_distance is None on a load/compute error
    (exit 2). CD ~ 0 = identical, grows with deformation (ADR-0006).
    """
    py = cad_venv_python()
    args = [py, str(SCRIPTS / "compare.py"), str(generated), str(reference)]
    if samples is not None:
        args += ["--samples", str(samples)]
    if seed is not None:
        args += ["--seed", str(seed)]
    r = _run(args, timeout=180)
    try:
        report = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        report = {"raw": r.stdout[-2000:], "stderr": r.stderr[-1000:]}
    cd = report.get("chamfer_distance") if isinstance(report, dict) else None
    # A NaN/Inf score is not a valid distance — collapse it to the failure
    # contract rather than letting it masquerade as a real measurement.
    if cd is not None and not (isinstance(cd, (int, float)) and math.isfinite(cd)):
        logger.warning("cad_compare: non-finite chamfer_distance %r — treating as failure", cd)
        cd = None
    return {
        "success": r.returncode == 0 and cd is not None,
        "chamfer_distance": cd,
        "report": report,
    }


def doctor() -> dict[str, Any]:
    py = cad_venv_python()
    checks = []

    venv_ok = Path(py).exists()
    checks.append({"check": "cad_venv_exists", "pass": venv_ok, "detail": py})

    libs_ok = venv_ready() if venv_ok else False
    checks.append({"check": "cad_libs_import", "pass": libs_ok,
                   "detail": "cadquery+trimesh+scipy+vtk" if libs_ok else "missing — run `cad setup`"})

    display = os.environ.get("DISPLAY") or (":0" if Path("/tmp/.X11-unix/X0").exists() else None)
    checks.append({"check": "render_display", "pass": bool(display),
                   "detail": f"DISPLAY={display}" if display else "no X display — VTK falls back to matplotlib"})

    # Headless GL path (ADR-0004): which strategy yields a real z-buffered VTK
    # render when there's no display. Passing requires SOME real-GL path
    # (display / osmesa / xvfb); matplotlib-only is a soft fail (works, lower
    # fidelity). Not required for core readiness.
    gl = headless_gl_info(libs_ok)
    checks.append({"check": "render_headless_gl", "pass": gl["path"] != "matplotlib-only",
                   "detail": gl["detail"]})

    has_or_key = _has_openrouter_key()
    checks.append({"check": "vision_gate_key", "pass": has_or_key,
                   "detail": "OPENROUTER_API_KEY present" if has_or_key
                             else "OPTIONAL: set OPENROUTER_API_KEY in ~/.hermes/.env for the multi-model vision gate"})

    scripts_ok = all((SCRIPTS / s).exists() for s in
                     ("render.py", "measure.py", "scatter_review.py",
                      "export_artifacts.py", "placement.py", "cad_helpers.py", "pbr.py"))
    checks.append({"check": "scripts_present", "pass": scripts_ok, "detail": str(SCRIPTS)})

    # Sandbox availability (ADR-0002). Not required for readiness — generated
    # code runs unsandboxed by default — but reported so operators know whether
    # HERMES_CAD_SANDBOX=1 will actually confine untrusted code.
    sb = sandbox_info()
    checks.append({"check": "sandbox_available", "pass": sb["available"],
                   "detail": (f"{sb['tool']} ({sb['bin']}) — set HERMES_CAD_SANDBOX=1 to confine "
                              "generated code"
                              if sb["available"]
                              else "OPTIONAL: install bubblewrap (bwrap) or firejail to sandbox "
                                   "untrusted generated code (HERMES_CAD_SANDBOX=1)")})

    # Optional OpenSCAD backend (ADR-0008). Not required — CadQuery is the
    # default and primary backend — but reported so the agent knows whether
    # cad_generate(backend='openscad') is available.
    oscad = openscad_bin()
    checks.append({"check": "openscad_backend", "pass": bool(oscad),
                   "detail": (f"openscad ({oscad}) — cad_generate backend='openscad' available"
                              if oscad
                              else "OPTIONAL: install openscad (or set HERMES_OPENSCAD_BIN) for "
                                   "the CSG backend; CadQuery is the default")})

    # numeric gate works without display or key; vision gate needs key.
    core_ok = venv_ok and libs_ok and scripts_ok
    return {"ready": core_ok, "vision_ready": core_ok and has_or_key, "checks": checks}


def _has_openrouter_key() -> bool:
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    env_path = Path(os.path.expanduser("~/.hermes/.env"))
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY=") and not line.lstrip().startswith("#"):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                return bool(val)
    return False


def setup(uv_bin: str | None = None) -> dict[str, Any]:
    """Provision the CAD venv with uv (preferred) or venv+pip fallback."""
    venv = DEFAULT_VENV
    uv = uv_bin or shutil.which("uv")
    pkgs = ["cadquery", "trimesh", "scipy", "numpy", "vtk", "matplotlib", "pillow"]
    logs = []
    if uv:
        r1 = subprocess.run([uv, "venv", str(venv), "--python", "3.11"],
                            capture_output=True, text=True, timeout=120)
        logs.append(r1.stdout + r1.stderr)
        r2 = subprocess.run([uv, "pip", "install", "--python", str(venv / "bin" / "python")] + pkgs,
                            capture_output=True, text=True, timeout=900)
        logs.append((r2.stdout + r2.stderr)[-1500:])
        ok = r2.returncode == 0
    else:
        import venv as _venv
        _venv.create(str(venv), with_pip=True)
        py = str(venv / "bin" / "python")
        r = subprocess.run([py, "-m", "pip", "install"] + pkgs,
                           capture_output=True, text=True, timeout=900)
        logs.append((r.stdout + r.stderr)[-1500:])
        ok = r.returncode == 0
    return {"success": ok and venv_ready(), "venv": str(venv), "used_uv": bool(uv), "log": "\n".join(logs)[-2500:]}
