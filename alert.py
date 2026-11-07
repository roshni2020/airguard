"""Phone alerts via Twilio REST (requests only, no SDK). Never logs or stores credentials.

    py alert.py --test        -> one SMS + one call saying "AirGuard test alert"

Env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, ALERT_TO (E.164). Missing -> skipped.
Alerts are never cached: a cached/offline gate run still sends a real alert.
"""
import os, sys
from xml.sax.saxutils import escape
import requests
import llm  # noqa: F401  (loads .env)
import sim

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{kind}.json"
VARS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "ALERT_TO")
SEVERITY_ORDER = {"jet_blast_zone": 0, "pushback_clearance": 1, "personnel_clearance": 2, "aircraft_clearance": 3,
                  "max_speed": 4, "low_visibility_speed": 5, "stress_noninferiority": 6, "mission_complete": 7}


def _configured():
    return all(os.environ.get(k) for k in VARS)


def _post(kind, data):
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    r = requests.post(API.format(sid=sid, kind=kind), data={"To": os.environ["ALERT_TO"],
                      "From": os.environ["TWILIO_FROM"], **data},
                      auth=(sid, os.environ["TWILIO_AUTH_TOKEN"]), timeout=10)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not r.ok:
        raise RuntimeError(f"twilio {kind} HTTP {r.status_code} code={j.get('code')}: {j.get('message', '')[:120]}")
    return j.get("sid"), j.get("status")


def send_sms(text):
    return _post("Messages", {"Body": text[:299]})


def place_call(text):
    say = escape(text)
    return _post("Calls", {"Twiml": f"<Response><Say voice='alice'>{say}</Say><Pause length='1'/>"
                                    f"<Say voice='alice'>{say}</Say></Response>"})


def _worst(verdict):
    vs = verdict.get("violations") or []
    sev = {(s["rule"], s["scenario"]): s["score"] for s in ((verdict.get("triage") or {}).get("severity") or [])}
    return min(vs, key=lambda v: (-sev.get((v["rule"], v.get("scenario")), 0), SEVERITY_ORDER.get(v["rule"], 9), v.get("t") or 0)) if vs else None


def message(verdict):
    name = os.path.splitext(os.path.basename(verdict["candidate"]))[0]
    back = os.path.splitext(os.path.basename(verdict.get("deployed_after") or "approved controller"))[0]
    if verdict["verdict"] == "PASS":
        return f"AirGuard approved {name}, deployed."
    v = _worst(verdict)
    gate = v.get("stand") or (min(sim.STAND_REFS, key=lambda r: sim.dist((v["x"], v["y"]), sim.MAP["stands"][r]["nose"]))
                              if v.get("x") is not None else "unknown")
    cite = v["citation"].split(";")[0]
    t = f" at t={round(v['t'])} seconds" if v.get("t") is not None else ""
    return (f"AirGuard blocked update {name}. Rule {v['rule']}, FAA {cite}: violation near stand {gate}{t} "
            f"in scenario {v.get('scenario')}. Rolled back to {back}. Fleet is safe.")


def alert(verdict, mode="both", notify_pass=False):
    """Returns {sms_sid, call_sid, status, error}. Never raises."""
    res = {"sms_sid": None, "call_sid": None, "status": None, "error": None, "mode": mode}
    if mode == "off" or (verdict.get("verdict") == "PASS" and not notify_pass):
        res["status"] = "off"
        return res
    if not _configured():
        print("alert skipped: twilio not configured")
        res["status"] = "skipped"
        return res
    if verdict.get("verdict") == "PASS":
        mode = "sms"
    elif mode == "both" and verdict.get("triage"):        # Snap only picks the channel; default stays both
        worst = max((s["score"] for s in verdict["triage"].get("severity", [])), default=None)
        if worst is not None and worst < 0.5 and verdict["triage"].get("page_ops_now", 1) < 0.5:
            mode = "sms"                                   # minor -> sms only
    text = message(verdict)
    res["mode"] = mode
    try:
        if mode in ("sms", "both"):
            res["sms_sid"], res["status"] = send_sms(text)
        if mode in ("call", "both"):
            res["call_sid"], res["status"] = place_call(text)
    except Exception as e:                                 # never crash the gate
        res["error"] = f"{type(e).__name__}: {e}"[:200]
    print(f"alert: sms={res['sms_sid']} call={res['call_sid']} status={res['status']}"
          + (f" error={res['error']}" if res["error"] else ""))
    return res


if __name__ == "__main__":
    if "--test" in sys.argv:
        if not _configured():
            sys.exit("alert skipped: twilio not configured")
        out = {}
        for name, fn in (("sms", send_sms), ("call", place_call)):
            try:
                out[name] = fn("AirGuard test alert")
            except Exception as e:
                out[name] = f"error: {e}"
        print(out)
