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


# ---- opt-in OS sandbox for generated code (ADR-0002) ------------------------
# When HERMES_CAD_SANDBOX=1, wrap the generate subprocess in bubblewrap (no
# network, read-only filesystem except CAD_OUT, secret dirs masked with tmpfs).
# firejail is the fallback. Opt-in: absent the flag, generate runs unsandboxed
# (the default trusted-use case). When the flag is set but no sandbox binary
# exists, we WARN and run unsandboxed (degrade, not fail) — a conscious choice
# documented in ADR-0002, surfaced as sandbox="requested-unavailable".

# Home-relative dirs to mask inside the sandbox so generated code can't read
# secrets/keys even though the root is bind-mounted read-only. Masked via tmpfs
# at the RESOLVED realpath (a symlinked ~/.aws -> /mnt/c/... still gets masked).
_SECRET_DIRS = (".hermes", ".ssh", ".aws", ".gnupg", ".config/gcloud",
                ".kube", ".docker", ".netrc")


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
    if info["tool"] == "bwrap":
        argv = [
            info["bin"],
            "--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        ]
        # mask secret dirs (only those whose realpath is an existing dir — bwrap
        # errors on a tmpfs target that doesn't exist in the new root)
        seen: set[str] = set()
        for d in _SECRET_DIRS:
            rp = os.path.realpath(os.path.join(home, d))
            if rp not in seen and os.path.isdir(rp):
                argv += ["--tmpfs", rp]
                seen.add(rp)
        argv += [
            "--bind", out_dir, out_dir,
            "--chdir", out_dir,
        ]
        argv += list(inner_argv)
        return argv
    # firejail fallback
    argv = [
        info["bin"], "--quiet", "--net=none", "--private-tmp",
        f"--whitelist={out_dir}",
    ]
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

DEFAULT_TOL_MM = 0.5


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

    features: list[str] = []
    th = spec.get("through_holes")
    if th:
        plural = "s" if th != 1 else ""
        features.append(
            f"through_hole x{th}: {th} hole{plural} passing fully through the body "
            f"(.faces(...).workplane().hole(d) or .cutThruAll()); "
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

    export = [
        "part.stl  (mesh — render + numeric gate + 3D printing)",
        "part.step (B-rep — manufacturing; CadQuery only, OpenSCAD is mesh-only)",
    ]

    notes = (
        "Agent: expand `operations` into concrete CadQuery calls (booleans, "
        "shell, fillet, patterns) that realize the `features`, then call "
        "cad_generate. Keep dimensions consistent with `spec` so the numeric "
        "gate passes. `operations` is intentionally empty — it's yours to fill."
    )

    return {
        "prompt": prompt,
        "spec": spec,
        "primitives": primitives,
        "operations": [],   # agent fills: booleans / shell / fillet / patterns
        "features": features,
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


def generate(code: str, out_dir: str | None = None, stem: str = "part") -> dict[str, Any]:
    """Execute user-supplied CadQuery code in the CAD venv.

    The code should export <stem>.stl and <stem>.step into CAD_OUT.
    Returns {success, out_dir, stl, step, stdout, stderr}.
    """
    py = cad_venv_python()
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="cad_"))
    out.mkdir(parents=True, exist_ok=True)

    # Layer-1 static safety pre-check (ADR-0003): reject obvious exfil/abuse
    # BEFORE executing model-authored code. Defense-in-depth — the scrubbed env
    # (ADR-0001) and opt-in sandbox (ADR-0002) are the real boundaries.
    check = safety.check_code(code)
    if not check["ok"]:
        logger.warning("cad_generate: rejected unsafe code: %s", check["violations"])
        return {
            "success": False,
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
    return {
        "success": r.returncode == 0 and stl_ok,
        "sandbox": sandbox_state,
        "out_dir": str(out),
        "stl": str(stl) if stl_ok else None,
        "step": str(step) if (step.exists() and step.stat().st_size > 0) else None,
        "stdout": r.stdout[-2000:],
        "stderr": r.stderr[-3000:],
    }


def render(stl: str) -> dict[str, Any]:
    py = cad_venv_python()
    r = _run([py, str(SCRIPTS / "render.py"), stl], timeout=180)
    montage = r.stdout.strip().splitlines()[-1].strip() if r.returncode == 0 and r.stdout.strip() else None
    return {
        "success": r.returncode == 0 and bool(montage) and Path(montage).exists(),
        "montage": montage,
        "backend": "vtk" if "backend=vtk" in r.stderr else ("matplotlib" if "matplotlib" in r.stderr else "?"),
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
    "opening", "so", "its", "into", "onto", "at", "by", "as",
}


def _fix_tokens(text: str) -> set[str]:
    """Salient content-word set of a must-fix string, for convergence matching.

    Lowercase, drop punctuation and stopwords. Two fixes converge when their
    token sets overlap enough (Jaccard) — robust to paraphrase, which real
    cross-family LLM output always is ('add a through hole on the top face' vs
    'Add exactly one through hole opening on the top face').
    """
    import re as _re
    t = _re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return {w for w in t.split() if w and w not in _FIX_STOPWORDS}


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
    """Extract the REVIEWS_JSON {mode, reviewers} block scatter_review emits."""
    marker = "REVIEWS_JSON"
    if marker not in stdout:
        return None
    tail = stdout.split(marker, 1)[1].strip()
    # the JSON is the first line after the marker
    line = tail.splitlines()[0] if tail else ""
    try:
        return json.loads(line)
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

    has_or_key = _has_openrouter_key()
    checks.append({"check": "vision_gate_key", "pass": has_or_key,
                   "detail": "OPENROUTER_API_KEY present" if has_or_key
                             else "OPTIONAL: set OPENROUTER_API_KEY in ~/.hermes/.env for the multi-model vision gate"})

    scripts_ok = all((SCRIPTS / s).exists() for s in ("render.py", "measure.py", "scatter_review.py"))
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
