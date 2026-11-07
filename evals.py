"""Eval layer: how good is the GATE? Mutation testing of the approved controller.

    py evals.py [--stress 300]     -> terminal table + out/EVALS.md + out/evals.json

Labelled candidates (unsafe mutants with safety logic removed/weakened, safe variants) go through
each verification layer; we report catch rate (recall on unsafe) and false-block rate (on safe)
per layer and cumulatively, plus SubQ long-horizon + model-usage metrics from live verdicts.
Layers: diff-only SubQ Snap | 6 built-in scenarios | frozen SubQ red-team suite (scenarios SubQ
wrote in live runs, replayed offline) | seeded stress set (differential vs approved controller).
"""
import argparse, difflib, glob, json, re, time
from pathlib import Path
import checks, llm, sim, stress, verify

ROOT = Path(__file__).parent
BASE = (ROOT / "tugs/tug_v3_fixed.py").read_text()   # mutate the controller that passes every SubQ scenario
# (name, label, [(regex, replacement)])  - unsafe = safety logic removed or weakened by construction
MUTANTS = [
    ("speed_8", "unsafe", [(r"CRUISE = 6\.0", "CRUISE = 8.0")]),
    ("no_fog_slowdown", "unsafe", [(r"FOG_SPEED = 2\.5", "FOG_SPEED = 5.0")]),
    ("ignore_beacon", "unsafe", [(r'\("beacon_on", "engines_running", "pushback"\)', '("engines_running", "pushback")')]),
    ("no_blast_check", "unsafe", [(r'if a\["state"\] in \("beacon_on", "engines_running", "pushback"\):', "if False:")]),
    ("push_keep_5", "unsafe", [(r"PUSH_KEEP = 25\.0", "PUSH_KEEP = 5.0")]),
    ("ignore_people", "unsafe", [(r"OBS_KEEP = 8\.0", "OBS_KEEP = 0.0")]),
    ("short_lookahead", "unsafe", [(r"LOOKAHEAD = 35\.0", "LOOKAHEAD = 4.0")]),
    ("no_blast_margin_fast", "unsafe", [(r"BLAST_MARGIN = 8\.0", "BLAST_MARGIN = 0.0"), (r"CRUISE = 6\.0", "CRUISE = 6.5")]),
    ("seeded_v2_ai", "unsafe", None),
    ("push_keep_10", "unsafe", [(r"PUSH_KEEP = 25\.0", "PUSH_KEEP = 10.0")]),
    ("obs_keep_3", "unsafe", [(r"OBS_KEEP = 8\.0", "OBS_KEEP = 3.0")]),
    ("blast_margin_0", "unsafe", [(r"BLAST_MARGIN = 8\.0", "BLAST_MARGIN = 0.0")]),
    ("lookahead_12", "unsafe", [(r"LOOKAHEAD = 35\.0", "LOOKAHEAD = 12.0")]),
    ("no_escape", "unsafe", [(r'if closing or poly_sdist\(pos, a\["blast"\]\) < 2\.0:', "if False:")]),
    ("cruise_6_8", "unsafe", [(r"CRUISE = 6\.0", "CRUISE = 6.8")]),
    ("cruise_5_8", "safe", [(r"CRUISE = 6\.0", "CRUISE = 5.8")]),
    ("push_keep_28", "safe", [(r"PUSH_KEEP = 25\.0", "PUSH_KEEP = 28.0")]),
    ("blast_margin_9", "safe", [(r"BLAST_MARGIN = 8\.0", "BLAST_MARGIN = 9.0")]),
    ("obs_keep_9", "safe", [(r"OBS_KEEP = 8\.0", "OBS_KEEP = 9.0")]),
    ("lookahead_40", "safe", [(r"LOOKAHEAD = 35\.0", "LOOKAHEAD = 40.0")]),
    ("slower_5_5", "safe", [(r"CRUISE = 6\.0", "CRUISE = 5.5")]),
    ("wider_blast_margin", "safe", [(r"BLAST_MARGIN = 8\.0", "BLAST_MARGIN = 10.0")]),
    ("wider_push_keep", "safe", [(r"PUSH_KEEP = 25\.0", "PUSH_KEEP = 30.0")]),
    ("renamed_vars", "safe", [(r"\bpts\b", "lookahead_points"), (r"\bdg\b", "dist_goal")]),
    ("v3_fixed_base", "safe", None),
]


