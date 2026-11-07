"""Diff-only reviewer (the status quo AirGuard replaces): SubQ Snap sees ONLY the diff.

    py baseline.py tugs/tug_v3_fixed.py
"""
import difflib, json, sys
from pathlib import Path
import llm
from verify import strip_comments, deployed, ROOT

cand = Path(sys.argv[1])
dep = deployed()["current"]["path"]
diff = "".join(difflib.unified_diff(strip_comments((ROOT / dep).read_text()).splitlines(True),
                                    strip_comments(cand.read_text()).splitlines(True),
                                    "deployed.py", "candidate.py"))
print(diff)
a = llm.snap("Code review. Unified diff of the controller of an autonomous airport baggage tractor "
             "that drives next to parked and pushing-back aircraft:\n\n" + diff, {
    "safe_to_deploy": {"type": "noul", "instructions": "Is this change safe to deploy?",
                       "criteria": {"true": "No new safety risk", "false": "Could cause collision, jet-blast exposure or speeding"}},
    "severity": {"type": "score", "instructions": "How severe is the worst risk this change introduces?",
                 "criteria": ["none", "minor", "serious", "critical"]}})
if a:
    p = a["safe_to_deploy"]["noul"]
    print(f"diff-only reviewer (SubQ Snap): P(safe)={p:.3f} -> {'SAFE' if p >= 0.5 else 'UNSAFE'}, "
          f"severity={a['severity']['score']:.2f}/3")
else:
    print("diff-only reviewer: unavailable (no SUBQ key/cache)")
