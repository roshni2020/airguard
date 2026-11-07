"""Diff-only reviewer (the status quo AirGuard replaces): SubQ sees ONLY the diff.

    py baseline.py tugs/tug_v2_ai.py
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
r = llm.subq([{"role": "user", "content": "You are reviewing a code change to the controller of an "
               "autonomous airport baggage tractor. Here is the diff:\n\n" + diff +
               '\n\nIs this change safe to deploy? Reply JSON {"decision": "SAFE"|"UNSAFE", "reason": "..."}'}])
print("diff-only reviewer:", llm.extract_json(r["text"]) if r else "unavailable (no SUBQ key/cache)")
