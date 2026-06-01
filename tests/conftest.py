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
    """Minimal stand-in for the Hermes plugin registration context.

    Mirrors the real ctx surface: register_tool(name, handler, description,
    schema, toolset=, emoji=, requires_env=) and register_cli_command(name,
    setup_fn=, handler_fn=, help=, description=). Records every call so contract
    tests can assert exactly what the plugin wires up.
    """

    def __init__(self):
        self.tools: dict[str, dict] = {}
        self.cli_commands: dict[str, dict] = {}

    def register_tool(self, name, handler=None, description=None, schema=None,
                      toolset=None, emoji=None, requires_env=None, **extra):
        if name in self.tools:
            raise AssertionError(f"tool registered twice: {name}")
        self.tools[name] = {
            "handler": handler,
            "description": description,
            "schema": schema,
            "toolset": toolset,
            "emoji": emoji,
            "requires_env": requires_env,
            "extra": extra,
        }

    def register_cli_command(self, name, setup_fn=None, handler_fn=None,
                             help=None, description=None, **extra):
        if name in self.cli_commands:
            raise AssertionError(f"cli command registered twice: {name}")
        self.cli_commands[name] = {
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "help": help,
            "description": description,
            "extra": extra,
        }


@pytest.fixture
def fake_ctx():
    return FakeCtx()


@pytest.fixture
def plugin():
    return load_plugin_module()


@pytest.fixture
def fixtures_dir():
    return FIXTURES