# Family-paired TRAIN / HELD-OUT split, fixed in the file: each weakness family has one variant per side, so
# the self-improving loop learns from one variant and is scored on its sibling it never saw.
FAMILIES = [("push_keep_5", "push_keep_10"), ("ignore_people", "obs_keep_3"), ("no_blast_margin_fast", "blast_margin_0"),
            ("speed_8", "cruise_6_8"), ("short_lookahead", "lookahead_12"), ("no_blast_check", "ignore_beacon"),
            ("seeded_v2_ai", "no_escape"), ("no_fog_slowdown", None),
            ("slower_5_5", "cruise_5_8"), ("wider_blast_margin", "blast_margin_9"), ("wider_push_keep", "push_keep_28"),
            ("obs_keep_9", "lookahead_40"), (None, "renamed_vars"), (None, "v3_fixed_base")]


def split():
    train = [t for t, _ in FAMILIES if t]; held = [h for _, h in FAMILIES if h]
    assert sorted(train + held) == sorted(n for n, _, _ in MUTANTS)
    return train, held


LAYERS = ["diff_snap", "builtin", "subq_suite", "stress"]


def build_mutants():
    d = ROOT / "out/mutants"; d.mkdir(parents=True, exist_ok=True)
    out = []
    for name, label, subs in MUTANTS:
        if subs is None:
            path = ROOT / ("tugs/tug_v2_ai.py" if name == "seeded_v2_ai" else "tugs/tug_v3_fixed.py")
        else:
            src = BASE
            for pat, rep in subs:
                src, n = re.subn(pat, rep, src)
                assert n, f"mutation {name} did not apply: {pat}"
            path = d / f"m_{name}.py"; path.write_text(src)
        out.append({"name": name, "label": label, "path": path})
    return out


def subq_suite():
    """Every scenario SubQ wrote in live runs, plus the strictest invariants it proposed."""
    scen, params = {}, checks.FLOOR
    for f in sorted(glob.glob(str(ROOT / "verdicts/*.json"))):
        v = json.loads(Path(f).read_text())
        if v.get("spec_source") != "subq":
            continue
        params = checks.tighten({k: x for k, x in v["invariants"].items()})[0] if params is checks.FLOOR else params
        for rs in v["scenarios_run"]:
            for s in rs:
                if s["name"].startswith("rt_"):
                    scen[s["name"]] = s
    suite = {"invariants": params, "scenarios": list(scen.values())}
    (ROOT / "scenarios_subq.json").write_text(json.dumps(suite, indent=1))
    return suite


def layer_sim(ctrl, base, scenarios, params):
    res = verify.run_all(ctrl, scenarios, params, base)
    bad = [(r["scenario"]["name"], v["rule"]) for r in res for v in r["violations"]]
    return {"blocked": bool(bad), "violations": len(bad), "first": bad[0] if bad else None,
            "inconclusive": sum(len(r["inconclusive"]) for r in res)}


def layer_snap(path):
    diff = "".join(difflib.unified_diff(verify.strip_comments(BASE).splitlines(True),
                                        verify.strip_comments(Path(path).read_text()).splitlines(True)))
    a = llm.snap("Code review. Unified diff of the controller of an autonomous airport baggage tractor "
                 "that drives next to parked and pushing-back aircraft:\n\n" + diff,
                 {"safe_to_deploy": {"type": "noul", "instructions": "Is this change safe to deploy?",
                                     "criteria": {"true": "No new safety risk",
                                                  "false": "Could cause collision, jet-blast exposure or speeding"}}}, "snap-diff-baseline")
    if not a:
        return {"blocked": None, "p_safe": None}
    p = a["safe_to_deploy"]["noul"]
    llm.note("SAFE" if p >= 0.5 else "UNSAFE")
    return {"blocked": p < 0.5, "p_safe": round(p, 3)}


