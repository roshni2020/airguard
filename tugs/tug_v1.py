"""tug_v1 - APPROVED baseline controller (hand-written, conservative)."""
import math
from sim import dist, poly_sdist, footprint_dist

CRUISE = 5.0        # m/s
FOG_SPEED = 2.5     # m/s when visibility < 200 m
LOOKAHEAD = 25.0    # m of route checked ahead
BLAST_MARGIN = 5.0  # m, covers GPS noise
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
        for a in obs["aircraft"]:
            if a["is_destination"]:
                continue
            if a["state"] in ("beacon_on", "engines_running", "pushback"):
                inside_now = poly_sdist(pos, a["blast"]) < 0
                if not inside_now and any(poly_sdist(p, a["blast"]) < BLAST_MARGIN for p in pts):
                    v = 0.0
            if a["state"] == "pushback" and any(footprint_dist(p, a) < PUSH_KEEP for p in pts):
                v = 0.0
        for o in obs["obstacles"]:
            if any(dist(p, o["pos"]) < OBS_KEEP for p in pts):
                v = 0.0
        return {"target": route[self.i], "speed": v}
