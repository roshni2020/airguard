"""Whole-airport fleet simulation: N autonomous baggage tractors on real SFO (OpenStreetMap),
all running the DEPLOYED controller (the one that passed the AirGuard gate).

    py fleet.py [--bots 16] [--minutes 10] [--seed 1]   -> out/airport_replay.js, open airport.html

Routes run over the real airside service-road + taxilane network (Dijkstra). Aircraft at real
stands start engines and push back on their own schedules; other tractors and crew are
obstacles; every bot is scored by the same checks.py invariants.
"""
import argparse, heapq, importlib.util, json, math, random, time
from collections import defaultdict
from pathlib import Path
import sim, checks

ROOT = Path(__file__).parent
DT = 0.1


def load():
    els = json.loads((ROOT / "data/sfo_full_osm.json").read_text())["elements"]
    xy = lambda g: [sim.to_xy(q["lat"], q["lon"]) for q in g]
    m = defaultdict(list)
    for e in els:
        t = e.get("tags", {})
        if e["type"] == "node":
            if t.get("aeroway") == "gate":
                m["gates"].append(sim.to_xy(e["lat"], e["lon"]))
            continue
        if "geometry" not in e:
            continue
        pts, a = xy(e["geometry"]), t.get("aeroway")
        if a == "runway":
            m["runways"].append({"pts": pts, "width": float(str(t.get("width", "60")).split()[0] or 60), "ref": t.get("ref")})
        elif a in ("taxiway", "taxilane"):
            m["taxiways"].append({"pts": pts, "nodes": e["nodes"]})
        elif a == "apron":
            m["aprons"].append(pts)
        elif a == "terminal":
            m["terminals"].append(pts)
        elif a == "jet_bridge":
            m["jet_bridges"].append(pts)
        elif a == "parking_position" and t.get("ref"):
            m["stands"].append({"ref": t["ref"], "pts": pts})
        elif t.get("highway") == "service":
            m["service"].append({"pts": pts, "nodes": e["nodes"]})
        if "building" in t and len(pts) > 2:
            h = t.get("height") or (float(t["building:levels"]) * 3.5 if t.get("building:levels", "").replace(".", "").isdigit() else None)
            try:
                h = float(str(h).split()[0]) if h else 8.0
            except ValueError:
                h = 8.0
            m["buildings"].append({"pts": pts, "h": h, "terminal": a == "terminal"})
    return m


