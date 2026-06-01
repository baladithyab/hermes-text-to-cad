"""Wave 1.2 — ReAct error-feedback loop (unit tests, mocked subprocess).

Three surfaces, all LLM-free so they're deterministic and testable offline:

  core.summarize_error(stderr) -> dict | None
      Parse a Python/OCC traceback into a structured observation
      {error_type, message, hint, raw}. The hint turns OCC's cryptic
      "StdFail_NotDone: BRep_API: command not done" into actionable guidance.

  core.iterate(prompt, history=None, last_error=None) -> dict
      The thin ReAct contract: assemble the structured observation the agent
      feeds into the NEXT cad_generate (attempt #, derived spec, summarized
      last error, an instruction). Pure — no subprocess.

  core.generate_with_retry(code_fn, ...) -> dict
      The executable driver. code_fn(observation) -> CadQuery code plays the
      role of the agent; the loop regenerates with the error in context until
      success or the iteration cap. Here we mock core.generate.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_text_to_cad.core as core  # noqa: E402


# A real CadQuery/OCC traceback for a too-large fillet (captured 2026-05-31).
OCC_FILLET_TB = """Traceback (most recent call last):
  File "/tmp/cad_x/part_gen.py", line 4, in <module>
    part = cq.Workplane("XY").box(10, 10, 10).edges("|Z").fillet(50)
  File ".../cadquery/cq.py", line 1243, in fillet
    s = solid.fillet(radius, edgeList)
  File ".../cadquery/occ_impl/shapes.py", line 3732, in fillet
    return self.__class__(fillet_builder.Shape())
OCP.OCP.StdFail.StdFail_NotDone: BRep_API: command not done
"""

NAME_ERROR_TB = """Traceback (most recent call last):
  File "/tmp/cad_x/part_gen.py", line 2, in <module>
    part = Workplane("XY").box(1, 2, 3)
