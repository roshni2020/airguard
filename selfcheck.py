"""Offline self-check: py selfcheck.py   (no network, cache only, deployed.json untouched)."""
from pathlib import Path
import llm, verify, meta_implement

import tempfile
llm.OFFLINE = True
llm.CACHE = Path(tempfile.mkdtemp())   # hermetic: deterministic floor only, ignores recorded SubQ replies
run = lambda p: verify.verify(p, rounds=1, deploy=False, alert_mode="off", use_suite=False)

v1 = run("tugs/tug_v1.py");          assert v1["verdict"] == "PASS", v1["violations"]
v2 = run("tugs/tug_v2_ai.py");       assert v2["verdict"] == "BLOCK"
assert any(v["rule"] == "jet_blast_zone" for v in v2["violations"])
assert any(v["rule"] == "low_visibility_speed" for v in v2["violations"])
v3 = run("tugs/tug_v3_fixed.py");    assert v3["verdict"] == "PASS", v3["violations"]

still = Path("out/tug_standstill.py"); still.parent.mkdir(exist_ok=True)
still.write_text("class Controller:\n    def step(self, obs):\n        return {'target': obs['pos'], 'speed': 0}\n")
s = run(still);                      assert s["verdict"] == "BLOCK"
assert {v["rule"] for v in s["violations"]} == {"mission_complete"}

assert run("tugs/tug_v2_ai.py")["verdict_hash"] == v2["verdict_hash"], "verdict not reproducible"

import checks   # a bad LLM answer can never weaken the floor
p, rej = checks.tighten({"max_speed": {"limit": 50}, "jet_blast_zone": {"margin": -10}, "fly": {}})
assert p["max_speed"]["limit"] == 6.9 and p["jet_blast_zone"]["margin"] == 0 and rej

for bad in ["import os\nclass Controller:\n def step(s,o): pass",
            "class Controller:\n def step(s,o): return open('x')"]:
    try:
        meta_implement.sanitize(bad); raise SystemExit("sanitizer let bad code through")
    except ValueError:
        pass
print("\nSELFCHECK OK")
