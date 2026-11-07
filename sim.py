"""AirGuard apron simulator on real SFO geometry (OpenStreetMap, ODbL).

Deterministic: fixed dt, seeded RNG. The controller only sees `obs` (noisy GPS,
sensor range shrinks in fog). Truth metrics are recorded per step for checks.py.
"""
import json, math, random
from pathlib import Path

ROOT = Path(__file__).parent
DT = 0.1
LAT0, LON0 = 37.6125, -122.3890
ACTIVE = ("beacon_on", "engines_running", "pushback")   # jet-blast zone live
STAND_REFS = ["A6", "A7", "A8", "A11", "B11", "B16", "B19", "B20"]
TRACTOR = dict(max_accel=1.0, max_decel=2.5, max_turn=math.radians(30), max_speed=12.0)
PUSH_SPEED = 1.0
BEACON_REACTION_S = 3.0   # time to see a beacon and brake before entering counts as a violation


# ---------- geometry ----------
def to_xy(lat, lon):
    return ((lon - LON0) * math.cos(math.radians(LAT0)) * 111320, (lat - LAT0) * 110540)

def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])

def seg_dist(p, a, b):
    ax, ay = b[0] - a[0], b[1] - a[1]
    L = ax * ax + ay * ay
    t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L))
    return dist(p, (a[0] + t * ax, a[1] + t * ay))

