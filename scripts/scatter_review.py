#!/usr/bin/env python3
"""scatter_review.py — cross-family vision gate for the CAD loop.

Sends a render montage (base64 image) + spec to 3 different model families in
parallel and collects STRUCTURED verdicts. Pure urllib => guaranteed route
fidelity (no delegate_task vision-propagation dependency). Reads OPENROUTER_API_KEY
from ~/.hermes/.env.

Two modes (ADR-0005):
  --mode qa   (default) — CADCodeVerify pattern: each model GENERATES 2-5 Yes/No
               questions derived from the spec, ANSWERS each against the render
               with chain-of-thought, and 'no' answers become must_fix.
  --mode free — legacy free-form critique (preserved for cheap/simple parts).

Output: one JSON object per reviewer on stdout under a machine-readable
REVIEWS_JSON marker (core.review parses + aggregates cross-family convergence).
A human-readable summary is also printed.

Usage:
    ~/.venvs/cad/bin/python scatter_review.py MONTAGE.png SPEC.json
    ~/.venvs/cad/bin/python scatter_review.py MONTAGE.png SPEC.json --mode free
    # optional: --models a/b,c/d  (comma-separated, cross-family)

Convergent (>=2/3) must_fix findings = hard must-fix. Vision CANNOT verify exact
dims / watertightness — that's the numeric gate's job (measure.py). Use both.
"""
import os
import sys
import json
import base64
import argparse
import urllib.request
import urllib.error
import concurrent.futures

DEFAULT_MODELS = [
    "google/gemini-3.1-pro-preview",
    "anthropic/claude-opus-4.8",
    "openai/gpt-5.5",
]

# ---- prompts -----------------------------------------------------------------

QA_RUBRIC = (
    "You are a CAD design reviewer. The image is a 3-view render (front/top/iso) "
    "of a part. Work in TWO steps:\n"
    "1. GENERATE 2-5 specific Yes/No questions that check whether the render "
    "satisfies the spec — identity, every named feature's presence and placement, "
    "orientation, proportions, obvious defects. Each question must be answerable "
    "as exactly 'yes' or 'no' from the render.\n"
    "2. ANSWER each question against the render with a one-sentence chain-of-"
    "thought reason, then 'yes' or 'no'. 'yes' = the spec is satisfied for that "
    "question; 'no' = it is violated.\n"
    "Do NOT judge exact dimensions or watertightness from pixels (a separate "
    "numeric gate handles that). Answer STRICTLY as JSON, no prose outside it:\n"
    '{"questions":["...","..."],'
    '"answers":[{"q_index":0,"reasoning":"...","answer":"yes|no"}],'
    '"verdict":"matches|needs_fixes|cant_tell",'
    '"must_fix":["concrete fix for each NO answer"]}'
)

FREE_RUBRIC = (
    "You are a CAD design reviewer. The image is a 3-view render (front/top/iso) of a part. "
    "Grade it against the spec on: identity, feature-presence, feature-placement, proportions, "
    "orientation, defects. Do NOT try to judge exact dimensions or watertightness from pixels "
    "(a separate numeric gate handles that). Answer STRICTLY as JSON: "
    '{"verdict":"matches|needs_fixes|cant_tell","intent_score":1-10,'
    '"findings":["..."],"must_fix":["..."]}'
)


def _strip_fences(text):
    """Pull a JSON object out of text that may be wrapped in ```json fences or
    surrounded by prose. Returns the substring most likely to be the JSON object,
    or the original text."""
    t = text.strip()
    if "```" in t:
        # take the content of the first fenced block
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    # fall back to the outermost {...}
    if not t.startswith("{") and "{" in t and "}" in t:
        t = t[t.index("{"): t.rindex("}") + 1]
    return t


