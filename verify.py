"""AirGuard gate: long-horizon SubQ red-team + deterministic checks -> PASS/BLOCK + rollback.

    py verify.py tugs/tug_v2_ai.py [--rounds 4] [--offline] [--gif] [--no-deploy]
"""
import argparse, copy, difflib, hashlib, importlib.util, io, json, sys, time, tokenize
from pathlib import Path
import sim, checks, llm, stress, alert, agents

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


def run_all(ctrl_cls, scenarios, params, base_cls=None):
    """Safety violations always block. A missed deadline is 'inconclusive' (non-blocking) only
    when the approved controller also cannot complete that scenario - so a frozen candidate
    still fails wherever the approved one delivers."""
    res = []
    for sc in scenarios:
        tr = sim.run(ctrl_cls(), sc)
        v, inc = checks.check(tr, params), []
        if base_cls and any(x["rule"] == "mission_complete" for x in v):
            if any(x["rule"] == "mission_complete" for x in checks.check(sim.run(base_cls(), sc), params)):
                inc = [x for x in v if x["rule"] == "mission_complete"]
                v = [x for x in v if x["rule"] != "mission_complete"]
        res.append({"scenario": sc, "trace": tr, "violations": v, "inconclusive": inc,
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


def stress_context(cand_path, dep, fuzz):
    """Large-scale seeded stress set, scored differentially vs the approved controller.
    Returns (report, text block that goes into SubQ's red-team context)."""
    fz = [stress.random_scenario(i, seed=7) for i in range(fuzz)]
    cand_r = stress.run_many(cand_path, fz)
    base_r = stress.run_many(ROOT / dep["current"]["path"], fz)
    bf = {r["name"] for r in base_r if r["violations"]}
    cf = {r["name"] for r in cand_r if r["violations"]}
    regress = [r for r in cand_r if r["name"] in cf - bf]
    rep = {"n": fuzz, "seed": 7, "baseline": dep["current"]["path"], "baseline_failed": len(bf),
           "candidate_failed": len(cf), "regressions": [r["name"] for r in regress],
           "blocking": len(cf) > len(bf),
           "first_regression": regress[0]["violations"][0] if regress else None,
           "sim_steps": sum(r["steps"] for r in cand_r + base_r)}
    byname = {x["name"]: x for x in fz}
    rows = ["name,dest,vis,gps_noise,n_aircraft,n_obstacles,min_blast,min_push,min_ac,min_obs,max_v,done_t,violations"]
    for r in cand_r:
        sc, nm = byname[r["name"]], r["near_misses"]
        rows.append(f"{r['name']},{sc['destination']},{sc['visibility']},{sc['gps_noise']},"
                    f"{len(sc['aircraft'])},{len(sc['obstacles'])},{nm['min_blast']['value']},"
                    f"{nm['min_push']['value']},{nm['min_ac']['value']},{nm['min_obs']['value']},"
                    f"{nm['max_v']},{nm['done_t']},{'|'.join(v['rule'] for v in r['violations'])}")
    ctx = (f"\n\nLARGE-SCALE STRESS DATA: candidate on {fuzz} seeded random scenarios (approved "
           f"controller failed {len(bf)}, candidate failed {len(cf)}). Every row:\n" + "\n".join(rows) +
           "\n\nREGRESSIONS (candidate fails, approved passes) - full scenarios:\n" +
           "\n".join(json.dumps(byname[r["name"]]) + "\n  violations=" + json.dumps(r["violations"])
                     for r in regress) +
           "\nPrioritise the stress regressions and the closest stress near-misses.")
    print(f"stress: {fuzz} scenarios, approved failed {len(bf)}, candidate failed {len(cf)}, "
          f"{len(regress)} regressions -> {'BLOCKING' if rep['blocking'] else 'non-inferior'}")
    return rep, ctx


SNAP_BUDGET_CHARS = 440_000        # numeric traces are ~1.4 chars/token -> ~320k tokens, under Snap's 393,216


def snap_screen(all_res):
    """Large-data cross-check: SubQ Snap reads the COMPLETE per-step traces of every scenario
    (up to ~300k tokens) and rates each scenario's hazard; we report agreement with the
    deterministic Python verdict. Advisory only - never changes PASS/BLOCK."""
    runs = [r for rs in all_res for r in rs][:120]
    if not runs:
        return None
    stride = 1
    while True:   # full resolution if it fits; otherwise thin every scenario equally (never drop one)
        blocks = []
        for i, r in enumerate(runs):
            rows = trace_csv(r["trace"]).splitlines()
            blocks.append(f"### s{i} {r['scenario']['name']}\n" + "\n".join([rows[0]] + rows[1::stride]))
        state = ("Autonomous baggage tractor runs at SFO. Columns: t (s), x, y (m), v (m/s), min_ac (m to nearest "
                 "aircraft), min_push (m to a pushing-back aircraft), blast (signed m to an ACTIVE jet-blast zone, "
                 "negative = inside), blast_stand, min_obs (m to nearest person/debris).\n\n" +
                 (ROOT / "rules/ground_ops.md").read_text() + "\n\n" + "\n\n".join(blocks))
        if len(state) <= SNAP_BUDGET_CHARS:
            break
        stride += 1
    qs = {f"s{i}": {"type": "noul", "instructions": f"Did the tractor break a safety rule in scenario s{i}?",
                    "criteria": {"true": "Entered an active jet-blast zone, came closer than the rule limits to an aircraft, "
                                         "pushback or person, or exceeded a speed limit",
                                 "false": "Kept every clearance and speed limit"}} for i in range(len(runs))}
    try:
        a = agents.snap("snap-screen").decide(state, qs)
    except Exception as e:        # advisory: never crash the gate
        print(f"  snap screen failed: {str(e)[:120]}")
        return {"error": str(e)[:200]}
    if not a:
        return None
    rows = []
    for i, r in enumerate(runs):
        p = a[f"s{i}"]["noul"]
        safety = [v for v in r["violations"] if v["rule"] != "mission_complete"]
        rows.append({"scenario": r["scenario"]["name"], "p_violation": round(p, 3), "python_violation": bool(safety)})
    agree = sum((x["p_violation"] >= 0.5) == x["python_violation"] for x in rows)
    out = {"scenarios": len(rows), "state_chars": len(state), "trace_stride": stride,
           "agreement": f"{agree}/{len(rows)}", "missed_by_snap": [x["scenario"] for x in rows if x["python_violation"] and x["p_violation"] < 0.5],
           "false_alarms": [x["scenario"] for x in rows if not x["python_violation"] and x["p_violation"] >= 0.5], "rows": rows}
    llm.note(f"agrees with Python on {agree}/{len(rows)}")
    print(f"  snap screen: read {len(state):,} chars (stride {stride}) of raw traces, agrees with Python on {agree}/{len(rows)} scenarios")
    return out


def usage_summary():
    by = {}
    for u in llm.USAGE:
        m = by.setdefault(u["model"], {"calls": 0, "cached": 0, "prompt_tokens": 0,
                                       "completion_tokens": 0, "prompt_chars": 0, "latency_s": 0})
        m["calls"] += 1; m["cached"] += u["cached"]; m["prompt_chars"] += u["prompt_chars"]
        m["prompt_tokens"] += u["prompt_tokens"] or 0
        m["completion_tokens"] += u["completion_tokens"] or 0
        m["latency_s"] = round(m["latency_s"] + u["latency_s"], 2)
    return {"calls": list(llm.USAGE), "by_model": by}


def load_suite():
    """Active versioned scenario library (suite/current.json -> suite/vNNN.json)."""
    cur = ROOT / "suite/current.json"
    if not cur.exists():
        return None
    v = json.loads((ROOT / "suite" / (json.loads(cur.read_text())["version"] + ".json")).read_text())
    return v


def verify(cand_path, rounds=4, gif=False, deploy=True, fuzz=0, alert_mode="both", notify_pass=False, use_suite=True, export=True):
    llm.USAGE.clear()
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
    red = agents.redteam(system)            # long-horizon agent: keeps every round in memory

    log, rejected, all_res, prompt_hashes = [], [], [], []
    r0 = red.ask(blind, 0)
    spec = r0["json"] if r0 else None
    spec_source = "subq" if isinstance(spec, dict) else "builtin_floor"
    suite = load_suite() if use_suite else None
    base_params = checks.tighten(suite["invariants"])[0] if suite else None
    proposed_inv = spec.get("invariants") if spec_source == "subq" else None
    if suite:   # limits are pinned by the versioned suite; SubQ's round-0 limits are logged, not applied
        params, rej = base_params, []       # (they can only enter the suite through improve.py's held-out test)
    else:
        params, rej = checks.tighten(proposed_inv, base_params)
    rejected += rej
    new = []
    if spec_source == "subq":
        for i, s in enumerate(spec.get("scenarios", [])[:MAX_SCEN]):
            v = validate_scenario(s, 100 + i)
            (new.append(v) if v else rejected.append(f"invalid scenario #{i}"))
    if r0:
        llm.note(f"{len(new)} scenarios proposed" if spec_source == "subq" else "unparseable spec")
    lib = [x for x in (suite["scenarios"] if suite else []) if x["name"] not in {b["name"] for b in builtin}]
    scenarios = builtin + lib + [x for x in new if x["name"] not in {l["name"] for l in lib}]   # floor + library always run
    stress_rep, stress_ctx = stress_context(cand_path, dep, fuzz) if fuzz else (None, "")
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
                    'near-misses above]} or {"scenarios": []} if you have nothing harder.'
                    + (stress_ctx if rnd == 1 else ""))
            rr = red.ask(body, rnd)
            if not rr:
                log.append({"round": rnd, "note": "no SubQ (offline/no key/no cache/over budget)"}); break
            j = rr["json"]
            got = j.get("scenarios", []) if isinstance(j, dict) else []
            scenarios = [v for i, s in enumerate(got[:MAX_SCEN])
                         if (v := validate_scenario(s, 1000 * rnd + i))]
            llm.note(f"{len(scenarios)} scenarios proposed")
            prompt_hashes.append(rr["hash"])
            log.append({"round": rnd, "prompt_tokens": rr["usage"].get("prompt_tokens"),
                        "context_chars": red.context_chars(),
                        "new_scenarios": len(scenarios)})
            if not scenarios:
                break
        else:
            if r0:
                prompt_hashes.append(r0["hash"])
            log.append({"round": 0, "spec_source": spec_source,
                        "prompt_tokens": r0["usage"].get("prompt_tokens") if r0 else None,
                        "context_chars": red.context_chars(),
                        "new_scenarios": len(new)})
        res = run_all(ctrl, scenarios, params, load_controller(ROOT / dep["current"]["path"]))
        all_res.append(res)
        print(f"round {rnd}: {len(res)} scenarios, "
              f"{sum(len(r['violations']) for r in res)} violations, context {log[-1].get('context_chars')} chars")
        if any(r["violations"] for r in res):
            break

    flat = [{**v, "scenario": r["scenario"]["name"], "round": i}
            for i, rs in enumerate(all_res) for r in rs for v in r["violations"]]
    if stress_rep and stress_rep["blocking"]:
        fv = stress_rep["first_regression"] or {}
        flat.append({"rule": "stress_noninferiority", "scenario": stress_rep["regressions"][0],
                     "t": fv.get("t"), "x": fv.get("x"), "y": fv.get("y"), "stand": fv.get("stand"),
                     "measured": stress_rep["candidate_failed"], "limit": stress_rep["baseline_failed"],
                     "unit": "failed stress scenarios",
                     "citation": "differential gate: must not fail more seeded stress scenarios than the "
                                 "approved controller; first regression: " + str(fv.get("citation", ""))})
    verdict = "BLOCK" if flat else "PASS"
    vision = None
    sim_flat = [v for v in flat if "round" in v]
    if sim_flat:
        first = sim_flat[0]
        tr = next(r["trace"] for r in all_res[first["round"]] if r["scenario"]["name"] == first["scenario"])
        vs = next(r["violations"] for r in all_res[first["round"]] if r["scenario"]["name"] == first["scenario"])
        png = ROOT / "out" / f"{cand_path.stem}_violation.png"
        png.parent.mkdir(exist_ok=True)
        sim.save_frame(tr, first["t"], png, vs)
        vr = agents.vision().ask("This is a top-down frame from an airport apron simulation (green square = "
                                 "autonomous baggage tractor, navy = aircraft, orange box = active jet-blast "
                                 "zone, red X = safety violation). In two sentences, describe the hazard.", image=png)
        if vr:
            llm.note("described frame")
        vision = {"frame": png.relative_to(ROOT).as_posix(),
                  "description": vr["text"] if vr else "vision unavailable (no META key/cache)",
                  "source": "meta" if vr else None}
        if gif:
            sim.animate(tr, ROOT / "out" / f"{cand_path.stem}_violation.gif", vs, stride=10)

    body = {
        "candidate": rel, "verdict": verdict, "violations": flat,
        "spec_source": spec_source, "invariants": params, "rejected_llm_output": rejected,
        "rounds": log, "scenarios_run": [[r["scenario"] for r in rs] for rs in all_res],
        "vision": vision, "stress": stress_rep, "subq_proposed_invariants": proposed_inv,
        "suite_version": suite["version"] if suite else None,
        "inconclusive": [{**x, "scenario": r["scenario"]["name"], "round": i, "note": "approved controller also misses the deadline"}
                         for i, rs in enumerate(all_res) for r in rs for x in r["inconclusive"]],
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

    triage = None
    if flat:   # SubQ Snap: advisory severity triage for ops (never changes PASS/BLOCK)
        rules_md = (ROOT / "rules/ground_ops.md").read_text()
        qs = {f"v{i}": {"type": "score", "criteria": ["minor", "serious", "critical"],
                        "instructions": f"How severe is violation v{i} ({v['rule']} in scenario {v['scenario']}) for ground crew and aircraft?"}
              for i, v in enumerate(flat[:100])}
        qs["page_ops_now"] = {"type": "noul", "instructions": "Should airport operations be phoned immediately?",
                              "criteria": {"true": "Real risk of collision or jet-blast injury", "false": "Minor or procedural"}}
        try:
            a = agents.snap("snap-triage").decide(rules_md + "\n\nVIOLATIONS:\n" + json.dumps(flat, indent=1), qs)
        except Exception as e:    # advisory: alert falls back to call+sms
            print(f"  snap triage failed: {str(e)[:120]}"); a = None
        if a:
            labels = ["minor", "serious", "critical"]
            for i, v in enumerate(flat[:100]):
                v_sev = a[f"v{i}"]
                triage = triage or {"page_ops_now": round(a["page_ops_now"]["noul"], 3), "severity": []}
                triage["severity"].append({"rule": v["rule"], "scenario": v["scenario"],
                                           "label": labels[min(2, round(v_sev["score"]))],
                                           "score": round(v_sev["score"], 2)})
            llm.note(max(triage["severity"], key=lambda s: s["score"])["label"])
            print(f"  snap triage: page_ops_now={triage['page_ops_now']}, worst="
                  f"{max(triage['severity'], key=lambda s: s['score'])['label']}")
    screen = snap_screen(all_res)
    alert_res = alert.alert({**body, "deployed_after": body_dep, "triage": triage}, alert_mode, notify_pass)
    usage = usage_summary()          # outside verdict_hash: latency varies run to run
    builtin_names = {b["name"] for b in builtin}
    runs = [{"round": i, "source": "builtin" if r["scenario"]["name"] in builtin_names else "subq",
             "trace": r["trace"], "violations": r["violations"]} for i, rs in enumerate(all_res) for r in rs]
    if export:   # selfcheck passes export=False so it never overwrites the published replays
        sim.export_replay(ROOT / "out" / f"replay_{cand_path.stem}.js", runs,
                      {**body, "deployed_after": body_dep, "usage": usage, "triage": triage})
    out = ROOT / "verdicts" / f"{cand_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    llm.RUN["verdict_file"] = out.name
    calls = {"run_id": llm.RUN["run_id"], "n": len(llm.USAGE), "by_agent": {}}
    for u in llm.USAGE:
        g = calls["by_agent"].setdefault(u["agent"], {"calls": 0, "cached": 0, "prompt_tokens": 0, "latency_s": 0.0})
        g["calls"] += 1; g["cached"] += u["cached"]; g["prompt_tokens"] += u["prompt_tokens"] or 0
        g["latency_s"] = round(g["latency_s"] + u["latency_s"], 2)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({**body, "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "deployed_after": body_dep, "usage": usage, "triage": triage,
                               "alert": alert_res, "calls": calls, "snap_screen": screen}, indent=1))
    llm.flush_calls()
    print(f"\n{verdict}  {rel}   verdict_hash={body['verdict_hash'][:16]}  ({out.name})")
    for v in flat[:5]:
        print(f"  {v['rule']:20} scenario={v['scenario']} t={v['t']}s at ({v['x']},{v['y']}) "
              f"measured={v['measured']} limit={v['limit']}\n    {v['citation']}")
    if vision:
        print(f"  vision: {vision['description']}")
    for mdl, m in usage["by_model"].items():
        print(f"  usage {mdl}: {m['calls']} calls ({m['cached']} cached), {m['prompt_tokens']:,} prompt tok, "
              f"{m['completion_tokens']:,} completion tok, {m['prompt_chars']:,} prompt chars, {m['latency_s']}s")
    print(f"  replay: open viewer.html#out/replay_{cand_path.stem}.js")
    print(f"  deployed: {body_dep}")
    return body


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", nargs="?")
    ap.add_argument("--candidate", dest="candidate_opt")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--gif", action="store_true")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--fuzz", type=int, default=0, help="add N seeded stress scenarios (differential)")
    ap.add_argument("--alert", choices=["call", "sms", "both", "off"], default="both")
    ap.add_argument("--no-alert", action="store_true", help="same as --alert off")
    ap.add_argument("--notify-pass", action="store_true", help="SMS on PASS too")
    a = ap.parse_args()
    if a.offline:
        llm.OFFLINE = True
    cand = a.candidate_opt or a.candidate
    if not cand:
        ap.error("candidate required (positional or --candidate)")
    b = verify(cand, a.rounds, a.gif, not a.no_deploy, a.fuzz, "off" if a.no_alert else a.alert, a.notify_pass)
    sys.exit(0 if b["verdict"] == "PASS" else 1)
