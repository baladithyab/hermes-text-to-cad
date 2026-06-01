"""Shared pytest fixtures + helpers for hermes-text-to-cad.

These tests run in the *Hermes* venv (or any plain python with pytest) — they must
NOT import cadquery/vtk/trimesh. The CAD stack lives only in ~/.venvs/cad and is
exercised via subprocess; unit tests mock subprocess.run, integration tests skip
unless that venv is present.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Ensure the repo root is importable so `import hermes_text_to_cad.core` works
# when pytest is invoked from anywhere.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


_PLUGIN_PKG = "hermes_plugins_h2c"


def load_plugin_module():
    """Load the plugin's root __init__.py the way the Hermes directory loader does.

    Hermes directory plugins load as ``hermes_plugins.<slug>`` with
    ``submodule_search_locations`` set to the plugin dir, so the root
    __init__.py's ``from .hermes_text_to_cad import core`` resolves. We replicate
    that here: a named package whose search path is the repo root, so the
    relative import works exactly as in production.
    """
    if _PLUGIN_PKG in sys.modules:
        return sys.modules[_PLUGIN_PKG]
    init_path = REPO_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _PLUGIN_PKG, init_path, submodule_search_locations=[str(REPO_ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PLUGIN_PKG] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """Stand-in for the Hermes plugin registration context.

    Replicates the register_tool / register_cli_command signatures verified
    against Hermes at authoring time; it is NOT enforced against the live Hermes
    package (which isn't importable in the test env). To catch signature drift
    early, it REJECTS unknown kwargs — if the plugin starts passing a kwarg this
    stub doesn't model, the contract test fails loudly rather than silently
    passing against a stale stub. Records every call so contract tests can
    assert exactly what the plugin wires up.
    """

    def __init__(self):
        self.tools: dict[str, dict] = {}
        self.cli_commands: dict[str, dict] = {}

    def register_tool(self, name, handler=None, description=None, schema=None,
                      toolset=None, emoji=None, requires_env=None, **extra):
        if extra:
            raise AssertionError(
                f"register_tool({name!r}) got unmodeled kwargs {sorted(extra)} — "
                "FakeCtx may be out of sync with the real Hermes signature"
            )
        if name in self.tools:
            raise AssertionError(f"tool registered twice: {name}")
        self.tools[name] = {
            "handler": handler,
            "description": description,
            "schema": schema,
            "toolset": toolset,
            "emoji": emoji,
            "requires_env": requires_env,
        }

    def register_cli_command(self, name, setup_fn=None, handler_fn=None,
                             help=None, description=None, **extra):
        if extra:
            raise AssertionError(
                f"register_cli_command({name!r}) got unmodeled kwargs {sorted(extra)} — "
                "FakeCtx may be out of sync with the real Hermes signature"
            )
        if name in self.cli_commands:
            raise AssertionError(f"cli command registered twice: {name}")
        self.cli_commands[name] = {
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "help": help,
            "description": description,
        }


@pytest.fixture
def fake_ctx():
    return FakeCtx()


@pytest.fixture
def plugin():
    return load_plugin_module()


def load_measure_module():
    """Load scripts/measure.py as a module.

    measure.py imports trimesh *inside* measure() (not at module top), so the
    module loads — and its pure gate() function is testable — without the CAD
    stack present. measure() itself only runs under the CAD venv.
    """
    measure_path = REPO_ROOT / "scripts" / "measure.py"
    spec = importlib.util.spec_from_file_location("cad_measure_script", measure_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def measure_mod():
    return load_measure_module()


@pytest.fixture
def fixtures_dir():
    return FIXTURES