def parse_reviewer(content):
    """Parse ONE model's reply into {questions, answers, must_fix, verdict}.

    Tolerant of ```json fences and surrounding prose. If must_fix is absent it is
    derived from the 'no' answers (the CADCodeVerify rule). Unparseable output
    yields verdict 'cant_tell' with empty lists — never raises, never silently
    drops a reviewer.
    """
    base = {"questions": [], "answers": [], "must_fix": [], "verdict": "cant_tell"}
    try:
        obj = json.loads(_strip_fences(content))
    except (json.JSONDecodeError, ValueError, TypeError):
        return base
    if not isinstance(obj, dict):
        return base

    questions = obj.get("questions") or []
    answers = obj.get("answers") or []
    verdict = obj.get("verdict") or "cant_tell"
    must_fix = obj.get("must_fix")

    # derive must_fix from 'no' answers when the model omitted it
    if not must_fix:
        derived = []
        for a in answers:
            if isinstance(a, dict) and str(a.get("answer", "")).strip().lower() == "no":
                qi = a.get("q_index")
                q = questions[qi] if isinstance(qi, int) and 0 <= qi < len(questions) else None
                reason = a.get("reasoning", "")
                derived.append(f"{q or 'fix'}: {reason}".strip())
        must_fix = derived

    # normalize verdict against the answers when possible
    if verdict not in ("matches", "needs_fixes", "cant_tell"):
        verdict = "needs_fixes" if must_fix else "matches"

    return {
        "questions": list(questions),
        "answers": list(answers),
        "must_fix": list(must_fix),
        "verdict": verdict,
    }


def _load_key():
    path = os.path.expanduser("~/.hermes/.env")
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    if os.path.exists(path):
        for line in open(path):
            if line.startswith("OPENROUTER_API_KEY=") and not line.lstrip().startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def call(model, rubric, user, data_url, key):
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": rubric},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return model, r.get("model", "?"), r["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return model, "ERR", e.read().decode()[:300]
    except Exception as e:
        return model, "ERR", repr(e)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("montage")
    ap.add_argument("spec")
    ap.add_argument("--mode", choices=["qa", "free"], default="qa")
    ap.add_argument("--models", help="comma-separated model slugs (cross-family)")
    a = ap.parse_args(argv)

    spec = json.load(open(a.spec))
    models = a.models.split(",") if a.models else DEFAULT_MODELS
    rubric = QA_RUBRIC if a.mode == "qa" else FREE_RUBRIC
    user = (f"SPEC:\n{json.dumps(spec, indent=2)}\n\n"
            "Review the rendered part against this spec.")

    key = _load_key()
    if not key:
        sys.exit("OPENROUTER_API_KEY not found in env or ~/.hermes/.env")

    data_url = ("data:image/png;base64,"
                + base64.b64encode(open(a.montage, "rb").read()).decode())

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as ex:
        results = list(ex.map(
            lambda m: call(m, rubric, user, data_url, key), models))

    reviewers = []
    for model, served, content in results:
        if a.mode == "qa":
            parsed = parse_reviewer(content)
        else:
            # free mode: still try to lift a verdict/must_fix for aggregation
            parsed = parse_reviewer(content)
        reviewers.append({
            "model": model, "served": served,
            "raw": content,
            **parsed,
        })

    # human-readable
    for rv in reviewers:
        print("=" * 70)
        print(f"REQUESTED: {rv['model']}\nSERVED:    {rv['served']}\nVERDICT:   {rv['verdict']}")
        if rv["must_fix"]:
            print("MUST_FIX:")
            for m in rv["must_fix"]:
                print(f"  - {m}")
        print("-" * 70)
        print(rv["raw"][:1500])
    print("=" * 70)

    # machine-readable block for core.review to parse
    print("REVIEWS_JSON")
    print(json.dumps({"mode": a.mode, "reviewers": reviewers}))

    # route-fidelity warning
    for rv in reviewers:
        fam_req = rv["model"].split("/")[0]
        if rv["served"] != "ERR" and fam_req not in rv["served"]:
            print(f"[warn] {rv['model']} served as {rv['served']} — route may have drifted",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
