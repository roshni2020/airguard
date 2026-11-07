"""tug_v3_fixed - HAND-SEEDED fix: +20% cruise, all safety checks kept, longer lookahead, sidesteps debris (added after SubQ round-0 finding)."""
import math
from sim import dist, poly_sdist, footprint_dist

CRUISE = 6.0        # m/s  (+20%)
FOG_SPEED = 2.5     # m/s when visibility < 200 m
LOOKAHEAD = 35.0    # m, longer to match higher speed
BLAST_MARGIN = 8.0  # m
PUSH_KEEP = 25.0    # m from a pushing aircraft
OBS_KEEP = 8.0      # m from people/debris


def ahead(route, i, pos, length, step=2.0):
    pts, cur, left = [pos], pos, length
    for wp in route[i:]:
        d = dist(cur, wp)
        while d > 0 and left > 0:
            s = min(step, d, left)
            cur = (cur[0] + (wp[0] - cur[0]) * s / d, cur[1] + (wp[1] - cur[1]) * s / d)
            pts.append(cur); d -= s; left -= s
    return pts


class Controller:
    def __init__(self):
        self.i = 0

    def step(self, obs):
        pos, route = obs["pos"], obs["route"]
        while self.i < len(route) - 1 and dist(pos, route[self.i]) < 4:
            self.i += 1
        v = CRUISE if obs["visibility"] >= 200 else FOG_SPEED
        dg = dist(pos, obs["goal"])
        if self.i == len(route) - 1:
            v = min(v, max(0.6, 0.4 * dg)) if dg > 1.5 else 0.0
        pts = ahead(route, self.i, pos, LOOKAHEAD)
        live = ("beacon_on", "engines_running", "pushback")
        in_blast = any(a["state"] in live and not a["is_destination"] and poly_sdist(pos, a["blast"]) < 0
                       for a in obs["aircraft"])
        for a in obs["aircraft"]:
            if a["is_destination"]:
                continue
            if a["state"] in ("beacon_on", "engines_running", "pushback"):
                inside_now = poly_sdist(pos, a["blast"]) < 0
                if not inside_now and not in_blast and any(poly_sdist(p, a["blast"]) < BLAST_MARGIN for p in pts):
                    v = 0.0
            if a["state"] == "pushback" and any(footprint_dist(p, a) < PUSH_KEEP for p in pts):
                v = 0.0
        for o in obs["obstacles"]:
            if any(dist(p, o["pos"]) < OBS_KEEP for p in pts):
                if o["kind"] != "debris" or v == 0.0:
                    v = 0.0
                    continue
                a, b = route[max(0, self.i - 1)], route[self.i]
                L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
                ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
                side = (o["pos"][0] + 12 * -uy + 10 * ux, o["pos"][1] + 12 * ux + 10 * uy)
                detour = ahead([side], 0, pos, LOOKAHEAD)
                if any(q["kind"] == "person" and min(dist(p, q["pos"]) for p in detour + pts) < OBS_KEEP
                       for q in obs["obstacles"]):
                    return {"target": route[self.i], "speed": 0.0}      # people first: wait, then detour
                while self.i < len(route) - 1 and dist(route[self.i], o["pos"]) < 15:
                    self.i += 1
                return {"target": side, "speed": min(v, 2.0)}
        for a in obs["aircraft"]:   # inside live blast, or a pushback closing in: escape sideways
            if a["is_destination"] or a["state"] not in live:
                continue
            closing = a["state"] == "pushback" and (footprint_dist(pos, a) < PUSH_KEEP or
                                                    poly_sdist(pos, a["blast"]) < BLAST_MARGIN)
            if closing or poly_sdist(pos, a["blast"]) < 2.0:
                # escape in the blast rectangle's own frame: u = engine->aft axis, n = lateral
                c0, c1, _, c3 = a["blast"]
                Lb, W = dist(c0, c1), dist(c0, c3)
                u = ((c1[0] - c0[0]) / Lb, (c1[1] - c0[1]) / Lb)
                n = ((c3[0] - c0[0]) / W, (c3[1] - c0[1]) / W)
                s = (pos[0] - c0[0]) * u[0] + (pos[1] - c0[1]) * u[1]
                lat = (pos[0] - c0[0]) * n[0] + (pos[1] - c0[1]) * n[1]
                fast = 2.5 if obs["visibility"] < 250 else 4.0
                drift = 1.0 if a["state"] == "pushback" else 0.0       # zone moves aft during pushback
                M = BLAST_MARGIN
                options = [((Lb + M - s) / max(0.5, fast - drift), u),
                           ((W + M - lat) / fast, n),
                           ((lat + M) / fast, (-n[0], -n[1]))]
                people = any(dist(p, o["pos"]) < OBS_KEEP for p in pts for o in obs["obstacles"])
                if not closing and not people:
                    k = next((k for k, p in enumerate(pts) if poly_sdist(p, a["blast"]) >= M), None)
                    if k is not None:
                        options.append((2.0 * k / max(v, fast), None))   # straight on along the route
                _, d = min(options, key=lambda o: o[0])
                if d is None:
                    return {"target": route[self.i], "speed": max(v, fast)}
                return {"target": (pos[0] + 40 * d[0], pos[1] + 40 * d[1]), "speed": fast}
        return {"target": route[self.i], "speed": v}