def inside(p, poly):
    x, y, c = p[0], p[1], False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def build_graph(m):
    """Airside graph: service roads touching an apron + taxiways (taxiways cost 3x - tractors prefer roads)."""
    on_apron = lambda pts: any(inside(p, a) for p in pts[:: max(1, len(pts) // 4)] for a in m["aprons"])
    G, P = defaultdict(list), {}
    for kind, ways in (("service", [w for w in m["service"] if on_apron(w["pts"])]), ("taxiway", m["taxiways"])):
        for w in ways:
            for (a, pa), (b, pb) in zip(zip(w["nodes"], w["pts"]), list(zip(w["nodes"], w["pts"]))[1:]):
                P[a], P[b] = pa, pb
                d = sim.dist(pa, pb) * (3.0 if kind == "taxiway" else 1.0)
                G[a].append((b, d)); G[b].append((a, d))
    # keep the largest connected component
    seen, best = set(), []
    for s in G:
        if s in seen:
            continue
        comp, stack = [], [s]; seen.add(s)
        while stack:
            u = stack.pop(); comp.append(u)
            for v, _ in G[u]:
                if v not in seen:
                    seen.add(v); stack.append(v)
        best = max(best, comp, key=len)
    keep = set(best)
    return {u: [(v, d) for v, d in G[u] if v in keep] for u in keep}, {u: P[u] for u in keep}


def dijkstra(G, P, s, t):
    dist, prev, pq = {s: 0}, {}, [(0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == t:
            break
        if d > dist[u]:
            continue
        for v, w in G[u]:
            if d + w < dist.get(v, 1e18):
                dist[v], prev[v] = d + w, u
                heapq.heappush(pq, (d + w, v))
    if t not in dist:
        return None
    path = [t]
    while path[-1] != s:
        path.append(prev[path[-1]])
    return [P[u] for u in reversed(path)]


def keep_right(pts, off=2.0):
    """Drive on the right: offset the polyline so opposing traffic passes ~4 m apart."""
    out = []
    for i, p in enumerate(pts):
        a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
        L = sim.dist(a, b) or 1.0
        out.append((p[0] + off * (b[1] - a[1]) / L, p[1] - off * (b[0] - a[0]) / L))
    return out


def simplify(pts, step=6.0):
    out = [pts[0]]
    for p in pts[1:]:
        if sim.dist(p, out[-1]) >= step:
            out.append(p)
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


class Plane(sim.Aircraft):
    def __init__(self, ref, nose, u, ac_type, events, push):
        self.stand, self.type, self.d = ref, ac_type, sim.AIRCRAFT[ac_type]
        self.nose0, self.u, self.events, self.push_dist = nose, u, events, push


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", type=int, default=16)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    t0 = time.time()
    m = load()
    G, P = build_graph(m)
    nodes = list(P)
    near = lambda p: min(nodes, key=lambda u: (P[u][0] - p[0]) ** 2 + (P[u][1] - p[1]) ** 2)

    # stands: nose = end of the lead-in line nearest a gate / jet bridge
    anchors = m["gates"] + [p for jb in m["jet_bridges"] for p in (jb[0], jb[-1])]
    stands = {}
    for s in m["stands"]:
        p0, p1 = s["pts"][0], s["pts"][-1]
        d0, d1 = (min(sim.dist(p, q) for q in anchors) for p in (p0, p1))
        nose, tail = (p0, p1) if d0 < d1 else (p1, p0)
        if min(d0, d1) > 90 or sim.dist(nose, tail) < 30 or s["ref"] in stands:
            continue
        L = sim.dist(nose, tail)
        stands[s["ref"]] = {"nose": nose, "u": ((tail[0] - nose[0]) / L, (tail[1] - nose[1]) / L), "len": L}
    refs = sorted(stands)
    T = a.minutes * 60
    planes = []
    for ref in refs:
        if rng.random() < 0.25:
            continue                                          # empty stand
        st = stands[ref]
        typ = rng.choice(["B777-300ER", "A350-900"]) if st["len"] > 75 and rng.random() < 0.6 else rng.choice(["A320", "B737-800"])
        ev = []
        if rng.random() < 0.12:                               # departs during the window
            tb = rng.uniform(20, T - 120)
            ev = [{"t": round(tb, 1), "state": "beacon_on"}, {"t": round(tb + rng.uniform(20, 50), 1), "state": "engines_running"},
                  {"t": round(tb + rng.uniform(60, 120), 1), "state": "pushback"}, {"t": round(tb + rng.uniform(170, 220), 1), "state": "gone"}]
        planes.append(Plane(ref, st["nose"], st["u"], typ, ev, round(rng.uniform(35, 55), 1)))
    by_stand = {p.stand: p for p in planes}
    # safety-aware routing: road segments near a parked aircraft cost 25x, inside a blast zone 3x
    g0 = [p.geometry(0) for p in planes]
    def penalty(pa, pb):
        mid = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
        f = 1.0
        for g in g0:
            if sim.dist(mid, g["fuselage"][0]) > 120:
                continue
            if min(sim.footprint_dist(q, g) for q in (pa, mid, pb)) < 8:
                return 25.0
            if sim.poly_sdist(mid, g["blast"]) < 0:
                f = 3.0
        return f
    G = {u: [(v, d * penalty(P[u], P[v])) for v, d in G[u]] for u in G}
    clear = [u for u in nodes if all(sim.footprint_dist(P[u], g) > 25 for g in g0 if sim.dist(P[u], g["fuselage"][0]) < 150)]
    near_clear = lambda p: min(clear, key=lambda u: (P[u][0] - p[0]) ** 2 + (P[u][1] - p[1]) ** 2)

    # depots: graph node nearest each terminal's centroid
    cents = [(sum(x for x, _ in t) / len(t), sum(y for _, y in t) / len(t)) for t in m["terminals"]]
    depots = list({near_clear(c): P[near_clear(c)] for c in cents}.values())

    def door(ref):
        st = stands[ref]
        (nx, ny), (ux, uy) = st["nose"], st["u"]
        return (nx + 12 * ux - 9 * uy, ny + 12 * uy + 9 * ux)

    def plan(src, dst):
        r = dijkstra(G, P, near(src), near(dst))
        if not r:
            return None
        return simplify([src] + keep_right(r) + [dst])

    spec = importlib.util.spec_from_file_location("deployed_ctrl", ROOT / json.loads((ROOT / "deployed.json").read_text())["current"]["path"])
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    deployed_path = json.loads((ROOT / "deployed.json").read_text())["current"]["path"]

    crew = []
    for _ in range(a.bots):
        st = stands[rng.choice(refs)]
        c = (st["nose"][0] + 20 * st["u"][0], st["nose"][1] + 20 * st["u"][1])
        crew.append(sim.Obstacle({"kind": "person", "speed": 1.2, "t0": rng.uniform(0, T),
                                  "path": [[c[0] - 25 * st["u"][1], c[1] + 25 * st["u"][0]], [c[0] + 25 * st["u"][1], c[1] - 25 * st["u"][0]]]}))

    bots = []
    for k in range(a.bots):
        home = depots[k % len(depots)]
        bots.append({"id": k, "x": home[0] + rng.uniform(-3, 3), "y": home[1] + rng.uniform(-3, 3), "hd": rng.uniform(-3, 3), "v": 0.0,
                     "home": home, "phase": "idle", "wait": rng.uniform(0, 20), "route": None, "ctrl": None, "stand": None,
                     "steps": [], "jobs": [], "done": 0, "entered": {}})

    def new_job(b, t):
        ok = [r for r in by_stand if not by_stand[r].events or by_stand[r].events[0]["t"] > t + 200]
        for _ in range(20):
            ref = rng.choice(ok)
            route = plan((b["x"], b["y"]), door(ref))
            if route and len(route) > 2:
                b.update(route=route, ctrl=mod.Controller(), stand=ref, phase="to_stand")
                b["jobs"].append({"t": round(t, 1), "stand": ref, "kind": "deliver"})
                return

    frames, t, N = [], 0.0, int(T / DT)
    for step in range(N + 1):
        t = step * DT
        geoms = [(p, p.geometry(t)) for p in planes]
        crew_pos = [c.pos(t) for c in crew]
        for b in bots:
            pos = (b["x"], b["y"])
            if b["phase"] in ("idle", "loading", "unloading"):
                b["wait"] -= DT
                if b["wait"] <= 0:
                    if b["phase"] == "loading":            # back to the terminal
                        b["route"], b["ctrl"], b["phase"] = plan(pos, b["home"]), mod.Controller(), "to_home"
                        b["jobs"].append({"t": round(t, 1), "stand": b["stand"], "kind": "return"})
                        if not b["route"]:
                            b["phase"], b["wait"] = "idle", 5
                    else:
                        new_job(b, t)
                cmd = {"target": pos, "speed": 0}
            else:
                route, goal = b["route"], b["route"][-1]
                dest = b["stand"] if b["phase"] == "to_stand" else None
                obs = {"t": round(t, 1), "pos": (pos[0] + rng.gauss(0, 0.3), pos[1] + rng.gauss(0, 0.3)), "heading": b["hd"],
                       "speed": b["v"], "route": route, "goal": goal, "visibility": 5000, "sensor_range": 150.0,
                       "aircraft": [], "obstacles": []}
                for p, g in geoms:
                    if g and sim.dist(pos, p.nose0) < 230 and sim.footprint_dist(pos, g) <= 150:
                        obs["aircraft"].append({"stand": p.stand, "type": p.type, "state": p.state(t), "is_destination": p.stand == dest, **g})
                for cp in crew_pos:
                    if sim.dist(cp, pos) < 150:
                        obs["obstacles"].append({"kind": "person", "pos": cp})
                hx, hy = math.cos(b["hd"]), math.sin(b["hd"])
                for o in bots:                               # other tractors: ahead, not oncoming, right-of-way by id
                    if o is b:
                        continue
                    dx, dy = o["x"] - pos[0], o["y"] - pos[1]
                    d = math.hypot(dx, dy)
                    if d < 40 and (dx * hx + dy * hy) / (d or 1) > 0.5:
                        oncoming = math.cos(o["hd"]) * hx + math.sin(o["hd"]) * hy < -0.5
                        if not oncoming and (o["v"] < 0.3 or o["id"] < b["id"] or d < 6):
                            obs["obstacles"].append({"kind": "vehicle", "pos": (o["x"], o["y"])})
                cmd = b["ctrl"].step(obs)
                if sim.dist(pos, goal) < 3.0 and b["v"] < 0.5:
                    if b["phase"] == "to_stand":
                        b["phase"], b["wait"] = "loading", rng.uniform(25, 45)
                    else:
                        b["phase"], b["wait"], b["done"] = "unloading", rng.uniform(10, 20), b["done"] + 1
            # dynamics (same as sim.run)
            tgt = cmd.get("target") or pos
            want = max(0.0, min(sim.TRACTOR["max_speed"], float(cmd.get("speed", 0))))
            dh = (math.atan2(tgt[1] - pos[1], tgt[0] - pos[0]) - b["hd"] + math.pi) % (2 * math.pi) - math.pi
            if sim.dist(tgt, pos) > 0.5:
                b["hd"] += max(-sim.TRACTOR["max_turn"] * DT, min(sim.TRACTOR["max_turn"] * DT, dh))
            b["v"] += max(-sim.TRACTOR["max_decel"] * DT, min(sim.TRACTOR["max_accel"] * DT, want - b["v"]))
            # truth metrics for checks.py (other tractors count as obstacles too)
            dest = b["stand"] if b["phase"] in ("to_stand", "loading") else None
            near_g = [(p, g) for p, g in geoms if g and sim.dist(pos, p.nose0) < 230]
            blast = []
            for p, g in near_g:
                if p.stand == dest:
                    continue
                sd, s_ = sim.poly_sdist(pos, g["blast"]), p.state(t)
                prev = b["entered"].get(p.stand, (999, False))
                lit = max([e["t"] for e in p.events if e["state"] == "beacon_on" and e["t"] <= t] or [t])
                ent = s_ == "beacon_on" and sd < 0 and (prev[1] or (prev[0] >= 0 and t - lit >= sim.BEACON_REACTION_S))
                b["entered"][p.stand] = (sd, ent)
                if s_ in ("engines_running", "pushback") or ent:
                    blast.append((sd, p.stand))
            bd, bs = min(blast) if blast else (999, None)
            others = [sim.dist(pos, (o["x"], o["y"])) for o in bots if o is not b] + [sim.dist(pos, c) for c in crew_pos]
            b["steps"].append({"t": round(t, 1), "x": round(pos[0], 2), "y": round(pos[1], 2), "v": round(b["v"], 2),
                               "min_ac": round(min([sim.footprint_dist(pos, g) for p, g in near_g if p.stand != dest] or [999]), 2),
                               "min_push": round(min([sim.footprint_dist(pos, g) for p, g in near_g if p.state(t) == "pushback"] or [999]), 2),
                               "blast": round(bd, 2), "blast_stand": bs, "min_obs": round(min(others or [999]), 2), "vis": 5000})
            b["x"] += b["v"] * math.cos(b["hd"]) * DT
            b["y"] += b["v"] * math.sin(b["hd"]) * DT
        if step % 5 == 0:
            frames.append({"t": round(t, 1), "b": [[round(b["x"], 1), round(b["y"], 1), round(b["hd"], 2), round(b["v"], 1),
                                                    ["idle", "to_stand", "loading", "to_home", "unloading"].index(b["phase"])] for b in bots],
                           "c": [[round(p[0], 1), round(p[1], 1)] for p in crew_pos]})
        if step % 600 == 0:
            print(f"  t={t:5.0f}s  deliveries={sum(b['done'] for b in bots)}  ({time.time() - t0:.0f}s wall)")

    viol = []
    for b in bots:
        tr = {"scenario": {"deadline": 1e9}, "done_t": 0.0, "steps": b["steps"]}
        for v in checks.check(tr):
            viol.append({**v, "bot": b["id"]})
    r1 = lambda pts: [[round(p[0], 1), round(p[1], 1)] for p in pts]
    BB = (-3500, 3500)
    keep = lambda pts: any(BB[0] < p[0] < BB[1] and BB[0] < p[1] < BB[1] for p in pts)
    out = {"meta": {"controller": deployed_path, "bots": a.bots, "seconds": T, "seed": a.seed, "aircraft": len(planes),
                    "stands": len(stands), "deliveries": sum(b["done"] for b in bots), "violations": len(viol),
                    "violations_by_rule": {r: sum(1 for v in viol if v["rule"] == r) for r in {v["rule"] for v in viol}},
                    "source": "OpenStreetMap contributors (ODbL), SFO; fleet, aircraft schedules and crew simulated"},
           "map": {"runways": [{"pts": r1(r["pts"]), "width": r["width"], "ref": r["ref"]} for r in m["runways"]],
                   "taxiways": [r1(w["pts"]) for w in m["taxiways"]],
                   "service": [r1(w["pts"]) for w in m["service"] if keep(w["pts"])],
                   "aprons": [r1(p) for p in m["aprons"]], "jet_bridges": [r1(p) for p in m["jet_bridges"]],
                   "buildings": [{"pts": r1(b["pts"]), "h": b["h"], "terminal": b["terminal"]} for b in m["buildings"] if keep(b["pts"])],
                   "stands": {k: r1([v["nose"]])[0] for k, v in stands.items()}, "depots": r1(depots)},
           "aircraft": [{"stand": p.stand, "type": p.type, "nose": r1([p.nose0])[0], "u": [round(p.u[0], 4), round(p.u[1], 4)],
                         "events": p.events, "push": p.push_dist, **{k: p.d[k] for k in ("length_m", "wingspan_m")},
                         "eng": p.d["engines"][0], "blast": p.d["jet_blast_m"]["idle"]} for p in planes],
           "jobs": [b["jobs"] for b in bots], "violations": viol, "frames": frames}
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out/airport_replay.js").write_text("window.AIRPORT = " + json.dumps(out, separators=(",", ":")) + ";\n")
    print(f"{a.bots} bots, {len(planes)} aircraft at {len(stands)} real stands, {out['meta']['deliveries']} deliveries, "
          f"{len(viol)} violations {out['meta']['violations_by_rule']} - controller {deployed_path} - {time.time() - t0:.0f}s")
    print("open airport.html")


if __name__ == "__main__":
    main()
