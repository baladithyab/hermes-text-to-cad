"""Wave 0.1 — plugin-contract tests.

Assert the plugin's *contract* with the Hermes loader without booting a gateway:
  - plugin.yaml parses and matches the documented identity
  - register(ctx) wires exactly the 5 documented tools + the `cad` CLI
  - every tool's JSON Schema (schema["parameters"]) is structurally valid
  - schema["name"] mirrors the registered tool name
  - the manifest's provides_tools list matches what register() actually wires
  - tool handlers are callable with the (args: dict, **kw) calling convention
  - the optional cred (OPENROUTER_API_KEY) is surfaced at the tool level, not
    the manifest's requires_env (the hostile-UX trap the hard rules call out)

Nothing here imports the CAD stack; subprocess is never invoked.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {
    "cad_spec_from_prompt", "cad_plan", "cad_generate", "cad_render", "cad_measure", "cad_review",
}


@pytest.fixture
def manifest():
    return yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())


@pytest.fixture
def registered(fake_ctx, plugin):
    plugin.register(fake_ctx)
    return fake_ctx


def _params(tool_spec):
    """The JSON Schema for a tool lives under schema['parameters']."""
    return tool_spec["schema"]["parameters"]


# ---- manifest -----------------------------------------------------------------

def test_manifest_identity(manifest):
    assert manifest["name"] == "hermes-text-to-cad"
    assert manifest["manifest_version"] == 1
    assert "version" in manifest


def test_manifest_requires_env_is_empty(manifest):
    # Hard rule: never prompt for env vars at install time (hostile-UX trap).
    # Optional creds are surfaced at the tool level / doctor time instead.
    assert manifest["requires_env"] == []


def test_manifest_provides_tools_matches_register(manifest, registered):
    assert set(manifest["provides_tools"]) == set(registered.tools.keys()) == EXPECTED_TOOLS


# ---- register() wiring --------------------------------------------------------

def test_register_wires_expected_tools(registered):
    assert set(registered.tools.keys()) == EXPECTED_TOOLS
    assert len(registered.tools) == len(EXPECTED_TOOLS)


def test_register_wires_cad_cli(registered):
    assert "cad" in registered.cli_commands
    cmd = registered.cli_commands["cad"]
    assert callable(cmd["setup_fn"])
    assert callable(cmd["handler_fn"])


def test_register_is_callable(plugin):
    assert callable(plugin.register)


# ---- tool schemas -------------------------------------------------------------

def test_every_tool_param_schema_is_valid_json_schema(registered):
    for name, spec in registered.tools.items():
        params = _params(spec)
        # Raises SchemaError if the schema itself is malformed.
        Draft202012Validator.check_schema(params)
        assert params["type"] == "object", f"{name} param schema must be an object"
        assert "properties" in params, f"{name} param schema missing properties"


def test_schema_name_mirrors_tool_name(registered):
    for name, spec in registered.tools.items():
        assert spec["schema"]["name"] == name, (
            f"{name}: schema['name'] is {spec['schema']['name']!r}, must mirror the tool name"
        )


def test_required_fields_exist_in_properties(registered):
    for name, spec in registered.tools.items():
        params = _params(spec)
        for req in params.get("required", []):
            assert req in params["properties"], (
                f"{name}: required field '{req}' not declared in properties"
            )


def test_tool_descriptions_nonempty(registered):
    for name, spec in registered.tools.items():
        assert spec["description"].strip(), f"{name} has an empty description"


def test_tool_handlers_are_callable(registered):
    for name, spec in registered.tools.items():
        assert callable(spec["handler"]), f"{name} handler is not callable"


# ---- optional-cred surfacing --------------------------------------------------

def test_vision_gate_declares_optional_cred(registered):
    # cad_review needs OPENROUTER_API_KEY; it's declared at the tool level so the
    # gateway can show it without blocking install. The numeric tools must NOT
    # require any env.
    assert registered.tools["cad_review"]["requires_env"] == ["OPENROUTER_API_KEY"]
    for name in ("cad_spec_from_prompt", "cad_generate", "cad_render", "cad_measure"):
        assert not registered.tools[name]["requires_env"], (
            f"{name} must not require any env var (numeric path works offline)"
        )


# ---- required-arg coverage (documented tool table) ---------------------------

@pytest.mark.parametrize(
    "tool,required",
    [
        ("cad_spec_from_prompt", ["prompt"]),
        ("cad_plan", ["prompt"]),
        ("cad_generate", ["code"]),
        ("cad_render", ["stl"]),
        ("cad_measure", ["stl"]),
        ("cad_review", ["montage", "spec_path"]),
    ],
)
def test_tool_required_args(registered, tool, required):
    assert _params(registered.tools[tool]).get("required", []) == required
