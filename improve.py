"""Self-improving gate: SubQ grows the scenario library; Python adopts a new version only if it
catches more HELD-OUT unsafe mutants without more false blocks.

    py improve.py [--iterations 3]

Loop: run current suite on TRAIN -> escaped unsafe mutants -> SubQ (subq-improve) proposes new
scenarios from their diffs + full traces (never sees HELD-OUT) -> SubQ Snap drops unrealistic
ones -> candidate suite = current + new -> evaluate both on HELD-OUT -> adopt/reject -> history.
"""
import argparse, difflib, hashlib, json, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import checks, evals, llm, sim, verify

ROOT = Path(__file__).parent
SUITE = ROOT / "suite"
BASE = ROOT / "tugs/tug_v3_fixed.py"          # approved controller the mutants are derived from


def _judge(args):
    path, sc, params = args
    r = verify.run_all(verify.load_controller(path), [sc], params, verify.load_controller(BASE))[0]
    return bool(r["violations"]), r["near_misses"]


class Memo:
    """(candidate, scenario) -> (blocked, near_misses). Invariants are fixed, so results carry over."""
    def __init__(self, params):
        self.params, self.d = params, {}

    def fill(self, cands, scenarios):
        todo = [(c, s) for c in cands for s in scenarios if (c["name"], s["name"]) not in self.d]
        if todo:
            with ProcessPoolExecutor() as ex:
                for (c, s), res in zip(todo, ex.map(_judge, [(str(c["path"]), s, self.params) for c, s in todo], chunksize=4)):
                    self.d[(c["name"], s["name"])] = res

    def blocked(self, c, scenarios):
        return any(self.d[(c["name"], s["name"])][0] for s in scenarios)


def metrics(memo, cands, scenarios):
    memo.fill(cands, scenarios)
    un = [c for c in cands if c["label"] == "unsafe"]; sa = [c for c in cands if c["label"] == "safe"]
    cu = sum(memo.blocked(c, scenarios) for c in un); fs = sum(memo.blocked(c, scenarios) for c in sa)
    return {"catch": cu / len(un), "catch_n": f"{cu}/{len(un)}", "false_block": fs / len(sa), "fb_n": f"{fs}/{len(sa)}",
            "escaped": [c["name"] for c in un if not memo.blocked(c, scenarios)],
            "false_blocked": [c["name"] for c in sa if memo.blocked(c, scenarios)]}


def load(ver):
    return json.loads((SUITE / f"{ver}.json").read_text())


def realistic(scenarios):
    """SubQ Snap: one call, one noul per scenario. Advisory filter - drops implausible proposals."""
    if not scenarios:
        return scenarios, {}
    qs = {f"s{i}": {"type": "noul", "instructions": f"Is scenario s{i} ({s['name']}) a realistic apron situation that a careful, "
                    "rule-following tractor could get through safely?",
                    "criteria": {"true": "Plausible ramp operations; a safe controller can comply",
                                 "false": "Physically impossible, contradictory, or unavoidable for any controller"}}
          for i, s in enumerate(scenarios)}
    state = (ROOT / "rules/ground_ops.md").read_text() + "\n\nSCENARIOS:\n" + json.dumps(
        {f"s{i}": s for i, s in enumerate(scenarios)}, indent=1)
    a = llm.snap(state, qs, "snap-realism")
    if not a:
        return scenarios, {}
    p = {s["name"]: round(a[f"s{i}"]["noul"], 3) for i, s in enumerate(scenarios)}
    keep = [s for s in scenarios if p[s["name"]] >= 0.3]
    llm.note(f"{len(keep)}/{len(scenarios)} realistic")
    return keep, p