def poly_sdist(p, poly):
    """Signed distance to a convex polygon (negative = inside)."""
    d = min(seg_dist(p, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))
    s = [(b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
         for a, b in zip(poly, poly[1:] + poly[:1])]
    return -d if all(v >= 0 for v in s) or all(v <= 0 for v in s) else d


# ---------- real map ----------
def load_map():
    els = json.loads((ROOT / "data/sfo_osm.json").read_text())["elements"]
    m = {"stands": {}, "taxiways": {}, "aprons": [], "terminals": [], "parking": []}
    for e in els:
        tags, g = e.get("tags", {}), e.get("geometry")
        if not g:
            continue
        pts = [to_xy(q["lat"], q["lon"]) for q in g]
        kind, ref = tags.get("aeroway"), tags.get("ref")
        if kind == "parking_position":
            m["parking"].append(pts)
            # OSM stand lines run tail -> nose; keep the longest line per stand
            if ref in STAND_REFS:
                L = dist(pts[0], pts[-1])
                if ref not in m["stands"] or L > m["stands"][ref]["len"]:
                    m["stands"][ref] = {"nose": pts[-1], "tail": pts[0], "len": L}
        elif kind == "taxiway":
            m["taxiways"].setdefault(ref or f"tw{e['id']}", []).append(pts)
        elif kind in ("apron", "terminal"):
            m[kind + "s"].append(pts)
    return m

MAP = load_map()
AIRCRAFT = {k: v for k, v in json.loads((ROOT / "data/aircraft.json").read_text()).items()
            if not k.startswith("_")}

def route_to(dest):
    """Service route: south along taxilane M5 (real OSM polyline), then to the
    destination aircraft's baggage door (simulated: 12 m aft of nose, 9 m port side)."""
    m5 = max(MAP["taxiways"]["M5"], key=len)
    m5 = sorted(m5, key=lambda p: -p[1])                    # north -> south
    door = door_point(dest)
    k = min(range(len(m5)), key=lambda i: dist(m5[i], door))
    return [m5[i] for i in range(k + 1)] + [door]

def stand_frame(ref):
    s = MAP["stands"][ref]
    ux, uy = s["tail"][0] - s["nose"][0], s["tail"][1] - s["nose"][1]
    L = math.hypot(ux, uy)
    return s["nose"], (ux / L, uy / L)                      # nose, unit vector nose->tail

def door_point(ref, ac_type="A320"):
    (nx, ny), (ux, uy) = stand_frame(ref)
    return (nx + 12 * ux - 9 * uy, ny + 12 * uy + 9 * ux)


# ---------- world entities ----------
class Aircraft:
    def __init__(self, spec):
        self.stand, self.type = spec["stand"], spec.get("type", "A320")
        self.d = AIRCRAFT[self.type]
        self.nose0, self.u = stand_frame(self.stand)
        self.events = sorted(spec.get("events", []), key=lambda e: e["t"])
        self.push_dist = spec.get("push_dist", 40.0)

    def state(self, t):
        s = "parked"
        for e in self.events:
            if e["t"] <= t:
                s = e["state"]
        return s

    def offset(self, t):
        """Tail-first pushback distance travelled by time t."""
        t0 = next((e["t"] for e in self.events if e["state"] == "pushback"), None)
        return 0.0 if t0 is None or t < t0 else min(self.push_dist, (t - t0) * PUSH_SPEED)

    def geometry(self, t):
        if self.state(t) == "gone":
            return None
        ux, uy = self.u
        o = self.offset(t)
        n = (self.nose0[0] + o * ux, self.nose0[1] + o * uy)
        P = lambda along, side: (n[0] + along * ux - side * uy, n[1] + along * uy + side * ux)
        L, W = self.d["length_m"], self.d["wingspan_m"]
        ex, ey = self.d["engines"][0]["x"], self.d["engines"][0]["y"]
        bl, hw = self.d["jet_blast_m"]["idle"], ey + 10
        return {"fuselage": (P(0, 0), P(L, 0)),
                "wing": (P(0.45 * L, -W / 2), P(0.45 * L, W / 2)),
                "blast": [P(ex, -hw), P(ex + bl, -hw), P(ex + bl, hw), P(ex, hw)]}

def footprint_dist(p, g):
    return min(seg_dist(p, *g["fuselage"]) - 2.0, seg_dist(p, *g["wing"]) - 0.5)

class Obstacle:
    """Person or debris: walks along `path` at `speed` from t0 (debris: speed 0)."""
    def __init__(self, spec):
        self.kind, self.path = spec.get("kind", "person"), [tuple(p) for p in spec["path"]]
        self.speed, self.t0 = spec.get("speed", 1.2), spec.get("t0", 0.0)

    def pos(self, t):
        s = max(0.0, t - self.t0) * self.speed
        for a, b in zip(self.path, self.path[1:]):
            L = dist(a, b)
            if s <= L:
                f = s / L if L else 0
                return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
            s -= L
        return self.path[-1]


# ---------- run ----------
def run(controller, scenario):
    sc = scenario
    rng = random.Random(sc.get("seed", 0))
    dest = sc.get("destination", "A8")
    route = route_to(dest)
    goal = route[-1]
    aircraft = [Aircraft(a) for a in sc.get("aircraft", [])]
    if not any(a.stand == dest for a in aircraft):
        aircraft.append(Aircraft({"stand": dest, "type": "A320"}))
    obstacles = [Obstacle(o) for o in sc.get("obstacles", [])]
    vis, noise = sc.get("visibility", 5000), sc.get("gps_noise", 0.3)
    rng_sensor = min(150.0, vis)
    x, y = route[0]
    hd = math.atan2(route[1][1] - y, route[1][0] - x)
    v, t, done_t, steps, entered = 0.0, 0.0, None, [], {}
    T = sc.get("duration", sc.get("deadline", 240) + 10)
    while t <= T + 1e-9:
        geoms = [(a, a.geometry(t)) for a in aircraft]
        obs = {"t": round(t, 1), "pos": (x + rng.gauss(0, noise), y + rng.gauss(0, noise)),
               "heading": hd, "speed": v, "route": route, "goal": goal, "visibility": vis,
               "sensor_range": rng_sensor, "aircraft": [], "obstacles": []}
        for a, g in geoms:
            if g and footprint_dist((x, y), g) <= rng_sensor:
                obs["aircraft"].append({"stand": a.stand, "type": a.type, "state": a.state(t),
                                        "is_destination": a.stand == dest, **g})
        for o in obstacles:
            p = o.pos(t)
            if dist(p, (x, y)) <= rng_sensor:
                obs["obstacles"].append({"kind": o.kind, "pos": p})
        cmd = controller.step(obs) if done_t is None else {"target": goal, "speed": 0}
        # truth metrics
        min_ac = min([footprint_dist((x, y), g) for a, g in geoms if g and a.stand != dest] or [999])
        min_push = min([footprint_dist((x, y), g) for a, g in geoms
                        if g and a.state(t) == "pushback"] or [999])
        # jet blast: inside while engines run / pushback, OR entering while the beacon is on.
        # Being caught inside the instant a beacon lights is not a violation (you must then exit
        # before engine start - otherwise the engines_running part fires).
        blast = []
        for a, g in geoms:
            if not g or a.stand == dest:
                continue
            sd, st_ = poly_sdist((x, y), g["blast"]), a.state(t)
            prev = entered.get(a.stand, (999, False))
            lit = max([e["t"] for e in a.events if e["state"] == "beacon_on" and e["t"] <= t] or [t])
            ent = st_ == "beacon_on" and sd < 0 and (prev[1] or (prev[0] >= 0 and t - lit >= BEACON_REACTION_S))
            entered[a.stand] = (sd, ent)
            if st_ in ("engines_running", "pushback") or ent:
                blast.append((sd, a.stand))
        bd, bs = min(blast) if blast else (999, None)
        min_obs = min([dist((x, y), o.pos(t)) for o in obstacles] or [999])
        steps.append({"t": round(t, 1), "x": round(x, 2), "y": round(y, 2), "v": round(v, 2),
                      "min_ac": round(min_ac, 2), "min_push": round(min_push, 2),
                      "blast": round(bd, 2), "blast_stand": bs, "min_obs": round(min_obs, 2),
                      "vis": vis})
        if done_t is None and dist((x, y), goal) < 3.0 and v < 0.5:
            done_t = t
        # dynamics
        tgt = cmd.get("target") or (x, y)
        want = max(0.0, min(TRACTOR["max_speed"], float(cmd.get("speed", 0))))
        dh = (math.atan2(tgt[1] - y, tgt[0] - x) - hd + math.pi) % (2 * math.pi) - math.pi
        if dist(tgt, (x, y)) > 0.5:
            hd += max(-TRACTOR["max_turn"] * DT, min(TRACTOR["max_turn"] * DT, dh))
        dv = want - v
        v += max(-TRACTOR["max_decel"] * DT, min(TRACTOR["max_accel"] * DT, dv))
        x, y = x + v * math.cos(hd) * DT, y + v * math.sin(hd) * DT
        t += DT
        if done_t is not None and t > done_t + 1:
            break
    return {"scenario": sc, "destination": dest, "route": route,
            "done_t": None if done_t is None else round(done_t, 1), "steps": steps}


# ---------- rendering ----------
def _base(ax):
    for poly in MAP["aprons"]:
        ax.fill(*zip(*poly), color="#e6e6e6", zorder=0)
    for poly in MAP["terminals"]:
        ax.fill(*zip(*poly), color="#9aa7b8", zorder=1)
    for ways in MAP["taxiways"].values():
        for w in ways:
            ax.plot(*zip(*w), color="#d9b44a", lw=0.8, zorder=1)
    for w in MAP["parking"]:
        ax.plot(*zip(*w), color="#bbbbbb", lw=0.5, zorder=1)
    for ref in STAND_REFS:
        ax.text(*MAP["stands"][ref]["nose"], ref, fontsize=7, zorder=5)
    ax.set_xlim(-120, 360); ax.set_ylim(-260, 140); ax.set_aspect("equal")

def _dyn(ax, trace, i, violations):
    sc, st = trace["scenario"], trace["steps"][i]
    t = st["t"]
    arts = []
    for spec in sc.get("aircraft", []) + [{"stand": trace["destination"]}]:
        a = Aircraft(spec)
        g = a.geometry(t)
        if not g:
            continue
        on = a.state(t) in ACTIVE
        arts += ax.plot(*zip(*g["fuselage"]), color="navy", lw=3, zorder=3)
        arts += ax.plot(*zip(*g["wing"]), color="navy", lw=2, zorder=3)
        arts += ax.fill(*zip(*g["blast"]), color="orange" if on else "none",
                        alpha=0.35, ec="orange", ls="--", zorder=2)
    for o in sc.get("obstacles", []):
        arts += ax.plot(*Obstacle(o).pos(t), "o", color="purple", ms=4, zorder=4)
    xs = [s["x"] for s in trace["steps"][:i + 1]]
    ys = [s["y"] for s in trace["steps"][:i + 1]]
    arts += ax.plot(xs, ys, color="green", lw=1, zorder=4)
    arts += ax.plot(st["x"], st["y"], "s", color="green", ms=6, zorder=6)
    for vi in violations:
        if vi["t"] <= t:
            arts += ax.plot(vi["x"], vi["y"], "x", color="red", ms=12, mew=3, zorder=7)
            arts.append(ax.text(vi["x"] + 4, vi["y"] + 4, vi["rule"], color="red", fontsize=7, zorder=7))
    arts.append(ax.set_title(f"{sc.get('name', '')}  t={t:.1f}s  v={st['v']:.1f} m/s  vis={st['vis']} m",
                             fontsize=9))
    return arts

def save_frame(trace, t, path, violations=()):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    i = min(range(len(trace["steps"])), key=lambda k: abs(trace["steps"][k]["t"] - t))
    fig, ax = plt.subplots(figsize=(8, 6.5)); _base(ax); _dyn(ax, trace, i, violations)
    fig.text(0.01, 0.01, "Map (c) OpenStreetMap contributors, ODbL. Aircraft/tractor simulated.", fontsize=6)
    fig.savefig(path, dpi=110); plt.close(fig)
    return path

def animate(trace, path=None, violations=(), stride=5, show=False):
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation, PillowWriter
    fig, ax = plt.subplots(figsize=(8, 6.5)); _base(ax)
    fig.text(0.01, 0.01, "Map (c) OpenStreetMap contributors, ODbL. Aircraft/tractor simulated.", fontsize=6)
    held = []
    def upd(i):
        for a in held:
            a.remove()
        held[:] = [a for a in _dyn(ax, trace, i, violations) if a is not ax.title]
        return held
    anim = FuncAnimation(fig, upd, frames=range(0, len(trace["steps"]), stride), interval=50)
    if path:
        anim.save(path, writer=PillowWriter(fps=20))
    if show:
        plt.show()
    plt.close(fig)


# ---------- replay export for the three.js viewer ----------
def export_replay(path, runs, meta, stride=5):
    """Write out/replay_*.js (`window.AIRGUARD_REPLAY = {...}`) - loads over file://, no server."""
    BX, BY = (-200, 450), (-320, 200)
    inside = lambda pts: any(BX[0] <= p[0] <= BX[1] and BY[0] <= p[1] <= BY[1] for p in pts)
    r1 = lambda pts: [[round(p[0], 1), round(p[1], 1)] for p in pts]
    mp = {"aprons": [r1(p) for p in MAP["aprons"] if inside(p)],
          "terminals": [r1(p) for p in MAP["terminals"] if inside(p)],
          "taxiways": [r1(w) for ws in MAP["taxiways"].values() for w in ws if inside(w)],
          "parking": [r1(w) for w in MAP["parking"] if inside(w)],
          "stands": {k: r1([v["nose"]])[0] for k, v in MAP["stands"].items()}}
    out_runs = []
    for run_ in runs:
        tr = run_["trace"]
        sc = tr["scenario"]
        acs = [Aircraft(a) for a in sc.get("aircraft", [])]
        if not any(a.stand == tr["destination"] for a in acs):
            acs.append(Aircraft({"stand": tr["destination"]}))
        obs = [Obstacle(o) for o in sc.get("obstacles", [])]
        frames = []
        for s in tr["steps"][::stride]:
            t = s["t"]
            ac = []
            for a in acs:
                g = a.geometry(t)
                if g:
                    ac.append([a.stand, a.type, a.state(t), *sum(r1(g["fuselage"]), []),
                               *sum(r1(g["wing"]), []), *sum(r1(g["blast"]), [])])
            frames.append({"t": t, "x": s["x"], "y": s["y"], "v": s["v"], "blast": s["blast"],
                           "min_ac": s["min_ac"], "min_push": s["min_push"], "min_obs": s["min_obs"],
                           "ac": ac, "ob": [[o.kind, *r1([o.pos(t)])[0]] for o in obs]})
        out_runs.append({"round": run_["round"], "source": run_["source"], "name": sc.get("name"),
                         "destination": tr["destination"], "visibility": sc.get("visibility", 5000),
                         "gps_noise": sc.get("gps_noise", 0.3), "done_t": tr["done_t"],
                         "deadline": sc.get("deadline", 240), "route": r1(tr["route"]),
                         "violations": run_["violations"], "frames": frames})
    Path(path).parent.mkdir(exist_ok=True)
    Path(path).write_text("window.AIRGUARD_REPLAY = " +
                          json.dumps({"map": mp, "meta": meta, "runs": out_runs}, separators=(",", ":")) + ";\n")
    return path