NameError: name 'Workplane' is not defined
"""


# ---- summarize_error ----------------------------------------------------------

def test_summarize_error_none_on_empty():
    assert core.summarize_error("") is None
    assert core.summarize_error("   \n  ") is None


def test_summarize_error_occ_kernel():
    s = core.summarize_error(OCC_FILLET_TB)
    assert s is not None
    # the dotted OCC leaf is extracted exactly (not the full OCP.OCP.StdFail...)
    assert s["error_type"] == "StdFail_NotDone"
    assert "BRep_API" in s["message"]
    # OCC "command not done" → a hint about fillet/chamfer radius
    assert "fillet" in s["hint"].lower() or "radius" in s["hint"].lower()


def test_summarize_error_python_exception():
    s = core.summarize_error(NAME_ERROR_TB)
    assert s["error_type"] == "NameError"
    assert "Workplane" in s["message"]


def test_summarize_error_keeps_raw_tail():
    s = core.summarize_error(OCC_FILLET_TB)
    assert "raw" in s and s["raw"]
    assert len(s["raw"]) <= 2000


def test_summarize_error_unknown_falls_back_to_last_line():
    s = core.summarize_error("some non-traceback chatter\nmysterious failure here")
    assert s is not None
    assert "mysterious failure here" in s["message"]


# ---- iterate (thin contract) --------------------------------------------------

def test_iterate_first_attempt_has_no_error():
    obs = core.iterate("a 40x30x20 block with a through hole")
    assert obs["attempt"] == 1
    assert obs["last_error"] is None
    assert obs["history"] == []
    assert obs["spec"]["through_holes"] == 1   # derived spec is attached


def test_iterate_second_attempt_summarizes_string_error():
    obs = core.iterate(
        "a 10mm cube with rounded edges",
        history=[{"attempt": 1, "success": False}],
        last_error=OCC_FILLET_TB,
    )
    assert obs["attempt"] == 2
    assert obs["last_error"]["message"]
    assert "BRep_API" in obs["last_error"]["message"]
    assert obs["instruction"]               # tells the model to fix the error
    assert "error" in obs["instruction"].lower() or "fix" in obs["instruction"].lower()


def test_iterate_accepts_already_summarized_error():
    summarized = {"error_type": "NameError", "message": "name 'cq' is not defined", "hint": "import"}
    obs = core.iterate("a cube", last_error=summarized)
    assert obs["last_error"] == summarized   # idempotent passthrough


def test_iterate_attempt_tracks_history_length():
    hist = [{"attempt": 1, "success": False}, {"attempt": 2, "success": False}]
    obs = core.iterate("a cube", history=hist, last_error="boom")
    assert obs["attempt"] == 3


# ---- generate_with_retry (driver, mocked generate) ---------------------------

def test_retry_converges_after_one_failure():
    calls = []

    def fake_generate(code, out_dir=None, stem="part"):
        calls.append(code)
        if len(calls) == 1:
            return {"success": False, "stl": None, "step": None,
                    "stderr": OCC_FILLET_TB, "stdout": "", "out_dir": out_dir or "/tmp/x"}
        return {"success": True, "stl": "/tmp/x/part.stl", "step": "/tmp/x/part.step",
                "stderr": "", "stdout": "", "out_dir": out_dir or "/tmp/x"}

    def code_fn(obs):
        # The "agent": first attempt broken, then fixes using the error.
        return "BROKEN" if obs["last_error"] is None else "FIXED"

    with mock.patch.object(core, "generate", side_effect=fake_generate):
        res = core.generate_with_retry(code_fn, prompt="a 10mm cube with rounded edges", max_iters=3)

    assert res["success"] is True
    assert res["attempts"] == 2
    assert len(res["history"]) == 2
    assert res["history"][0]["success"] is False
    assert res["history"][1]["success"] is True
    # the agent saw the summarized error on the second attempt
    assert calls == ["BROKEN", "FIXED"]


def test_retry_gives_up_at_cap():
    def always_fail(code, out_dir=None, stem="part"):
        return {"success": False, "stl": None, "step": None,
                "stderr": OCC_FILLET_TB, "stdout": "", "out_dir": "/tmp/x"}

    with mock.patch.object(core, "generate", side_effect=always_fail):
        res = core.generate_with_retry(lambda obs: "still broken",
                                       prompt="a cube", max_iters=3)

    assert res["success"] is False
    assert res["attempts"] == 3
    assert len(res["history"]) == 3
    assert all(h["success"] is False for h in res["history"])


def test_retry_passes_summarized_error_to_code_fn():
    seen = []
    gen_calls = {"n": 0}

    def fake_generate(code, out_dir=None, stem="part"):
        gen_calls["n"] += 1
        if gen_calls["n"] == 1:
            return {"success": False, "stl": None, "stderr": NAME_ERROR_TB, "out_dir": "/x"}
        return {"success": True, "stl": "/x/part.stl", "stderr": "", "out_dir": "/x"}

    def code_fn(obs):
        seen.append(obs["last_error"])
        return "code"

    with mock.patch.object(core, "generate", side_effect=fake_generate):
        core.generate_with_retry(code_fn, prompt="a cube", max_iters=3)

    assert seen[0] is None                          # first attempt: clean
    assert seen[1]["error_type"] == "NameError"     # second: structured error


def test_retry_reuses_out_dir_across_attempts():
    # A failed attempt's out_dir must be reused on the next attempt so artifacts
    # land together (guards core.py's `out_dir = out_dir or result.get(...)`).
    seen_out_dirs = []

    def fake_generate(code, out_dir=None, stem="part"):
        seen_out_dirs.append(out_dir)
        if len(seen_out_dirs) == 1:
            return {"success": False, "stl": None, "step": None,
                    "stderr": OCC_FILLET_TB, "stdout": "", "out_dir": "/tmp/cad_attempt1"}
        return {"success": True, "stl": "/tmp/cad_attempt1/part.stl",
                "step": None, "stderr": "", "stdout": "", "out_dir": "/tmp/cad_attempt1"}

    with mock.patch.object(core, "generate", side_effect=fake_generate):
        core.generate_with_retry(lambda obs: "code", prompt="a cube",
                                 out_dir=None, max_iters=3)

    assert seen_out_dirs[0] is None                  # harness picks a dir
    assert seen_out_dirs[1] == "/tmp/cad_attempt1"   # second attempt reuses it


def test_retry_first_attempt_succeeds_no_loop():
    def ok(code, out_dir=None, stem="part"):
        return {"success": True, "stl": "/x/part.stl", "stderr": "", "out_dir": "/x"}

    with mock.patch.object(core, "generate", side_effect=ok):
        res = core.generate_with_retry(lambda obs: "good", prompt="a cube", max_iters=5)

    assert res["success"] is True
    assert res["attempts"] == 1
