"""SECURITY (e) — the threat model is documented and the README warns (ADR 1-3).

Cheap contract tests so the security docs can't silently disappear or drift away
from the env-var names the code actually uses.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_security_md_exists_and_covers_layers():
    txt = (REPO_ROOT / "SECURITY.md").read_text()
    # the three defense layers + the key env vars must be documented
    for token in ("HERMES_CAD_SANDBOX", "HERMES_CAD_NO_AST_CHECK",
                  "bubblewrap", "OPENROUTER_API_KEY", "threat model"):
        assert token in txt or token.lower() in txt.lower(), f"SECURITY.md missing {token!r}"


def test_readme_states_generate_runs_model_code():
    txt = (REPO_ROOT / "README.md").read_text()
    assert "HERMES_CAD_SANDBOX" in txt
    # the explicit trust statement the security wave requires
    low = txt.lower()
    assert "model-authored python" in low or "arbitrary" in low
    assert "untrusted" in low
