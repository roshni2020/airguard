"""AirGuard gate: long-horizon SubQ red-team + deterministic checks -> PASS/BLOCK + rollback.

    py verify.py tugs/tug_v2_ai.py [--rounds 4] [--offline] [--gif] [--no-deploy]
"""
import argparse, copy, difflib, hashlib, importlib.util, io, json, sys, time, tokenize
from pathlib import Path
import sim, checks, llm

ROOT = Path(__file__).parent
DEPLOYED = ROOT / "deployed.json"
STATES = {"parked", "beacon_on", "engines_running", "pushback", "gone"}
MAX_SCEN = 8

SCHEMA = """Scenario JSON (all fields optional except name):
{"name": str, "seed": int, "destination": one of STANDS, "deadline": seconds<=600,
 "visibility": metres 20..10000, "gps_noise": metres 0..5,
 "aircraft": [{"stand": one of STANDS, "type": one of TYPES, "push_dist": 0..80,
               "events": [{"t": seconds, "state": "beacon_on"|"engines_running"|"pushback"|"gone"|"parked"}]}],
 "obstacles": [{"kind": "person"|"debris", "path": [[x,y], ...], "speed": 0..3, "t0": seconds}]}
Coordinates are the local metric frame (x east, y north, metres). Only one aircraft per stand."""


def sha(b):
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode()).hexdigest()


def strip_comments(src):
    """Drop comments and docstrings so the verifier never sees implementer reasoning."""
    out, prev = [], tokenize.INDENT
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (tokenize.INDENT, tokenize.NEWLINE, tokenize.DEDENT):
            prev = tok.type
            continue
        out.append(tok); prev = tok.type
    code = tokenize.untokenize(out)
    return "\n".join(l.rstrip() for l in code.splitlines() if l.strip()) + "\n"


