"""Agent call trace: py calls.py [--run RUN_ID]  (reads out/calls.jsonl)."""
import argparse, json
from collections import defaultdict
from pathlib import Path

rows = [json.loads(l) for l in (Path(__file__).parent / "out/calls.jsonl").read_text().splitlines() if l.strip()]
ap = argparse.ArgumentParser(); ap.add_argument("--run"); ap.add_argument("--export", action="store_true"); a = ap.parse_args()
runs = defaultdict(list)
for r in rows:
    if not a.run or r["run_id"] == a.run:
        runs[r["run_id"]].append(r)
fmt = lambda n: f"{n:,}" if isinstance(n, int) else "-"
tot = defaultdict(lambda: [0, 0, 0.0, 0])
for rid, rs in runs.items():
    print(f"\nrun {rid}  ({rs[-1].get('verdict_file') or 'no verdict'})")
    print(f"  {'agent':20} {'rnd':>3} {'model':16} {'prompt tok':>11} {'out tok':>8} {'lat s':>6} {'cache':5} outcome")
    for r in rs:
        print(f"  {r['agent'] or '?':20} {r['round'] if r['round'] is not None else '':>3} {r['model']:16} "
              f"{fmt(r['prompt_tokens']):>11} {fmt(r['completion_tokens']):>8} {r['latency_s']:>6} "
              f"{'yes' if r['cached'] else 'no':5} {r['outcome'] or ''}")
        t = tot[r["agent"]]; t[0] += 1; t[1] += r["prompt_tokens"] or 0; t[2] += r["latency_s"]; t[3] += (not r["cached"]) * (r["prompt_tokens"] or 0)
    sq = [r for r in rs if r["agent"] == "subq-redteam"]
    if len(sq) > 1:
        print("  SubQ context growth: " + " -> ".join(f"R{r['round']} {fmt(r['prompt_tokens'])}" for r in sq))
print(f"\n{'agent':20} {'calls':>5} {'prompt tok':>12} {'billed (uncached)':>18} {'latency s':>10}")
for ag, (n, pt, lat, billed) in sorted(tot.items(), key=lambda x: -x[1][1]):
    print(f"{ag or '?':20} {n:>5} {pt:>12,} {billed:>18,} {lat:>10.1f}")

if __name__ == "__main__" and "--export" in __import__("sys").argv:
    import glob
    import agents
    growth = []
    for f in sorted(glob.glob(str(Path(__file__).parent / "verdicts/*.json"))):
        v = json.loads(Path(f).read_text())
        if v.get("spec_source") == "subq" and len(v.get("rounds", [])) > 1:
            growth.append({"verdict": Path(f).name, "candidate": v["candidate"], "result": v["verdict"],
                           "rounds": [[r["round"], r.get("prompt_tokens") or 0] for r in v["rounds"]]})
    out = {"crew": agents.CREW, "calls": rows, "growth": growth,
           "models": {"subq-preview": "SubQ long-context LLM", "subq-snap-1.1": "SubQ Snap decision model", "muse-spark-1.2": "Meta Muse Spark"}}
    (Path(__file__).parent / "out/calls.js").write_text("window.OBS = " + json.dumps(out, separators=(",", ":")) + ";\n")
    print(f"wrote out/calls.js ({len(rows)} calls, {len(growth)} long-horizon runs)")
