"""tug_v3_fixed - HAND-SEEDED fix: +20% cruise, all safety checks kept, longer lookahead."""
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
                v = 0.0
        for a in obs["aircraft"]:   # a pushing aircraft closing in: back off along the route
            if a["state"] == "pushback" and not a["is_destination"] and footprint_dist(pos, a) < PUSH_KEEP:
                return {"target": route[max(0, self.i - 1)], "speed": 2.0}
        return {"target": route[self.i], "speed": v}
