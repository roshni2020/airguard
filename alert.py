"""BLOCK alert: Twilio voice call (text-to-speech), Telegram fallback. Plain requests, no SDK.

Env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, ALERT_TO
     (fallback) TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Anything missing -> "alert skipped".
"""
import os
from xml.sax.saxutils import escape
import requests
import llm  # noqa: F401  (loads .env)
import sim


def nearest_gate(x, y):
    return min(sim.STAND_REFS, key=lambda r: sim.dist((x, y), sim.MAP["stands"][r]["nose"]))


def message(verdict):
    v = verdict["violations"][0]
    gate = v.get("stand") or nearest_gate(v["x"], v["y"])
    rule = v["rule"].replace("_", " ")
    t = verdict.get("triage") or {}
    sev = max(t.get("severity", []), key=lambda s: s["score"], default=None)
    prio = f"Severity {sev['label']}, assessed by SubQ Snap. " if sev else ""
    return (f"AirGuard alert. {prio}Update {verdict['candidate']} was blocked. "
            f"Rule violated: {rule}, citing {v['citation']}. "
            f"Location: near gate {gate} at SFO, {v['t']} seconds into scenario {v['scenario']}. "
            f"The update was not deployed; {verdict.get('deployed_after', 'the approved controller')} remains in control.")


def send(verdict):
    if verdict.get("verdict") != "BLOCK" or not verdict.get("violations"):
        return None
    text = message(verdict)
    e = os.environ
    try:
        if all(e.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "ALERT_TO")):
            sid = e["TWILIO_ACCOUNT_SID"]
            r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
                              auth=(sid, e["TWILIO_AUTH_TOKEN"]), timeout=20,
                              data={"To": e["ALERT_TO"], "From": e["TWILIO_FROM"],
                                    "Twiml": f"<Response><Say>{escape(text)}</Say></Response>"})
            print(f"alert: twilio call HTTP {r.status_code}")
            if r.ok:
                return {"channel": "twilio", "status": r.status_code}
        if e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID"):
            r = requests.post(f"https://api.telegram.org/bot{e['TELEGRAM_BOT_TOKEN']}/sendMessage",
                              json={"chat_id": e["TELEGRAM_CHAT_ID"], "text": text}, timeout=20)
            print(f"alert: telegram HTTP {r.status_code}")
            return {"channel": "telegram", "status": r.status_code}
    except requests.RequestException as ex:
        print(f"alert failed: {type(ex).__name__}")
        return {"channel": "error"}
    print("alert skipped")
    return None
