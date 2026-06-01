"""Wave 3.1 — headless render backend precedence (ADR-0004).

render.choose_backend() is pure (env facts -> strategy name), so the
display->osmesa->xvfb->matplotlib precedence is unit-testable with no GPU,
display, or CAD stack. The crash-safe GL probe (--gl-probe in a child) and the
real Xvfb leg are exercised in test_integration (skipped without the venv).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_render_module():
    """Load scripts/render.py. All heavy imports (vtk/trimesh/matplotlib) are
    inside functions, so the module + its pure helpers import with no CAD stack."""
    path = REPO_ROOT / "scripts" / "render.py"
    spec = importlib.util.spec_from_file_location("cad_render_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def render():
    return load_render_module()


# ---- choose_backend precedence (ADR-0004) ------------------------------------

def test_backend_prefers_existing_display(render):
    # a display always wins (WSLg :0 / real X), even if osmesa/xvfb exist
    assert render.choose_backend(
        has_display=True, osmesa_ok=True, xvfb_path="/usr/bin/Xvfb",
        x11_dir_writable=True) == "display"


def test_backend_osmesa_when_no_display(render):
    # no display but an OSMesa/EGL VTK build -> in-process software GL
    assert render.choose_backend(
        has_display=False, osmesa_ok=True, xvfb_path=None,
        x11_dir_writable=False) == "osmesa"


def test_backend_osmesa_beats_xvfb(render):
    # OSMesa needs no extra process, so it's preferred over Xvfb
    assert render.choose_backend(
        has_display=False, osmesa_ok=True, xvfb_path="/usr/bin/Xvfb",
        x11_dir_writable=True) == "osmesa"


def test_backend_xvfb_when_x11only_vtk_headless(render):
    # the stock-wheel headless path: no display, no osmesa, but Xvfb can bind
    assert render.choose_backend(
        has_display=False, osmesa_ok=False, xvfb_path="/usr/bin/Xvfb",
        x11_dir_writable=True) == "xvfb"


def test_backend_no_xvfb_when_x11_dir_not_writable(render):
    # WSL: Xvfb present but /tmp/.X11-unix not writable -> can't use Xvfb
    assert render.choose_backend(
        has_display=False, osmesa_ok=False, xvfb_path="/usr/bin/Xvfb",
        x11_dir_writable=False) == "matplotlib"


def test_backend_matplotlib_last_resort(render):
    assert render.choose_backend(
        has_display=False, osmesa_ok=False, xvfb_path=None,
        x11_dir_writable=False) == "matplotlib"


# ---- gl-probe wiring ----------------------------------------------------------

def test_gl_probe_subcommand_exists(render):
    # the --gl-probe entrypoint must be callable (crash-safe child mechanism)
    assert hasattr(render, "_gl_probe")
    assert hasattr(render, "_gl_works")


def test_section_axis_parsing_present(render):
    # --section [axis] support exists for Wave 3.2
    assert hasattr(render, "_render_vtk_section")
    assert hasattr(render, "_render_matplotlib_section")