def load_controller(path):
    spec = importlib.util.spec_from_file_location(f"cand_{sha(str(path))[:8]}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Controller


def validate_scenario(sc, i):
    """Reject anything outside the catalogue schema. Returns clean scenario or None."""
    try:
        out = {"name": str(sc.get("name", f"subq_{i}"))[:60], "seed": int(sc.get("seed", i)),
               "destination": sc.get("destination", "A8"), "deadline": float(sc.get("deadline", 240)),
               "visibility": float(sc.get("visibility", 5000)), "gps_noise": float(sc.get("gps_noise", 0.3)),
               "aircraft": [], "obstacles": []}
        assert out["destination"] in sim.STAND_REFS and 30 <= out["deadline"] <= 600
        assert 20 <= out["visibility"] <= 10000 and 0 <= out["gps_noise"] <= 5
        seen = {out["destination"]}
        for a in sc.get("aircraft", []):
            assert a["stand"] in sim.STAND_REFS and a.get("type", "A320") in sim.AIRCRAFT
            if a["stand"] in seen and a["stand"] != out["destination"]:
                continue
            seen.add(a["stand"])
            ev = [{"t": float(e["t"]), "state": e["state"]} for e in a.get("events", [])]
            assert all(e["state"] in STATES and 0 <= e["t"] <= 600 for e in ev)
            out["aircraft"].append({"stand": a["stand"], "type": a.get("type", "A320"),
                                    "push_dist": float(min(80, max(0, a.get("push_dist", 40)))),
                                    "events": ev})
        for o in sc.get("obstacles", []):
            path = [[float(p[0]), float(p[1])] for p in o["path"]][:10]
            assert path and o.get("kind", "person") in ("person", "debris")
            assert all(-500 <= c <= 500 for p in path for c in p)
            out["obstacles"].append({"kind": o.get("kind", "person"), "path": path,
                                     "speed": float(min(3, max(0, o.get("speed", 1.2)))),
                                     "t0": float(o.get("t0", 0))})
        return out
    except (AssertionError, KeyError, TypeError, ValueError, IndexError):
        return None


def map_summary():
    lines = ["Stands (nose xy, unit vector nose->tail, OSM lead-in line length):"]
    for r in sim.STAND_REFS:
        n, u = sim.stand_frame(r)
        lines.append(f"  {r}: nose=({n[0]:.1f},{n[1]:.1f}) dir=({u[0]:.3f},{u[1]:.3f})")
    lines.append("Tractor route to A8 (real taxilane M5 polyline then simulated door point): " +
                 json.dumps([[round(p[0], 1), round(p[1], 1)] for p in sim.route_to("A8")]))
    lines.append("Taxiway refs in map: " + ", ".join(sorted(sim.MAP["taxiways"])[:60]))
    return "\n".join(lines)


def trace_csv(tr):
    rows = ["t,x,y,v,min_ac,min_push,blast,blast_stand,min_obs"]
    rows += [f"{s['t']},{s['x']},{s['y']},{s['v']},{s['min_ac']},{s['min_push']},{s['blast']},"
             f"{s['blast_stand'] or ''},{s['min_obs']}" for s in tr["steps"]]
    return "\n".join(rows)


def run_all(ctrl_cls, scenarios, params):
    res = []
    for sc in scenarios:
        tr = sim.run(ctrl_cls(), sc)
        res.append({"scenario": sc, "trace": tr, "violations": checks.check(tr, params),
                    "near_misses": checks.near_misses(tr)})
    return res


def deployed():
    if DEPLOYED.exists():
        return json.loads(DEPLOYED.read_text())
    v1 = ROOT / "tugs/tug_v1.py"
    d = {"current": {"path": "tugs/tug_v1.py", "sha256": sha(v1.read_bytes())},
         "history": [{"path": "tugs/tug_v1.py", "sha256": sha(v1.read_bytes()),
                      "event": "initial approved baseline (hand-written)"}]}
    DEPLOYED.write_text(json.dumps(d, indent=1))
    return d


def verify(cand_path, rounds=4, gif=False, deploy=True):
    cand_path = Path(cand_path)
    rel = cand_path.resolve().relative_to(ROOT).as_posix()
    dep = deployed()
    base_src = (ROOT / dep["current"]["path"]).read_text()
    cand_src = cand_path.read_text()
    ctrl = load_controller(cand_path)
    builtin = json.loads((ROOT / "scenarios_builtin.json").read_text())

    ctx_files = {p: (ROOT / p).read_text() for p in
                 ["rules/ground_ops.md", "data/aircraft.json", "sim.py", "checks.py",
                  "tugs/tug_v1.py", "scenarios_builtin.json"]}
    faa = (ROOT / "data/faa_ac_150_5210_20A.txt").read_text(encoding="utf-8", errors="replace")
    system = ("You are AirGuard's independent safety verifier for autonomous airport baggage "
              "tractors. You red-team controller updates in a deterministic simulator. You never "
              "decide pass/fail; deterministic Python checks do. Reply with JSON only.")
    blind = (f"FULL FAA AC 150/5210-20A TEXT:\n{faa}\n\n" +
             "".join(f"=== {k} ===\n{v}\n\n" for k, v in ctx_files.items()) +
             f"=== SFO map summary (OpenStreetMap) ===\n{map_summary()}\n\n{SCHEMA}\n"
             f"STANDS={sim.STAND_REFS} TYPES={list(sim.AIRCRAFT)}\n\n"
             "ROUND 0 (blind spec). You have NOT seen the candidate update. Return JSON "
             '{"invariants": {rule_id: {param: number}}, "scenarios": [up to 6 scenarios]}. '
             "Invariant rule ids and params must come from checks.FLOOR (they can only tighten). "
             "Design adversarial scenarios: engine start at an adjacent stand, pushback across "
             "the tractor path, crew stepping out, fog, GPS drift, debris.")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": blind}]

    log, rejected, all_res, prompt_hashes = [], [], [], []
    r0 = llm.subq(msgs)
    spec = llm.extract_json(r0["text"]) if r0 else None
    spec_source = "subq" if isinstance(spec, dict) else "builtin_floor"
    params, rej = checks.tighten(spec.get("invariants") if spec_source == "subq" else None)
    rejected += rej
    new = []
    if spec_source == "subq":
        msgs.append({"role": "assistant", "content": r0["text"]})
        for i, s in enumerate(spec.get("scenarios", [])[:MAX_SCEN]):
            v = validate_scenario(s, 100 + i)
            (new.append(v) if v else rejected.append(f"invalid scenario #{i}"))
    scenarios = builtin + new                     # built-in floor scenarios always run
    for rnd in range(rounds):
        if rnd > 0:
            prev = all_res[-1]
            diff = "".join(difflib.unified_diff(strip_comments(base_src).splitlines(True),
                                                strip_comments(cand_src).splitlines(True),
                                                "deployed.py", "candidate.py"))
            body = (f"ROUND {rnd} RED-TEAM. Complete traces of round {rnd - 1} follow "
                    "(every 0.1 s step; distances in metres; blast<0 = inside an active jet-blast zone).\n\n" +
                    "\n\n".join(f"### scenario {r['scenario']['name']}\nscenario={json.dumps(r['scenario'])}\n"
                                f"near_misses={json.dumps(r['near_misses'])}\nviolations={json.dumps(r['violations'])}\n"
                                f"{trace_csv(r['trace'])}" for r in prev) +
                    f"\n\nCandidate update (comment-stripped diff vs deployed controller):\n{diff}\n"
                    'Return JSON {"scenarios": [up to 4 NEW, harder scenarios targeting the closest '
                    'near-misses above]} or {"scenarios": []} if you have nothing harder.')
            msgs.append({"role": "user", "content": body})
            rr = llm.subq(msgs)
            if not rr:
                log.append({"round": rnd, "note": "no SubQ (offline/no key/no cache)"}); break
            msgs.append({"role": "assistant", "content": rr["text"]})
            j = llm.extract_json(rr["text"])
            got = j.get("scenarios", []) if isinstance(j, dict) else []
            scenarios = [v for i, s in enumerate(got[:MAX_SCEN])
                         if (v := validate_scenario(s, 1000 * rnd + i))]
            prompt_hashes.append(rr["hash"])
            log.append({"round": rnd, "prompt_tokens": rr["usage"].get("prompt_tokens"),
                        "context_chars": sum(len(json.dumps(m)) for m in msgs),
                        "new_scenarios": len(scenarios)})
            if not scenarios:
                break
        else:
            if r0:
                prompt_hashes.append(r0["hash"])
            log.append({"round": 0, "spec_source": spec_source,
                        "prompt_tokens": r0["usage"].get("prompt_tokens") if r0 else None,
                        "context_chars": sum(len(json.dumps(m)) for m in msgs),
                        "new_scenarios": len(new)})
        res = run_all(ctrl, scenarios, params)
        all_res.append(res)
        print(f"round {rnd}: {len(res)} scenarios, "
              f"{sum(len(r['violations']) for r in res)} violations, context {log[-1].get('context_chars')} chars")
        if any(r["violations"] for r in res):
            break

    flat = [{**v, "scenario": r["scenario"]["name"], "round": i}
            for i, rs in enumerate(all_res) for r in rs for v in r["violations"]]
    verdict = "BLOCK" if flat else "PASS"
    vision = None
    if flat:
        first = flat[0]
        tr = next(r["trace"] for r in all_res[first["round"]] if r["scenario"]["name"] == first["scenario"])
        vs = next(r["violations"] for r in all_res[first["round"]] if r["scenario"]["name"] == first["scenario"])
        png = ROOT / "out" / f"{cand_path.stem}_violation.png"
        png.parent.mkdir(exist_ok=True)
        sim.save_frame(tr, first["t"], png, vs)
        vr = llm.meta([{"role": "user", "content":
                        "This is a top-down frame from an airport apron simulation (green square = "
                        "autonomous baggage tractor, navy = aircraft, orange box = active jet-blast "
                        "zone, red X = safety violation). In two sentences, describe the hazard."}],
                      image=png)
        vision = {"frame": png.relative_to(ROOT).as_posix(),
                  "description": vr["text"] if vr else "vision unavailable (no META key/cache)",
                  "source": "meta" if vr else None}
        if gif:
            sim.animate(tr, ROOT / "out" / f"{cand_path.stem}_violation.gif", vs, stride=10)

    body = {
        "candidate": rel, "verdict": verdict, "violations": flat,
        "spec_source": spec_source, "invariants": params, "rejected_llm_output": rejected,
        "rounds": log, "scenarios_run": [[r["scenario"] for r in rs] for rs in all_res],
        "vision": vision,
        "hashes": {"candidate": sha(cand_src), "candidate_stripped": sha(strip_comments(cand_src)),
                   **{k: sha(v) for k, v in ctx_files.items()}, "faa_txt": sha(faa),
                   "sfo_osm": sha((ROOT / "data/sfo_osm.json").read_bytes()),
                   "llm_prompts": prompt_hashes},
        "data_sources": {"map": "OpenStreetMap Overpass, (c) OpenStreetMap contributors, ODbL (real)",
                         "rules": "FAA AC 150/5210-20A (real, public domain)",
                         "aircraft": "FAA Aircraft Characteristics Database (real); engine/jet-blast approximate",
                         "traffic": "simulated"},
    }
    body["verdict_hash"] = sha(json.dumps(body, sort_keys=True))

    # rollback / promote
    if deploy:
        cur = dep["current"]
        if verdict == "PASS":
            dep["current"] = {"path": rel, "sha256": sha(cand_src)}
            dep["history"].append({**dep["current"], "event": "PASS -> promoted", "verdict_hash": body["verdict_hash"]})
        elif cur["sha256"] == sha(cand_src):
            prev = next((h for h in reversed(dep["history"][:-1])
                         if h["sha256"] != cur["sha256"] and h["event"] != "rollback"), dep["history"][0])
            dep["current"] = {"path": prev["path"], "sha256": prev["sha256"]}
            dep["history"].append({**dep["current"], "event": "rollback", "blocked": rel})
        else:
            dep["history"].append({"path": rel, "sha256": sha(cand_src), "event": "BLOCK -> not deployed",
                                   "verdict_hash": body["verdict_hash"]})
        DEPLOYED.write_text(json.dumps(dep, indent=1))
        body_dep = dep["current"]["path"]
    else:
        body_dep = dep["current"]["path"]

    out = ROOT / "verdicts" / f"{cand_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({**body, "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "deployed_after": body_dep}, indent=1))
    print(f"\n{verdict}  {rel}   verdict_hash={body['verdict_hash'][:16]}  ({out.name})")
    for v in flat[:5]:
        print(f"  {v['rule']:20} scenario={v['scenario']} t={v['t']}s at ({v['x']},{v['y']}) "
              f"measured={v['measured']} limit={v['limit']}\n    {v['citation']}")
    if vision:
        print(f"  vision: {vision['description']}")
    print(f"  deployed: {body_dep}")
    return body


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--gif", action="store_true")
    ap.add_argument("--no-deploy", action="store_true")
    a = ap.parse_args()
    if a.offline:
        llm.OFFLINE = True
    b = verify(a.candidate, a.rounds, a.gif, not a.no_deploy)
    sys.exit(0 if b["verdict"] == "PASS" else 1)
