"""SubQ (verifier) and Meta (implementer + vision) clients with record/replay cache.

Keys come from env only and are never printed. Cache: cache/<sha256>.json keyed on
(endpoint, model, messages). OFFLINE=True -> cache only. No key and no cache -> None
(caller falls back to the built-in floor and records spec_source).
"""
import base64, hashlib, json, os, time
from pathlib import Path
import requests

CACHE = Path(__file__).parent / "cache"
_env = Path(__file__).parent / ".env"          # gitignored; KEY=VALUE lines, never printed
if _env.exists():
    for line in _env.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() and v.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
OFFLINE = os.environ.get("AIRGUARD_OFFLINE") == "1"
_chat = lambda base: base.rstrip("/") + ("" if base.rstrip("/").endswith("/chat/completions") else "/chat/completions")
SUBQ_URL = _chat(os.environ.get("SUBQ_URL") or os.environ.get("SUBQ_BASE_URL") or "https://api.subq.ai/v1")
SUBQ_MODEL = os.environ.get("SUBQ_MODEL", "subq-preview")
META_URL = _chat(os.environ.get("META_URL") or os.environ.get("META_BASE_URL") or "https://api.meta.ai/v1")
META_MODEL = os.environ.get("META_MODEL", "muse-spark-1.2")


def key_of(url, model, messages):
    blob = json.dumps({"url": url, "model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


USAGE = []   # one record per call: model usage metrics (tokens, latency, cache hits)
RUN = {"run_id": time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}", "verdict_file": None}
CALLS_LOG = Path(__file__).parent / "out" / "calls.jsonl"
_flushed = [0]


def _log(model, usage, latency, cached, chars, agent=None, rnd=None, h=None):
    USAGE.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_id": RUN["run_id"], "agent": agent, "round": rnd,
                  "model": model, "prompt_sha256": h, "prompt_tokens": usage.get("prompt_tokens"),
                  "completion_tokens": usage.get("completion_tokens"),
                  "total_tokens": usage.get("total_tokens"), "prompt_chars": chars,
                  "latency_s": round(latency, 2), "cached": cached, "outcome": None})


def note(outcome):
    """Callers record a short outcome for the last call ("6 scenarios proposed", "SAFE", ...)."""
    if USAGE:
        USAGE[-1]["outcome"] = str(outcome)[:120]


def flush_calls():
    """Append unflushed calls to out/calls.jsonl (hashes + metrics only - never keys or prompts)."""
    new = USAGE[_flushed[0]:]
    if new:
        CALLS_LOG.parent.mkdir(exist_ok=True)
        with CALLS_LOG.open("a") as f:
            for u in new:
                f.write(json.dumps({**u, "verdict_file": RUN["verdict_file"]}) + "\n")
    _flushed[0] = len(USAGE)


import atexit
atexit.register(flush_calls)


def _call(url, model, key_env, messages, timeout=600, agent=None, rnd=None):
    h = key_of(url, model, messages)
    f = CACHE / f"{h}.json"
    chars = len(json.dumps(messages))
    if f.exists():
        r = json.loads(f.read_text())
        _log(model, r.get("usage", {}), r.get("latency_s", 0), True, chars, agent, rnd, h)
        return {"text": r["text"], "usage": r.get("usage", {}), "hash": h, "cached": True}
    key = os.environ.get(key_env)
    if OFFLINE or not key:
        return None
    t0 = time.time()
    resp = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                         json={"model": model, "messages": messages, "temperature": 0},
                         timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{model} HTTP {resp.status_code}: {resp.text[:300]}")
    j = resp.json()
    out = {"text": j["choices"][0]["message"]["content"], "usage": j.get("usage", {}),
           "latency_s": round(time.time() - t0, 2)}
    _log(model, out["usage"], out["latency_s"], False, chars, agent, rnd, h)
    CACHE.mkdir(exist_ok=True)
    f.write_text(json.dumps({"model": model, **out}, indent=1))
    return {**out, "hash": h, "cached": False}


def subq(messages, agent="subq-redteam", rnd=None):
    return _call(SUBQ_URL, SUBQ_MODEL, "SUBQ_API_KEY", messages, agent=agent, rnd=rnd)


def meta(messages, image=None, agent=None, rnd=None):
    if image:
        b64 = base64.b64encode(Path(image).read_bytes()).decode()
        last = messages[-1]
        messages = messages[:-1] + [{"role": last["role"], "content": [
            {"type": "text", "text": last["content"]},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]
    return _call(META_URL, META_MODEL, "META_API_KEY", messages,
                 agent=agent or ("meta-vision" if image else "meta-implement"), rnd=rnd)


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


SNAP_MODEL = os.environ.get("SUBQ_SNAP_MODEL", "subq-snap-1.1")
SNAP_URL = SUBQ_URL.rsplit("/chat/completions", 1)[0] + "/systemone"


def snap(state, questions, agent="snap-triage", rnd=None):
    """SubQ Snap decision model (POST /v1/systemone): named noul/choice/score questions over a
    long `state` -> probabilities. Advisory only - AirGuard never lets it decide pass/fail."""
    body = {"model": SNAP_MODEL, "state": state, "questions": questions}
    h = key_of(SNAP_URL, SNAP_MODEL, [body])
    f = CACHE / f"{h}.json"
    if f.exists():
        r = json.loads(f.read_text())
        _log(SNAP_MODEL, r.get("usage", {}), r.get("latency_s", 0), True, len(json.dumps(body)), agent, rnd, h)
        return r["answers"]
    key = os.environ.get("SUBQ_API_KEY")
    if OFFLINE or not key:
        return None
    t0 = time.time()
    resp = requests.post(SNAP_URL, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"{SNAP_MODEL} HTTP {resp.status_code}: {resp.text[:300]}")
    j = resp.json()
    u = j.get("usage", {})
    usage = {"prompt_tokens": u.get("input_tokens", u.get("prompt_tokens")), "completion_tokens": 0}
    lat = round(time.time() - t0, 2)
    _log(SNAP_MODEL, usage, lat, False, len(json.dumps(body)), agent, rnd, h)
    CACHE.mkdir(exist_ok=True)
    f.write_text(json.dumps({"model": SNAP_MODEL, "answers": j["answers"], "usage": usage, "latency_s": lat}, indent=1))
    return j["answers"]