def propose(cur, escaped, cands, it, feedback=""):
    base_src = verify.strip_comments(BASE.read_text())
    parts = []
    for name in escaped:
        c = next(x for x in cands if x["name"] == name)
        diff = "".join(difflib.unified_diff(base_src.splitlines(True), verify.strip_comments(Path(c["path"]).read_text()).splitlines(True),
                                            "approved.py", f"{name}.py"))
        # full traces of the 3 scenarios where this mutant came closest to a violation
        ctrl, scored = verify.load_controller(c["path"]), []
        for s in cur["scenarios"]:
            tr = sim.run(ctrl(), s); nm = checks.near_misses(tr)
            scored.append((min(nm["min_blast"]["value"], nm["min_push"]["value"] - 15, nm["min_obs"]["value"] - 3), s, tr))
        scored.sort(key=lambda x: x[0])
        parts.append(f"### ESCAPED MUTANT {name}\ncomment-stripped diff vs approved controller:\n{diff}\n" +
                     "\n".join(f"closest scenario {s['name']} (margin {m:.2f} m):\n{verify.trace_csv(tr)}" for m, s, tr in scored[:3]))
    faa = (ROOT / "data/faa_ac_150_5210_20A.txt").read_text(encoding="utf-8", errors="replace")
    msgs = [{"role": "system", "content": "You improve AirGuard's scenario library for autonomous airport baggage tractors. "
             "Deterministic Python decides pass/fail; you only design scenarios. Reply with JSON only."},
            {"role": "user", "content": f"FAA AC 150/5210-20A:\n{faa}\n\nRULES:\n{(ROOT / 'rules/ground_ops.md').read_text()}\n\n"
             f"MAP:\n{verify.map_summary()}\n\n{verify.SCHEMA}\nSTANDS={sim.STAND_REFS} TYPES={list(sim.AIRCRAFT)}\n\n"
             f"CURRENT SUITE {cur['version']} ({len(cur['scenarios'])} scenarios):\n{json.dumps(cur['scenarios'])}\n\n"
             f"These unsafe controller variants PASSED every scenario in the suite - the suite has a blind spot:\n\n" +
             "\n\n".join(parts) + feedback +
             '\n\nPropose up to 6 NEW scenarios that a safe controller passes but these variants fail. Target the specific '
             'weakened behaviour in each diff, using the near-miss traces. Return JSON {"scenarios": [...]}.'}]
    r = llm.subq(msgs, "subq-improve", it)
    if not r:
        return [], None
    j = llm.extract_json(r["text"])
    got = j.get("scenarios", []) if isinstance(j, dict) else []
    new = [v for i, s in enumerate(got[:6]) if (v := verify.validate_scenario(s, 5000 + 100 * it + i))]
    for k, s in enumerate(new):
        s["name"] = f"imp{it}_{k}_{s['name']}"[:60]
    llm.note(f"{len(new)} scenarios proposed ({len(got) - len(new)} invalid)")
    return new, r["hash"]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--iterations", type=int, default=3); a = ap.parse_args()
    cands = evals.build_mutants()
    train_names, held_names = evals.split()
    TRAIN = [c for c in cands if c["name"] in train_names]; HELD = [c for c in cands if c["name"] in held_names]
    cur = load(json.loads((SUITE / "current.json").read_text())["version"])
    memo = Memo(checks.tighten(cur["invariants"])[0])
    hist = SUITE / "history.jsonl"
    rows, feedback = [], ""
    m_tr, m_ho = metrics(memo, TRAIN, cur["scenarios"]), metrics(memo, HELD, cur["scenarios"])
    rows.append({"version": cur["version"], "train": m_tr["catch_n"], "held": m_ho["catch"], "held_n": m_ho["catch_n"],
                 "fb_n": m_ho["fb_n"], "status": "current"})
    print(f"start {cur['version']}: train {m_tr['catch_n']} escaped {m_tr['escaped']} | held-out catch {m_ho['catch_n']} (escaped {m_ho['escaped']}), false-block {m_ho['fb_n']} {m_ho['false_blocked']}")
    for it in range(1, a.iterations + 1):
        if not m_tr["escaped"]:
            print("no escaped TRAIN mutants - nothing to learn"); break
        new, ph = propose(cur, m_tr["escaped"], cands, it, feedback)
        new, real = realistic(new)
        names = {s["name"] for s in cur["scenarios"]}
        new = [s for s in new if s["name"] not in names]
        ver = f"v{int(cur['version'][1:]) + 1:03d}"
        while (SUITE / f"{ver}.json").exists():
            ver = f"v{int(ver[1:]) + 1:03d}"
        cand = {**cur, "version": ver, "parent": cur["version"], "scenarios": cur["scenarios"] + new,
                "added": [s["name"] for s in new], "prompt_sha256": ph, "snap_realism": real}
        assert all(b in {s["name"] for s in cand["scenarios"]} for b in cur["builtin"])   # floor never removed
        (SUITE / f"{ver}.json").write_text(json.dumps(cand, indent=1))
        c_tr, c_ho = metrics(memo, TRAIN, cand["scenarios"]), metrics(memo, HELD, cand["scenarios"])
        adopt = bool(new) and c_ho["catch"] > m_ho["catch"] and c_ho["false_block"] <= m_ho["false_block"]
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": ver, "parent": cur["version"], "adopted": adopt,
               "added": cand["added"], "train_catch": c_tr["catch_n"], "train_escaped": c_tr["escaped"],
               "heldout_catch": [m_ho["catch_n"], c_ho["catch_n"]], "heldout_false_block": [m_ho["fb_n"], c_ho["fb_n"]],
               "prompt_sha256": ph}
        with hist.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        rows.append({"version": ver, "train": c_tr["catch_n"], "held": c_ho["catch"], "held_n": c_ho["catch_n"],
                     "fb_n": c_ho["fb_n"], "status": "ADOPTED" if adopt else "rejected"})
        print(f"iter {it}: {ver} +{len(new)} scenarios | train {c_tr['catch_n']} | held-out catch {m_ho['catch_n']} -> {c_ho['catch_n']}, "
              f"false-block {m_ho['fb_n']} -> {c_ho['fb_n']} => {'ADOPTED' if adopt else 'REJECTED (current stays)'}")
        if not adopt:   # tell SubQ why: its rejected scenarios + how close each escaped mutant came
            lines = []
            for sc in new:
                nm = {e: memo.d[(e, sc["name"])][1] for e in c_tr["escaped"]}
                lines.append(json.dumps(sc) + "\n  escaped-mutant closest approach: " + json.dumps(
                    {e: {k: n[k]["value"] for k in ("min_blast", "min_push", "min_obs", "min_ac")} for e, n in nm.items()}))
            feedback += (f"\n\nITERATION {it} FEEDBACK: your {len(new)} proposals below were simulated and REJECTED - "
                         f"they did not catch {c_tr['escaped']} (limits: pushback 18 m, personnel 3.5 m, aircraft 6 m, "
                         "blast margin 1 m). Design scenarios that push these variants past the limits:\n" + "\n".join(lines))
        if adopt:
            cur, m_tr, m_ho = cand, c_tr, c_ho
            (SUITE / "current.json").write_text(json.dumps({"version": ver}))
    print(f"\n{'version':8} {'train catch':>12} {'held-out catch':>15} {'held-out false-block':>21}  status")
    for r in rows:
        print(f"{r['version']:8} {r['train']:>12} {r['held_n']:>15} {r['fb_n']:>21}  {r['status']}")
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 3.2))
    xs = range(len(rows))
    ax.plot(xs, [r["held"] * 100 for r in rows], color="#58a6ff", marker="o")
    for i, r in enumerate(rows):
        if r["status"] == "rejected":
            ax.plot(i, r["held"] * 100, "x", color="red", ms=12, mew=3)
    ax.set_xticks(list(xs), [r["version"] for r in rows]); ax.set_ylabel("held-out catch %"); ax.set_ylim(0, 105)
    ax.set_title("Self-improving gate (red x = rejected version)", fontsize=10)
    fig.tight_layout(); fig.savefig(ROOT / "out/improve.png", dpi=110)
    print("saved out/improve.png; active suite:", json.loads((SUITE / "current.json").read_text())["version"])


if __name__ == "__main__":
    main()
