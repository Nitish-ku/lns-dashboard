"""Build data/bookings.json for the LNS show-rate dashboard.

One anonymous record per AI Launch Call booking made since the baseline (2026-10-11 13:56 UTC, the first personalised confirmation text): when it was
booked, the tier (= the GHL calendar ROAS booked into), and what happened. No names, emails or phone numbers leave
this script; the file is public.

Sources (read only):
  GHL    appointments on the AI Launch Call calendars -> booking time, tier, cancelled, call time
  Close  the lead (matched by email) -> confirmed, and the opportunity status the closers' EOC form sets -> showed

Definitions
  confirmed  Close "Confirmed Booking" = Yes, or "LNS Confirmation" = CONFIRMED, or the lead texted a clear yes
             between booking and the call (before LNS went live nothing recorded a texted yes)
  showed     an opportunity moved to a post-call status by the EOC form (follow-up, deposit, won, lost, disqualified)
  no_show    an opportunity in "No Show" (EOC: client ghosted)
  no_eoc     the call time has passed and the opportunity still says "Booked Meeting": the EOC was never filed

Env: CLOSE_API_KEY, GHL_API_KEY, GHL_LOCATION_ID (Vercel env vars, or a local .env, see .env.example).
Run: python scripts/build_data.py  (Vercel runs it as the build command on every deploy)
Records whose outcome is settled (cancelled, or showed / no-show) are reused from the previous file, so a run only
asks Close and GHL about bookings that can still change.
"""
import base64
import concurrent.futures as cf
import hashlib
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "bookings.json")
BASELINE = "2026-10-11T13:56:00Z"      # first booking that got the personalised confirmation message
LNS_LIVE = "2026-10-16T21:40:00Z"      # Anastasia LNS live for real leads (decision 74)
CALENDARS = {                          # GHL calendar id -> tier
    "LDm54ejvyPsAy4fZhCXz": "A",
    "siftFhrdGdv5kQ71jagu": "B",
    "jXdw3InfMs5xs1mV4dZv": "C",
    "trls1fLRxCD08igZxYYW": "Other",   # AI Launch Call (Aman)
    "saJ3vs2sJNnRdvzLEVPc": "Other",   # AI Launch Call (Nabeel)
    "9S4m6arZatkxuuHY4WRO": "Other",   # AI Launch Call with Nabeel
    "cpCBSC7dn0qhfvfFd7gQ": "Other",   # AI Launch Call with Aman
}
CONFIRMED_BOOKING = "cf_qm7w8fA9kw6mBm8TuyyGP9osqINC3XV7i5a9FMEyzAM"
LNS_CONFIRMATION = "cf_4zkkpwIWyyfZMUcOBOl7X7kjSNGj1l1MnPZjLaDmcxZ"
SHOWED = {"AP Follow-up", "DC Follow-up", "AI Partner Won", "DigitalCollege Won", "Deposit", "Lost", "Disqualified"}
YES = re.compile(r"^\s*(yes|yeah|yep|yup|ya|yea|sure|confirmed?|i'?ll be there|see you|sounds good|definitely|absolutely|si)\b", re.I)
NOT_YES = re.compile(r"reschedul|can'?t|cannot|cancel|not able|another time|different time|move it", re.I)


def _load_dotenv():
    """Local runs: read .env next to the repo root (Vercel passes real env vars, so there is none there)."""
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf8"):
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing env {name}")
    return v