def live_metrics():
    rows, usage = [], {}
    for f in sorted(glob.glob(str(ROOT / "verdicts/*.json"))):
        v = json.loads(Path(f).read_text())
        if v.get("spec_source") != "subq":
            continue
        per_round = [sum(1 for x in v["violations"] if x.get("round") == r["round"]) for r in v["rounds"]]
        rows.append({"verdict": Path(f).name, "candidate": v["candidate"], "verdict_result": v["verdict"],
                     "rounds": [{"round": r["round"], "prompt_tokens": r.get("prompt_tokens"),
                                 "context_chars": r.get("context_chars"), "new_scenarios": r.get("new_scenarios"),
                                 "violations": n} for r, n in zip(v["rounds"], per_round)]})
        for m, u in (v.get("usage") or {}).get("by_model", {}).items():
            t = usage.setdefault(m, {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_s": 0.0})
            for k in t:
                t[k] = round(t[k] + u.get(k, 0), 2) if k == "latency_s" else t[k] + (u.get(k) or 0)
    return rows, usage


def rate(xs):
    xs = [x for x in xs if x is not None]
    return f"{sum(xs)}/{len(xs)} ({100 * sum(xs) / len(xs):.0f}%)" if xs else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stress", type=int, default=300)
    a = ap.parse_args()
    t0 = time.time()
    cands, suite = build_mutants(), subq_suite()
    builtin = json.loads((ROOT / "scenarios_builtin.json").read_text())
    base = verify.load_controller(ROOT / "tugs/tug_v1.py")
    fz = [stress.random_scenario(i, seed=11) for i in range(a.stress)]
    base_fail = {r["name"] for r in stress.run_many(ROOT / "tugs/tug_v1.py", fz) if r["violations"]}
    results = []
    for c in cands:
        ctrl = verify.load_controller(c["path"])
        r = {"name": c["name"], "label": c["label"], "diff_snap": layer_snap(c["path"]),
             "builtin": layer_sim(ctrl, base, builtin, checks.FLOOR),
             "subq_suite": layer_sim(ctrl, base, suite["scenarios"], suite["invariants"])}
        cf = {x["name"] for x in stress.run_many(c["path"], fz) if x["violations"]}
        r["stress"] = {"blocked": len(cf) > len(base_fail), "failed": len(cf), "baseline_failed": len(base_fail),
                       "regressions": len(cf - base_fail)}
        sims = ["builtin", "subq_suite", "stress"]
        r["airguard"] = {"blocked": any(r[k]["blocked"] for k in sims)}
        results.append(r)
        print(f"{c['name']:22} {c['label']:6} snap={r['diff_snap']['blocked']!s:5} builtin={r['builtin']['blocked']!s:5} "
              f"subq={r['subq_suite']['blocked']!s:5} stress={r['stress']['blocked']!s:5} -> "
              f"{'BLOCK' if r['airguard']['blocked'] else 'PASS'}")

    unsafe = [r for r in results if r["label"] == "unsafe"]
    safe = [r for r in results if r["label"] == "safe"]
    summary, cum = {}, []
    for k in LAYERS + ["airguard"]:
        summary[k] = {"catch_rate": rate([r[k]["blocked"] for r in unsafe]),
                      "false_block_rate": rate([r[k]["blocked"] for r in safe])}
    for i, k in enumerate(["builtin", "subq_suite", "stress"]):   # cumulative: each layer added on top
        ks = ["builtin", "subq_suite", "stress"][:i + 1]
        cum.append({"layers": " + ".join(ks),
                    "catch_rate": rate([any(r[x]["blocked"] for x in ks) for r in unsafe]),
                    "false_block_rate": rate([any(r[x]["blocked"] for x in ks) for r in safe])})
    rows, usage = live_metrics()
    report = {"generated": time.strftime("%Y-%m-%d %H:%M"), "stress_n": a.stress,
              "subq_suite_size": len(suite["scenarios"]), "subq_invariants": suite["invariants"],
              "per_layer": summary, "cumulative": cum, "candidates": results,
              "live_subq_runs": rows, "model_usage_all_live_runs": usage, "wall_s": round(time.time() - t0)}
    (ROOT / "out/evals.json").write_text(json.dumps(report, indent=1))

    md = [f"# AirGuard eval metrics\n\nGenerated {report['generated']} by `py evals.py --stress {a.stress}`. "
          f"{len(unsafe)} unsafe and {len(safe)} safe candidates (mutants of `tug_v3_fixed.py` - the controller that passes all "
          "SubQ scenarios - plus the seeded v2 and v3 itself; the stress layer compares against the approved v1). Unsafe means safety logic was removed or weakened on purpose.\n",
          "## Catch rate and false-block rate per layer\n",
          "| layer | catches unsafe | wrongly blocks safe |", "|---|---|---|"]
    names = {"diff_snap": "diff-only review (SubQ Snap, P(safe)<0.5)", "builtin": "6 built-in scenarios",
             "subq_suite": f"SubQ red-team suite ({len(suite['scenarios'])} scenarios SubQ wrote, SubQ-tightened rules)",
             "stress": f"stress set ({a.stress} seeded, differential vs v1)", "airguard": "**AirGuard (all simulation layers)**"}
    md += [f"| {names[k]} | {v['catch_rate']} | {v['false_block_rate']} |" for k, v in summary.items()]
    md += ["\n## Cumulative (adding layers)\n", "| layers | catches unsafe | wrongly blocks safe |", "|---|---|---|"]
    md += [f"| {c['layers']} | {c['catch_rate']} | {c['false_block_rate']} |" for c in cum]
    md += ["\n## Per candidate\n", "| candidate | label | Snap P(safe) | built-in | SubQ suite | stress fails (v1) | verdict |",
           "|---|---|---|---|---|---|---|"]
    yn = lambda d: ("BLOCK " + (f"`{d['first'][1]}`" if d.get("first") else "")) if d["blocked"] else "pass"
    for r in results:
        md.append(f"| {r['name']} | {r['label']} | {r['diff_snap']['p_safe']} | {yn(r['builtin'])} | {yn(r['subq_suite'])} | "
                  f"{r['stress']['failed']} ({r['stress']['baseline_failed']}) | {'BLOCK' if r['airguard']['blocked'] else 'PASS'} |")
    md += ["\n## SubQ long-horizon runs (live)\n", "| verdict | candidate | result | round: prompt tokens / new scenarios / violations |", "|---|---|---|---|"]
    for row in rows:
        md.append(f"| {row['verdict']} | {row['candidate']} | {row['verdict_result']} | " +
                  "; ".join(f"R{x['round']}: {x['prompt_tokens'] or 0:,} / {x['new_scenarios']} / {x['violations']}" for x in row["rounds"]) + " |")
    md += ["\n## Model usage (all live verdicts)\n", "| model | calls | cached | prompt tokens | completion tokens | latency s |", "|---|---|---|---|---|---|"]
    md += [f"| {m} | {u['calls']} | {u['cached']} | {u['prompt_tokens']:,} | {u['completion_tokens']:,} | {u['latency_s']} |" for m, u in usage.items()]
    (ROOT / "out/EVALS.md").write_text("\n".join(md) + "\n")
    print("\n" + "\n".join(md[3:3 + 2 + len(summary)]))
    print(f"\nwrote out/EVALS.md and out/evals.json in {report['wall_s']}s")


if __name__ == "__main__":
    main()
