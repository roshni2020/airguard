"""Deterministic invariant checks. LLMs never decide pass/fail - this file does.

FLOOR always applies. Verifier-proposed params are merged with `tighten`: they can
only make a rule stricter, never weaker, and unknown rule types are rejected.
"""
CITE = {
    "max_speed": "AC 150/5210-20A Ch.3 item 5 (operator establishes speed limits; value is an example)",
    "jet_blast_zone": "AC 150/5210-20A App. B 2.2.1(c) avoid jet blast; 2.2.1(g) beacons illuminated; 2.2.3",
    "pushback_clearance": "AC 150/5210-20A App. B 2.2.1(f) aircraft ALWAYS have right-of-way; 4.7.2",
    "aircraft_clearance": "AC 150/5210-20A App. B 2.2.2 driving close to aircraft prohibited; 4.7.2",
    "personnel_clearance": "AC 150/5210-20A App. B 2.2.1(f) yield to passengers",
    "low_visibility_speed": "AC 150/5210-20A App. B 2.2.4 poor weather / fog",
    "mission_complete": "operational requirement (not FAA): a frozen tractor must not pass",
}
FLOOR = {
    "max_speed": {"limit": 6.9},
    "jet_blast_zone": {"margin": 0.0},
    "pushback_clearance": {"min_dist": 15.0},
    "aircraft_clearance": {"min_dist": 5.0},
    "personnel_clearance": {"min_dist": 3.0},
    "low_visibility_speed": {"visibility_below": 200.0, "limit": 2.8},
    "mission_complete": {},
}
# which direction is "stricter" for each numeric param
STRICT = {"limit": min, "margin": max, "min_dist": max, "visibility_below": max}
BOUNDS = {"limit": (0.5, 20), "margin": (0, 50), "min_dist": (0, 100), "visibility_below": (0, 10000)}


def tighten(proposed, base=None):
    """Merge verifier params into the floor (or an already-tightened base). Returns (params, rejected_notes)."""
    out = {k: dict(v) for k, v in (base or FLOOR).items()}
    rejected = []
    for rule, params in (proposed or {}).items():
        if rule not in FLOOR or not isinstance(params, dict):
            rejected.append(f"unknown rule {rule!r}")
            continue
        for k, val in params.items():
            lo_hi = BOUNDS.get(k)
            if k not in FLOOR[rule] or not isinstance(val, (int, float)) or not lo_hi \
                    or not lo_hi[0] <= val <= lo_hi[1]:
                rejected.append(f"bad param {rule}.{k}={val!r}")
                continue
            out[rule][k] = STRICT[k](out[rule][k], float(val))
    return out, rejected


def _episodes(steps, rule, bad, measured, limit, unit):
    """Contiguous violation intervals -> one record each, with the worst sample."""
    out, cur = [], None
    for s in steps:
        if bad(s):
            if cur is None:
                cur = {"rule": rule, "t": s["t"], "x": s["x"], "y": s["y"], "t_end": s["t"],
                       "measured": measured(s), "limit": limit, "unit": unit,
                       "citation": CITE[rule]}
            cur["t_end"] = s["t"]
            m = measured(s)
            if (unit == "m/s" and m > cur["measured"]) or (unit != "m/s" and m < cur["measured"]):
                cur["measured"] = m
            if rule == "jet_blast_zone":
                cur["stand"] = s["blast_stand"]
        elif cur:
            out.append(cur); cur = None
    if cur:
        out.append(cur)
    return out


def check(trace, params=None):
    p = params or FLOOR
    st = trace["steps"]
    v = []
    v += _episodes(st, "max_speed", lambda s: s["v"] > p["max_speed"]["limit"] + 1e-6,
                   lambda s: s["v"], p["max_speed"]["limit"], "m/s")
    lv = p["low_visibility_speed"]
    v += _episodes(st, "low_visibility_speed",
                   lambda s: s["vis"] < lv["visibility_below"] and s["v"] > lv["limit"] + 1e-6,
                   lambda s: s["v"], lv["limit"], "m/s")
    v += _episodes(st, "jet_blast_zone", lambda s: s["blast"] < p["jet_blast_zone"]["margin"],
                   lambda s: s["blast"], p["jet_blast_zone"]["margin"], "m (signed dist to zone)")
    v += _episodes(st, "pushback_clearance",
                   lambda s: s["min_push"] < p["pushback_clearance"]["min_dist"],
                   lambda s: s["min_push"], p["pushback_clearance"]["min_dist"], "m")
    v += _episodes(st, "aircraft_clearance",
                   lambda s: s["min_ac"] < p["aircraft_clearance"]["min_dist"],
                   lambda s: s["min_ac"], p["aircraft_clearance"]["min_dist"], "m")
    # a stationary tractor that a person walks past is yielding, not violating
    v += _episodes(st, "personnel_clearance",
                   lambda s: s["v"] > 0.3 and s["min_obs"] < p["personnel_clearance"]["min_dist"],
                   lambda s: s["min_obs"], p["personnel_clearance"]["min_dist"], "m")
    deadline = trace["scenario"].get("deadline", 240)
    if trace["done_t"] is None or trace["done_t"] > deadline:
        last = st[-1]
        v.append({"rule": "mission_complete", "t": last["t"], "x": last["x"], "y": last["y"],
                  "t_end": last["t"], "measured": trace["done_t"], "limit": deadline, "unit": "s",
                  "citation": CITE["mission_complete"]})
    v.sort(key=lambda r: r["t"])
    return v


def near_misses(trace, params=None):
    """Closest approach per metric - fed back to the red-team verifier."""
    st = trace["steps"]
    def mn(k):
        s = min(st, key=lambda s: s[k])
        return {"value": s[k], "t": s["t"], "x": s["x"], "y": s["y"]}
    return {"min_blast": mn("blast"), "min_push": mn("min_push"), "min_ac": mn("min_ac"),
            "min_obs": mn("min_obs"), "max_v": max(s["v"] for s in st), "done_t": trace["done_t"]}
