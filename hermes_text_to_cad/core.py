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
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN_DIR / "scripts"
DEFAULT_VENV = Path(os.path.expanduser("~/.venvs/cad"))


def cad_venv_python() -> str:
    """Path to the CAD venv python. Override with HERMES_CAD_PYTHON."""
    override = os.environ.get("HERMES_CAD_PYTHON")
    if override:
        return override
    return str(DEFAULT_VENV / "bin" / "python")


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

# "through hole", "through-hole", "thru hole", "mounting hole(s)", "bore" — all
# imply a hole that passes through the body (adds genus). "blind hole" does NOT.
_THROUGH_HOLE_RE = re.compile(
    r"\b(?:(\d+)|([a-z]+))?[\s-]*"
    r"(?:through|thru|mounting|clearance)?[\s-]*(?:through[\s-]*)?"
    r"(?:hole|bore|bolt[\s-]*hole)s?\b",
    re.IGNORECASE,
)
_BLIND_RE = re.compile(r"\bblind\s+(?:hole|bore)s?\b", re.IGNORECASE)
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

    # Through-holes: count blind holes out, then look for a through/mounting hole
    # phrase with an optional leading count ("4 mounting holes", "three through-holes").
    if not _BLIND_RE.search(low):
        hm = _THROUGH_HOLE_RE.search(low)
        if hm:
            num, word = hm.group(1), hm.group(2)
            count = None
            if num:
                count = int(num)
            elif word and word in _NUM_WORDS:
                count = _NUM_WORDS[word]
            elif word is None:
                count = 1  # "...with a hole" / bare "hole"
            # a bare matched word that isn't a number-word (e.g. "drilled hole")
            # still means at least one hole.
            if count is None:
                count = 1
            spec["through_holes"] = count

    # Multi-body intent relaxes the single-shell default.
    am = _ASSEMBLY_RE.search(low)
    if am:
        n = int(am.group(1)) if am.group(1) else 2
        spec["max_shells"] = max(2, n)
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
    env = dict(os.environ)
    # render.py auto-sets DISPLAY=:0 from WSLg; pass through if already set.
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
    script = out / f"{stem}_gen.py"
    script.write_text(code)
    env = dict(os.environ, CAD_OUT=str(out))
    # cwd=out so scripts that export with bare local names still land in CAD_OUT.
    r = subprocess.run([py, str(script)], capture_output=True, text=True,
                       timeout=300, env=env, cwd=str(out))
    stl = out / f"{stem}.stl"
    step = out / f"{stem}.step"
    stl_ok = stl.exists() and stl.stat().st_size > 0
    return {
        "success": r.returncode == 0 and stl_ok,
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


def review(montage: str, spec_path: str, models: str | None = None) -> dict[str, Any]:
    py = cad_venv_python()
    args = [py, str(SCRIPTS / "scatter_review.py"), montage, spec_path]
    if models:
        args.append(models)
    r = _run(args, timeout=240)
    return {
        "success": r.returncode == 0,
        "reviews": r.stdout,
        "stderr": r.stderr[-1500:],
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
