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
