"""SubQ (verifier) and Meta (implementer + vision) clients with record/replay cache.

Keys come from env only and are never printed. Cache: cache/<sha256>.json keyed on
(endpoint, model, messages). OFFLINE=True -> cache only. No key and no cache -> None
(caller falls back to the built-in floor and records spec_source).
"""
import base64, hashlib, json, os
from pathlib import Path
import requests

CACHE = Path(__file__).parent / "cache"
OFFLINE = os.environ.get("AIRGUARD_OFFLINE") == "1"
SUBQ_URL = os.environ.get("SUBQ_URL", "https://api.subq.ai/v1/chat/completions")
SUBQ_MODEL = os.environ.get("SUBQ_MODEL", "subq-preview")
META_URL = os.environ.get("META_URL", "https://api.meta.ai/v1/chat/completions")
META_MODEL = os.environ.get("META_MODEL", "muse-spark-1.2")


def key_of(url, model, messages):
    blob = json.dumps({"url": url, "model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _call(url, model, key_env, messages, timeout=600):
    h = key_of(url, model, messages)
    f = CACHE / f"{h}.json"
    if f.exists():
        r = json.loads(f.read_text())
        return {"text": r["text"], "usage": r.get("usage", {}), "hash": h, "cached": True}
    key = os.environ.get(key_env)
    if OFFLINE or not key:
        return None
    resp = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                         json={"model": model, "messages": messages, "temperature": 0},
                         timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{model} HTTP {resp.status_code}: {resp.text[:300]}")
    j = resp.json()
    out = {"text": j["choices"][0]["message"]["content"], "usage": j.get("usage", {})}
    CACHE.mkdir(exist_ok=True)
    f.write_text(json.dumps({"model": model, **out}, indent=1))
    return {**out, "hash": h, "cached": False}


def subq(messages):
    return _call(SUBQ_URL, SUBQ_MODEL, "SUBQ_API_KEY", messages)


def meta(messages, image=None):
    if image:
        b64 = base64.b64encode(Path(image).read_bytes()).decode()
        last = messages[-1]
        messages = messages[:-1] + [{"role": last["role"], "content": [
            {"type": "text", "text": last["content"]},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]
    return _call(META_URL, META_MODEL, "META_API_KEY", messages)


def extract_json(text):
    """First {...} or [...] block in an LLM reply (tolerates ```json fences)."""
    if not text:
        return None
    for o, c in (("{", "}"), ("[", "]")):
        i, j = text.find(o), text.rfind(c)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def extract_code(text):
    if text and "```" in text:
        parts = text.split("```")
        code = max(parts[1::2], key=len)
        return code.split("\n", 1)[1] if code.startswith(("python", "py\n")) else code
    return text
