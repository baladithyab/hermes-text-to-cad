#!/usr/bin/env python3
"""scatter_review.py — cross-family vision gate for the CAD loop.

Sends a render montage (base64 image) + spec to 3 different model families in
parallel and collects structured JSON verdicts. Pure urllib => guaranteed route
fidelity (no delegate_task vision-propagation dependency). Reads OPENROUTER_API_KEY
from ~/.hermes/.env.

Usage:
    ~/.venvs/cad/bin/python scatter_review.py MONTAGE.png SPEC.json
    # optional: override models (comma-separated) as 3rd arg

Convergent (>=2/3) findings = must-fix. Solo findings get a sanity check first.
Vision CANNOT verify exact dims / watertightness — that's the numeric gate's job
(measure.py). Use both; they're complementary.
"""
import os, sys, json, base64, urllib.request, urllib.error, concurrent.futures

IMG = sys.argv[1]
SPEC = json.load(open(sys.argv[2]))
MODELS = (sys.argv[3].split(",") if len(sys.argv) > 3 else
          ["google/gemini-3.1-pro-preview", "anthropic/claude-opus-4.8", "openai/gpt-5.5"])

key = None
for line in open(os.path.expanduser("~/.hermes/.env")):
    if line.startswith("OPENROUTER_API_KEY=") and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[1].strip().strip('"').strip("'"); break
if not key:
    sys.exit("OPENROUTER_API_KEY not found in ~/.hermes/.env")

data_url = "data:image/png;base64," + base64.b64encode(open(IMG, "rb").read()).decode()

RUBRIC = (
    "You are a CAD design reviewer. The image is a 3-view render (front/top/iso) of a part. "
    "Grade it against the spec on: identity, feature-presence, feature-placement, proportions, "
    "orientation, defects. Do NOT try to judge exact dimensions or watertightness from pixels "
    "(a separate numeric gate handles that). Answer STRICTLY as JSON: "
    '{"verdict":"matches|needs_fixes|cant_tell","intent_score":1-10,'
    '"findings":["..."],"must_fix":["..."]}'
)
USER = f"SPEC:\n{json.dumps(SPEC, indent=2)}\n\nReview the rendered part against this spec."


def call(model):
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": RUBRIC},
            {"role": "user", "content": [
                {"type": "text", "text": USER},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return model, r.get("model", "?"), r["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return model, "ERR", e.read().decode()[:300]
    except Exception as e:
        return model, "ERR", repr(e)


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(MODELS)) as ex:
        results = list(ex.map(call, MODELS))
    for model, served, content in results:
        print("=" * 70)
        print(f"REQUESTED: {model}\nSERVED:    {served}")
        print("-" * 70)
        print(content)
    print("=" * 70)
    # route-fidelity warning
    for model, served, _ in results:
        fam_req = model.split("/")[0]
        if served != "ERR" and fam_req not in served:
            print(f"[warn] {model} served as {served} — route may have drifted", file=sys.stderr)


if __name__ == "__main__":
    main()