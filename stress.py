"""Large-scale randomized stress test (seeded, multiprocess).

    py stress.py --n 2000 tugs/tug_v1.py tugs/tug_v2_ai.py tugs/tug_v3_fixed.py

Random scenarios span all 8 real SFO stands, 4 aircraft types, engine starts, pushbacks,
fog, GPS noise, crew and debris. Some random scenarios are infeasible for ANY controller,
so the gate scores them differentially: a fuzz scenario only counts if the currently
approved controller passes it (verify.py --fuzz).
"""
import argparse, json, math, os, random, time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import sim, checks

SEQS = [[], ["beacon_on"], ["beacon_on", "engines_running"],
        ["beacon_on", "engines_running", "pushback", "gone"], ["beacon_on", "pushback", "gone"]]


def random_scenario(i, seed=0):
    r = random.Random(seed * 1_000_003 + i)
    dest = r.choice(sim.STAND_REFS)
    others = r.sample([s for s in sim.STAND_REFS if s != dest], r.randint(1, 5))
    aircraft = []
    for st in others:
        t, ev = r.uniform(0, 40), []
        for state in r.choice(SEQS):
            ev.append({"t": round(t, 1), "state": state})
            t += r.uniform(5, 40)
        if ev and ev[-1]["state"] != "gone":                   # active aircraft always leave eventually
            ev.append({"t": round(min(t + r.uniform(20, 60), 200), 1), "state": "gone"})
        aircraft.append({"stand": st, "type": r.choice(list(sim.AIRCRAFT)),
                         "push_dist": round(r.uniform(20, 60), 1), "events": ev})
    route = sim.route_to(dest)
    obstacles = []
    for _ in range(r.randint(0, 3)):
        k = r.randrange(1, len(route) - 1)
        (ax, ay), (bx, by) = route[k - 1], route[k]
        f = r.random()
        px, py = ax + f * (bx - ax), ay + f * (by - ay)
        L = math.hypot(bx - ax, by - ay) or 1
        nx, ny = -(by - ay) / L, (bx - ax) / L
        if r.random() < 0.7:
            obstacles.append({"kind": "person", "speed": round(r.uniform(0.8, 1.6), 2),
                              "t0": round(r.uniform(0, 60), 1),
                              "path": [[px - 15 * nx, py - 15 * ny], [px + 15 * nx, py + 15 * ny]]})
        else:
            off = r.choice([-1, 1]) * r.uniform(9, 15)
            obstacles.append({"kind": "debris", "speed": 0, "path": [[px + off * nx, py + off * ny]]})
    return {"name": f"fuzz_{seed}_{i}", "seed": i, "destination": dest, "deadline": 300,
            "visibility": 5000 if r.random() < 0.7 else round(r.uniform(40, 400)),
            "gps_noise": round(r.uniform(0.1, 2.5), 2), "aircraft": aircraft, "obstacles": obstacles}


def _job(args):
    path, scenarios = args
    import verify
    ctrl = verify.load_controller(path)
    out = []
    for sc in scenarios:
        tr = sim.run(ctrl(), sc)
        out.append({"name": sc["name"], "violations": checks.check(tr),
                    "near_misses": checks.near_misses(tr), "steps": len(tr["steps"])})
    return out


def run_many(path, scenarios, workers=None):
    workers = workers or os.cpu_count()
    chunks = [scenarios[i::workers] for i in range(workers)]
    res = []
    with ProcessPoolExecutor(workers) as ex:
        for part in ex.map(_job, [(str(path), c) for c in chunks if c]):
            res += part
    order = {sc["name"]: i for i, sc in enumerate(scenarios)}
    return sorted(res, key=lambda r: order[r["name"]])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", nargs="+")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    scen = [random_scenario(i, a.seed) for i in range(a.n)]
    report = {}
    for c in a.candidates:
        t0 = time.time()
        res = run_many(c, scen)
        steps = sum(r["steps"] for r in res)
        failed = [r["name"] for r in res if r["violations"]]
        rules = Counter(v["rule"] for r in res for v in r["violations"])
        report[c] = {"scenarios": len(res), "failed": len(failed), "rules": dict(rules),
                     "sim_steps": steps, "sim_hours": round(steps * sim.DT / 3600, 2),
                     "wall_s": round(time.time() - t0, 1), "failed_names": failed}
        print(f"{c:24} {len(failed):5}/{len(res)} scenarios with violations  "
              f"{steps:,} steps ({steps * sim.DT / 3600:.1f} sim-h) in {time.time() - t0:.0f}s  {dict(rules)}")
    if len(a.candidates) > 1:   # differential: failures the first (approved) controller does NOT have
        base = set(report[a.candidates[0]]["failed_names"])
        for c in a.candidates[1:]:
            reg = [n for n in report[c]["failed_names"] if n not in base]
            report[c]["regressions_vs_" + a.candidates[0]] = len(reg)
            print(f"  {c}: {len(reg)} regressions vs {a.candidates[0]}")
    os.makedirs("out", exist_ok=True)
    json.dump(report, open("out/stress.json", "w"), indent=1)