def http(url, headers, tries=5):
    for attempt in range(tries):
        try:
            # GHL's edge rejects Python's default User-Agent with 403
            req = urllib.request.Request(url, headers={**headers, "User-Agent": "lns-dashboard/1.0 (+github actions)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 + attempt * 3)
                continue
            raise SystemExit(f"GET {url.split('?')[0]} -> {e.code}")
        except urllib.error.URLError:
            if attempt == tries - 1:
                raise
            time.sleep(3)


def ghl(path, params):
    return http("https://services.leadconnectorhq.com" + path + "?" + urllib.parse.urlencode(params),
                {"Authorization": "Bearer " + env("GHL_API_KEY"), "Version": "2021-04-15", "Accept": "application/json"})


def close(path, params):
    token = base64.b64encode((env("CLOSE_API_KEY") + ":").encode()).decode()
    return http("https://api.close.com/api/v1" + path + "?" + urllib.parse.urlencode(params),
                {"Authorization": "Basic " + token, "Accept": "application/json"})


def iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def outcome_of(lead_id, call_passed):
    statuses = [o["status_label"] for o in close("/opportunity/", {"lead_id": lead_id, "_fields": "status_label"})["data"]]
    if set(statuses) & SHOWED:
        return "showed"
    if "No Show" in statuses:
        return "no_show"
    return "no_eoc" if call_passed else "upcoming"


def texted_yes(lead_id, booked, call):
    sms = close("/activity/sms/", {"lead_id": lead_id, "_limit": 50, "date_created__gte": booked.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "_fields": "direction,text,date_created"})["data"]
    return any(s["direction"] == "inbound" and YES.search(s.get("text") or "") and not NOT_YES.search(s.get("text") or "")
               and iso(s["date_created"]) < call for s in sms)


FORWARD_FROM = "2026-10-19T04:00:00Z"   # Monday Oct 19, 00:00 ET: first week of the SalesKick-style tracking
ROAS_EVENTS = os.environ.get("ROAS_EVENTS_OUT") or os.path.join(__import__("tempfile").gettempdir(), "roas_events.json")


def load_roas_events():
    """Events saved by api/roas.js (already stripped of personal data), collected by scripts/fetch_roas.mjs."""
    if not os.path.exists(ROAS_EVENTS):
        return []
    seen, out = set(), []
    for ev in json.load(open(ROAS_EVENTS, encoding="utf8")):
        key = ev.get("deliveryId") or json.dumps(ev, sort_keys=True)
        if key not in seen:  # ROAS retries a delivery with the same id
            seen.add(key)
            out.append(ev)
    return out


def answer_value(a):
    v = a.get("values")
    return " + ".join(map(str, v)) if isinstance(v, list) and v else (str(a["value"]) if a.get("value") is not None else None)


def build_apps(events, by_ghl, ghl_status, now):
    """One anonymous record per completed ROAS application, joined to its booking (GHL) and outcome (Close).
    cancel: 'prospect' = ROAS sent a cancelled event (the lead used the cancel link); 'self' = cancelled in GHL with no
    ROAS cancel event (the team cancelled it; counted as a no-show, Nabeel 2026-10-19)."""
    apps = {}
    for ev in sorted(events, key=lambda e: e.get("createdAtUtc") or e.get("receivedAt") or ""):
        sub = ev.get("submission") or {}
        if not sub.get("id") or sub.get("status") not in (None, "complete"):
            continue
        a = apps.setdefault(sub["id"], {"sub": sub, "lead": ev.get("lead") or {}, "appts": {}, "prospect_cancel": set()})
        if sub.get("answers"):
            a["sub"] = sub
        ap = ev.get("appointment")
        if ap and ap.get("providerBookingId"):
            a["appts"][ap["providerBookingId"]] = ap  # latest event wins (rescheduled -> new time)
            if (ap.get("status") or "").lower() == "cancelled":
                a["prospect_cancel"].add(ap["providerBookingId"])
    out = []
    for sid, a in apps.items():
        sub = a["sub"]
        rec = {
            "id": hashlib.sha256(sid.encode()).hexdigest()[:12],
            "completed": sub.get("completedAtUtc") or sub.get("startedAtUtc"),
            "qualified": sub.get("qualification") == "qualified",
            "score": sub.get("score"),
            "ending": (sub.get("ending") or {}).get("name"),
            "answers": {x["label"]: answer_value(x) for x in sub.get("answers") or [] if answer_value(x) is not None},
            "utm": sub.get("utm") or {},
            "tz": a["lead"].get("timezone") or "America/New_York",
            "booking": None,
        }
        if a["appts"]:
            gid, ap = sorted(a["appts"].items(), key=lambda kv: kv[1].get("scheduledAtUtc") or "")[-1]
            b = by_ghl.get(gid) or {}
            cancelled = ghl_status.get(gid) in ("cancelled", "invalid") or (ap.get("status") or "").lower() == "cancelled"
            call = b.get("call") or ap.get("scheduledAtUtc")
            rec["booking"] = {
                "tier": b.get("tier") or CALENDARS.get(ap.get("providerCalendarId"), "Other"),
                "booked": b.get("booked") or ap.get("createdAtUtc"),
                "call": call,
                "cancel": ("prospect" if gid in a["prospect_cancel"] else "self") if cancelled else None,
                "confirmed": bool(b.get("confirmed")),
                "outcome": b.get("outcome") or ("upcoming" if call and iso(call) > now else "no_eoc"),
            }
        out.append(rec)
    return sorted(out, key=lambda r: r["completed"] or "")


def main():
    prev = {}
    if os.path.exists(OUT):
        prev = {r["id"]: r for r in json.load(open(OUT, encoding="utf8")).get("bookings", [])}
    now = dt.datetime.now(dt.timezone.utc)
    base = iso(BASELINE)
    lo, hi = int((base - dt.timedelta(days=2)).timestamp() * 1000), int((now + dt.timedelta(days=120)).timestamp() * 1000)
    loc = env("GHL_LOCATION_ID")
    events = []
    for cid, tier in CALENDARS.items():
        for e in ghl("/calendars/events", {"locationId": loc, "calendarId": cid, "startTime": lo, "endTime": hi}).get("events", []):
            if e.get("dateAdded") and iso(e["dateAdded"]) >= base:
                events.append((e, tier))
    def one(item):
        e, tier = item
        booked, call = iso(e["dateAdded"]), iso(e["startTime"])
        cancelled = (e.get("appointmentStatus") or "").lower() in ("cancelled", "invalid")
        rid = hashlib.sha256(e["id"].encode()).hexdigest()[:12]  # GHL ids stay private: the page is public
        old = prev.get(rid)
        if old and (old["outcome"] in ("showed", "no_show") or (cancelled and old["outcome"] == "cancelled")):
            return old, False, e["id"]
        rec = {"id": rid, "booked": booked.strftime("%Y-%m-%dT%H:%M:%SZ"), "call": call.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "tier": tier, "confirmed": False, "outcome": "cancelled" if cancelled else "unknown", "in_close": False}
        contact = ghl(f"/contacts/{e['contactId']}", {}).get("contact", {}) if e.get("contactId") else {}
        email = (contact.get("email") or "").strip().lower()
        lead = close("/lead/", {"query": f'email:"{email}"', "_limit": 1,
                                "_fields": f"id,custom.{CONFIRMED_BOOKING},custom.{LNS_CONFIRMATION}"})["data"] if email else []
        if lead:
            L = lead[0]
            rec["in_close"] = True
            rec["confirmed"] = (L.get(f"custom.{CONFIRMED_BOOKING}") == "Yes" or L.get(f"custom.{LNS_CONFIRMATION}") == "CONFIRMED"
                                or texted_yes(L["id"], booked, call))
            if not cancelled:
                rec["outcome"] = outcome_of(L["id"], call < now)
        elif not cancelled:
            rec["outcome"] = "not_in_close"
        return rec, True, e["id"]

    # 5 bookings at a time: fast enough for a Vercel build, gentle on Close's rate limit (429s are retried)
    with cf.ThreadPoolExecutor(5) as ex:
        results = list(ex.map(one, sorted(events, key=lambda x: x[0]["dateAdded"])))
    out = [r for r, _, _ in results]
    asked = sum(1 for _, a, _ in results if a)
    by_ghl = {gid: r for r, _, gid in results}
    ghl_status = {e["id"]: (e.get("appointmentStatus") or "").lower() for e, _ in events}
    apps = build_apps(load_roas_events(), by_ghl, ghl_status, now)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "baseline": BASELINE, "lns_live": LNS_LIVE,
               "forward_from": FORWARD_FROM, "bookings": out, "apps": apps}, open(OUT, "w", encoding="utf8"), indent=1)
    print(f"{len(out)} bookings ({asked} refreshed, {len(out) - asked} settled and reused), {len(apps)} ROAS applications -> {OUT}")


if __name__ == "__main__":
    main()
