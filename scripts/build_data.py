"""Build data/bookings.json for the LNS show-rate dashboard.

One record per AI Launch Call booking made since the baseline (2026-10-11 13:56 UTC, the first personalised
confirmation text): when it was booked, the tier (= the GHL calendar ROAS booked into), and what happened, under a
salted id. No lead names, emails or phone numbers leave this script. The morning report (build_report) adds team
member names and Close lead links, so the file is protected by the site's password gate and never committed.

Sources (read only):
  GHL    appointments on the AI Launch Call calendars -> booking time, tier, cancelled, call time
  Close  the lead (matched by email) -> confirmed, and the opportunity status the closers' EOC form sets -> showed

Definitions
  confirmed  Close "Confirmed Booking" = Yes, or "LNS Confirmation" = CONFIRMED, or the lead texted a clear yes
             between booking and the call (before LNS went live nothing recorded a texted yes)
  showed     an opportunity moved to a post-call status by the EOC form (follow-up, deposit, won, lost, disqualified)
  no_show    an opportunity in "No Show" (EOC: client ghosted)
  no_eoc     the call time has passed and the opportunity still says "Booked Meeting": the EOC was never filed
  kind       Nabeel's booking types (voice note 2026-10-19): direct (ROAS booked A/B straight onto a closer calendar),
             setter_led (a setter turned a Tier C or unbooked lead into a closer call), recovery (a closer call booked
             again after the lead no-showed: not a fresh booking), follow_up (a later call after one that showed: not
             counted), triage (Tier C setter call, its own show rate). See classify().

Env: CLOSE_API_KEY, GHL_API_KEY, GHL_LOCATION_ID (Vercel env vars, or a local .env, see .env.example).
Run: python scripts/build_data.py  (Vercel runs it as the build command on every deploy)
Records whose outcome is settled (cancelled, or showed / no-show) are reused from the previous file, so a run only
asks Close and GHL about bookings that can still change.
"""
import base64
import collections
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
import zoneinfo

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
# the closers' own follow-up links (Nabeel 2026-10-19): every booking on these is a follow-up, never a fresh booking
FOLLOW_UP_CALENDARS = {"9S4m6arZatkxuuHY4WRO", "cpCBSC7dn0qhfvfFd7gQ"}   # ai-launch-call-with-nabeel / -with-aman
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
                wait = 2 + attempt * 3
                if e.code == 429 and "airtable.com" in url:
                    try:
                        wait = max(31, int(e.headers.get("Retry-After") or 0))  # Airtable locks the base for 30 s
                    except ValueError:  # an HTTP-date instead of seconds
                        wait = 31
                time.sleep(wait)
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


def outcome_of(lead_id, booked, call, now, next_booked=None):
    """The EOC outcome of THIS booking (critic #2): opportunities created for it (from an hour before it was booked
    until the same person's NEXT booking) decide first; only if there are none, an older opportunity updated after
    this call (and before the next booking) counts. An old 'Lost' / 'No Show' or a later booking's result is ignored.
    If several match, the most recently updated one decides."""
    end = next_booked or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    opps = close("/opportunity/", {"lead_id": lead_id, "_fields": "status_label,date_created,date_updated"})["data"]
    upd = lambda o: iso(o.get("date_updated") or o["date_created"])  # noqa: E731
    mine = [o for o in opps if booked - dt.timedelta(hours=1) <= iso(o["date_created"]) < end]
    if not mine:
        mine = [o for o in opps if iso(o["date_created"]) < booked and call - dt.timedelta(hours=1) <= upd(o) < end]
    for o in sorted(mine, key=lambda o: o.get("date_updated") or o["date_created"], reverse=True):
        if o["status_label"] in SHOWED:
            return "showed"
        if o["status_label"] == "No Show":
            return "no_show"
    return "no_eoc" if call < now else "upcoming"


def airtable(table, formula, fields):
    """All records of an Airtable table matching `formula` (read-only token AIRTABLE_TOKEN)."""
    token, out, offset = os.environ.get("AIRTABLE_TOKEN"), [], None
    while True:
        q = [("filterByFormula", formula), ("pageSize", "100")] + [("fields[]", f) for f in fields] + ([("offset", offset)] if offset else [])
        d = http(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{table}?" + urllib.parse.urlencode(q), {"Authorization": "Bearer " + token})
        out += d.get("records", [])
        offset = d.get("offset")
        if not offset:
            return out
        time.sleep(0.25)  # Airtable allows 5 requests/s per base, shared with Make


EXCLUDED = ("prospect", "rescheduled", "dq")   # cancel_by values that take a call OUT of the show rate
SETTER_OUTCOMES = {"no show": "no_show", "closer booking": "showed", "downsold": "showed", "sdr followup": "showed", "dq": "showed"}


def _one(v):
    """A lookup/linked field comes back as a list: use its first value."""
    return (v[0] if v else None) if isinstance(v, list) else v


def load_eocs():
    """EOC forms by Close lead id: [(filed_at, form, outcome, cancel_by)], and the setter forms that booked a closer call
    by lead id: [(filed_at, booking_type)] (booking_type "setter_led" | "recovery" | None when the setter left it blank).
    The forms' "call time" is really when the form was filled in (minutes to hours after the call).
    outcome: showed | no_show | excluded (lead cancelled, or rescheduled: the new booking takes over) | blank."""
    if not os.environ.get("AIRTABLE_TOKEN"):
        if os.environ.get("VERCEL"):
            sys.exit("AIRTABLE_TOKEN is not set: add the read-only Airtable token in Vercel")
        print("note: no AIRTABLE_TOKEN, EOC outcomes come from Close only")
        return {}, {}
    since = (iso(BASELINE) - dt.timedelta(days=37)).strftime("%Y-%m-%d")
    by_lead, setter_books, skipped, unknown = {}, {}, 0, collections.Counter()

    def add(f, form, when_field, out, by):
        nonlocal skipped
        try:
            lead, when = _one(f.get("Lead ID")), _one(f.get(when_field))
            if lead and when:
                by_lead.setdefault(str(lead).strip(), []).append((iso(str(when)), form, out, by))
        except Exception:  # noqa: BLE001  an odd value never stops the build; it is counted instead
            skipped += 1

    for r in airtable(CLOSER_EOC, f"IS_AFTER({{Date & Time of Call}}, '{since}')",
                      ["Lead ID", "Date & Time of Call", "Showed?", "Call Outcome", "No Show Reason?"]):
        f = r["fields"]
        showed, reason = _one(f.get("Showed?")), _one(f.get("No Show Reason?")) or ""
        if showed == "Yes":
            out, by = "showed", None
        elif showed == "No":  # Showed? decides when it's set; Call Outcome only when it's blank (critic #5)
            out, by = {"Client Cancelled Call": ("excluded", "prospect"), "Rescheduled": ("excluded", "rescheduled"),
                       "Closer Cancelled Call": ("no_show", "team")}.get(reason, ("no_show", None))
        elif showed:  # "Other (Lead got Triage Disqualified)": not a closer call that could show
            out, by = "excluded", "dq"
        else:
            out, by = ("showed", None) if _one(f.get("Call Outcome")) else ("blank", None)
        add(f, "closer", "Date & Time of Call", out, by)
    for r in airtable(SETTER_EOC, f"IS_AFTER({{Event Date & Time}}, '{since}')",
                      ["Lead ID", "Event Date & Time", "Call Outcome (Booking)", "Call Outcome (Dial)", "Booking Type"]):
        f = r["fields"]
        o = str(_one(f.get("Call Outcome (Booking)")) or "").strip().lower()
        bt = str(_one(f.get("Booking Type")) or "").strip().lower()
        bt = {"setter-led booking": "setter_led", "recovery booking": "recovery"}.get(bt, bt)
        if bt and bt not in ("setter_led", "recovery"):
            unknown["booking type: " + bt] += 1   # a new option is never silently a setter-led booking (critic round 4, #10)
            bt = ""
        if bt or "closer booking" in (o, str(_one(f.get("Call Outcome (Dial)")) or "").strip().lower()):
            try:
                lead, when = _one(f.get("Lead ID")), _one(f.get("Event Date & Time"))
                if lead and when:
                    setter_books.setdefault(str(lead).strip(), []).append(
                        (iso(str(when)), bt or None))
            except Exception:  # noqa: BLE001
                skipped += 1
        out = SETTER_OUTCOMES.get(o, "blank")  # explicit list: a new option is never silently a show (critic #6)
        if o and o not in SETTER_OUTCOMES:
            unknown[o] += 1
        add(f, "setter", "Event Date & Time", out, None)
    print(f"Airtable EOCs: {sum(map(len, by_lead.values()))} forms for {len(by_lead)} leads"
          + (f", {skipped} skipped (unreadable lead/date)" if skipped else "")
          + (f", WARNING unknown setter values ignored: {dict(unknown)}" if unknown else "")
          + f", {sum(map(len, setter_books.values()))} setter closer-bookings")
    return by_lead, setter_books


def eoc_for(eocs, lead_id, booked, call, next_call, tier, prev_call=None):
    """The EOC form for THIS call: the right form for the tier (Closer EOC for A/B, Setter triage EOC for C; a setter's
    own triage call with an A/B lead is not the closer call) and the same Close lead.
    Window: from 3h before the call (from the booking itself for a cancel / reschedule note, which can come in early,
    critic #4) to 3 days after, never at or after the person's next live call. Among matches a filled form beats a
    blank one, then the latest filed wins (a corrected resubmission replaces the first, critic #3)."""
    want = "setter" if tier == "C" else "closer"
    end = min(call + dt.timedelta(days=3), (next_call - dt.timedelta(hours=3)) if next_call else dt.datetime.max.replace(tzinfo=dt.timezone.utc))

    # an early cancel / reschedule note may come in from the booking on, but never while it could still belong to the
    # person's previous call (its 3-day window), so one note never excuses two bookings (critic round 3, b1)
    early = max(booked, prev_call + dt.timedelta(days=3)) if prev_call else booked

    def starts(x):
        return early if x[3] in EXCLUDED else call - dt.timedelta(hours=3)
    hits = [x for x in eocs.get(lead_id, []) if x[1] == want and starts(x) <= x[0] < end]
    return max(hits, key=lambda x: (x[2] != "blank", x[0])) if hits else None


CLOSER_TIERS = ("A", "B", "Other")   # closer calendars; C is the setters' triage calendar


def by_person(x):
    """Booked by someone using the booking link, not by ROAS. A booking GHL doesn't say anything about counts as ROAS."""
    return bool(x["source"]) and x["source"] != "third_party"


def prior_status(p, forms):
    """What became of an earlier closer booking: showed | excused (lead cancelled / rescheduled / DQ) | failed (no-show,
    missing EOC, team cancel: Nabeel counts those as no-shows) | unknown. A booking from before the tracked period has
    no record, so only its own Closer EOC can tell; without one it is unknown and never makes a later call a recovery
    (critic round 4, #1)."""
    r = p["rec"]
    if r is not None:
        if r.get("outcome") == "showed":
            return "showed"
        if r.get("cancel_by") in EXCLUDED:
            return "excused"
        return "unknown" if r.get("outcome") in ("upcoming", "not_in_close") else "failed"
    mine = [f for f in forms if p["call"] - dt.timedelta(hours=3) <= f[0] < p["call"] + dt.timedelta(days=3)]
    if not mine:
        return "unknown"
    o = max(mine, key=lambda f: (f[1] != "blank", f[0]))[1]
    return {"showed": "showed", "excluded": "excused", "no_show": "failed"}.get(o, "unknown")


def classify(x, history, forms, tied, memo):
    """Nabeel's booking type for one booking `x` (dict: id, tier, booked, call, source, cancelled, rec), from the same
    person's other GHL bookings (`history`, oldest first, incl. ones from before the tracked period with rec None), their
    Closer EOCs (`forms`: [(filed, outcome, cancel_by)]) and the setter forms tied to each booking (`tied`: each form
    belongs to ONE booking, the first closer booking made from a day before it to 14 days after). Tier C is triage, the follow-up calendars are follow_up. Else:
      1. the setter's own Booking Type on the form tied to this booking (the latest filled one)
      2. follow_up   the person already had a closer call that showed before this was booked
      3. recovery    an earlier closer call of theirs, due before this was booked, didn't happen
      4. a closer booking cancelled before it was due and replaced by this one keeps its type (lead cancel /
         reschedule link, ROAS rebooking, or a history we can't vouch for); only the team cancelling and a person
         booking it again is a recovery (the cancelled one already counts as a no-show)
      5. setter_led  a person (not ROAS) booked it, the lead had a (not cancelled) Tier C triage call first, or a setter
                     form saying "Closer Booking" is tied to it
      6. direct      everything else: ROAS booked the lead straight onto a closer calendar"""
    if x["id"] in memo:
        return memo[x["id"]]
    mine = sorted(tied.get(x["id"], []), key=lambda f: f[0])
    typed = [bt for _, bt in mine if bt]
    closer = [p for p in history if p["id"] != x["id"] and p["tier"] in CLOSER_TIERS]
    due = [p for p in closer if p["call"] < x["booked"]]
    # cancelled before it was due (in GHL, or through the ROAS link while GHL still says booked: critic round 5, #3)
    moved = [p for p in closer if (p["cancelled"] or (p["rec"] or {}).get("outcome") == "cancelled")
             and p["booked"] < x["booked"] <= p["call"]]
    if x["tier"] == "C":
        kind = "triage"
    elif x.get("cal") in FOLLOW_UP_CALENDARS:
        kind = "follow_up"
    elif typed:
        kind = typed[-1]
    elif any(t < x["booked"] and o == "showed" for t, o, _ in forms) or any(prior_status(p, forms) == "showed" for p in due):
        kind = "follow_up"
    elif any(prior_status(p, forms) == "failed" for p in due):
        kind = "recovery"
    elif moved:
        m = moved[-1]
        if prior_status(m, forms) == "failed" and by_person(x):
            kind = "recovery"   # the team cancelled it (a no-show) and a person booked it again
        else:   # lead cancel / reschedule, a ROAS rebook, or history nobody can vouch for (critic round 5, #1): keep its type
            # recursion only goes to an earlier booking (m was booked before x), so it ends
            kind = (m["rec"] or {}).get("kind") or classify(m, history, forms, tied, memo)
    elif by_person(x) or mine or any(p["tier"] == "C" and not p["cancelled"] and p["call"] < x["booked"] for p in history):
        kind = "setter_led"
    else:
        kind = "direct"
    memo[x["id"]] = kind
    return kind


def is_cancel(status, event=None):
    """ROAS / GHL spell it both ways (critic #7)."""
    return (status or "").lower() in ("cancelled", "canceled", "invalid") or "cancel" in (event or "").lower()


def safe_tz(tz):
    """A non-IANA timezone would crash the page's date code (critic #15)."""
    try:
        zoneinfo.ZoneInfo(tz)
        return tz
    except Exception:  # noqa: BLE001
        return "America/New_York"


def rid_of(raw):
    """Anonymous record id, salted with ID_SALT (critic #8): without it nobody can link a record to a GHL/ROAS id.
    Required on Vercel; a separate secret so rotating the webhook secret doesn't change every id."""
    salt = os.environ.get("ID_SALT")
    if not salt and os.environ.get("VERCEL"):
        sys.exit("ID_SALT is not set: add it in Vercel (any long random string, never change it)")
    return hashlib.sha256(((salt or "local") + "|" + raw).encode()).hexdigest()[:12]


def texted_yes(lead_id, booked, call):
    sms = close("/activity/sms/", {"lead_id": lead_id, "_limit": 50, "date_created__gte": booked.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "_fields": "direction,text,date_created"})["data"]
    return any(s["direction"] == "inbound" and YES.search(s.get("text") or "") and not NOT_YES.search(s.get("text") or "")
               and iso(s["date_created"]) < call for s in sms)


AIRTABLE_BASE = "appCWa6TV6p7EBarC"          # AI Partners - Sales
CLOSER_EOC = "tblYsh3fxTpXuPdIW"             # "Full Closer Report Form" (Tier A/B)
SETTER_EOC = "tblaoMsiE3FSkHjQt"             # "Triage Calls EOC Form" (Tier C, setters)
FORWARD_FROM = "2026-10-19T04:00:00Z"   # Monday Oct 19, 00:00 ET: first week of the SalesKick-style tracking
ET = zoneinfo.ZoneInfo("America/New_York")
REPORT_CALENDARS = (("LDm54ejvyPsAy4fZhCXz", "A"), ("siftFhrdGdv5kQ71jagu", "B"), ("jXdw3InfMs5xs1mV4dZv", "C"))
FULL, NEARLY_FULL = 0, 2   # open slots tomorrow: red "hand to setter" at 0, amber at 2 or fewer (a guess: Nabeel can set)


def build_report(events, by_ghl, lead_of, now):
    """The morning sales report (Nabeel 2026-10-19), as of this build (early morning ET, daily):
    unconfirmed  today's calls (ET) nobody has confirmed yet
    capacity     open slots / calls still to come per calendar for today and the next 2 days (GHL free-slots: calendar
                 blocks and double-booking are already in it). A closer calendar is "full" (red, hand to setter) with
                 no open slot tomorrow, "nearly_full" (amber) at 2 or fewer, "closed" when tomorrow has neither open
                 slots nor calls (no working hours: not a capacity problem). When full, the hand-over candidates are
                 the LATER-booked call(s) in each closer's slot holding more than one call (the double-booking itself).
    missing_eoc  yesterday's calls (ET) with no EOC, or a blank one: closer for A/B, setter for C (follow-up calendars
                 left out; a lead not in Close can't be checked; calls the team cancelled in GHL are left out)
    People are team members (GHL users); a lead is only a Close link, never a name. Close lead ids make these rows
    identifiable: the protection is the site's password gate (the file is never committed)."""
    loc = env("GHL_LOCATION_ID")
    try:
        names = {u["id"]: (u.get("name") or f'{u.get("firstName", "")} {u.get("lastName", "")}').strip()
                 for u in ghl("/users/", {"locationId": loc}).get("users", [])}
    except (Exception, SystemExit) as ex:  # http() exits on a GHL error; the report never stops the build
        print("WARNING report: GHL users not readable:", type(ex).__name__)
        names = None
    try:
        per_slot = {c["id"]: c.get("appoinmentPerSlot") or c.get("appointmentPerSlot") or 1
                    for c in ghl("/calendars/", {"locationId": loc}).get("calendars", [])}
    except (Exception, SystemExit) as ex:
        print("WARNING report: GHL calendars not readable:", type(ex).__name__)
        per_slot = {}
    today = now.astimezone(ET).date()
    days = [today + dt.timedelta(days=i) for i in range(3)]
    yesterday = today - dt.timedelta(days=1)
    et_day = lambda e: iso(e["startTime"]).astimezone(ET).date()  # noqa: E731
    who = lambda e: ("Unknown" if names is None else names.get(e.get("assignedUserId")) or "Unassigned")  # noqa: E731

    def row(e, tier):
        r, lid = by_ghl.get(e["id"]) or {}, lead_of.get(e["id"])
        return {"call": iso(e["startTime"]).strftime("%Y-%m-%dT%H:%M:%SZ"), "tier": tier, "kind": r.get("kind"),
                "who": who(e), "confirmed": bool(r.get("confirmed")),
                "close": f"https://app.close.com/lead/{lid}/" if lid else None}

    # live = not cancelled in GHL AND not cancelled by the lead through ROAS (GHL can still say booked: critic r6 #2)
    live = [(e, t) for e, t in events if e.get("startTime") and not is_cancel(e.get("appointmentStatus"))
            and (by_ghl.get(e["id"]) or {}).get("outcome") != "cancelled"]
    unconfirmed = [row(e, t) for e, t in live if et_day(e) == today and not (by_ghl.get(e["id"]) or {}).get("confirmed")]

    missing = []
    for e, t in live:
        r = by_ghl.get(e["id"]) or {}
        if et_day(e) != yesterday or r.get("cancel_by") in EXCLUDED or e.get("calendarId") in FOLLOW_UP_CALENDARS:
            continue
        if r.get("outcome") == "not_in_close":   # the forms are keyed by Close lead id: nothing to match against
            missing.append(dict(row(e, t), form="Setter EOC" if t == "C" else "Closer EOC", state="not in Close"))
        elif not r.get("eoc") or r.get("eoc_blank"):   # "close" = the form reached Close's opportunity: it was filed
            missing.append(dict(row(e, t), form="Setter EOC" if t == "C" else "Closer EOC",
                                state="blank" if r.get("eoc_blank") else "not filed"))

    capacity = []
    for cid, tier in REPORT_CALENDARS:
        mine = [(e, t) for e, t in live if e.get("calendarId") == cid]
        cap = {"tier": tier, "who": sorted({who(e) for e, _ in mine}), "per_slot": per_slot.get(cid, 1),
               "days": [], "error": None, "status": None, "hand_over": []}
        try:   # fetch AND parse inside: one odd answer marks this calendar unreadable, never the build (critic r6 #1)
            fs = ghl(f"/calendars/{cid}/free-slots", {"startDate": int(dt.datetime.combine(today, dt.time(), ET).timestamp() * 1000),
                                                      "endDate": int(dt.datetime.combine(days[-1], dt.time(23, 59), ET).timestamp() * 1000),
                                                      "timezone": "America/New_York"})
            if not isinstance(fs, dict):
                raise ValueError("free-slots answer is not an object")   # never read an odd answer as "full"
            if not any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k)) for k in fs) and any(et_day(e) in days for e, _ in mine):
                raise ValueError("free-slots answer has no days")       # calls ahead but no day at all: unknown shape
            for d in days:
                v = fs.get(d.isoformat())
                slots = [s for s in ((v or {}).get("slots") or []) if iso(str(s)) > now]   # still bookable
                ahead = [e for e, _ in mine if et_day(e) == d and iso(e["startTime"]) > now]   # calls still to come
                cap["days"].append({"date": d.isoformat(), "open": len(slots), "booked": len(ahead)})
        except (Exception, SystemExit) as ex:
            cap["error"], cap["days"] = type(ex).__name__, [{"date": d.isoformat(), "open": None, "booked": None} for d in days]
            print(f"WARNING report: free slots for Tier {tier} not readable:", cap["error"])
        if not cap["error"] and tier != "C":
            tm = cap["days"][1]
            cap["status"] = ("closed" if tm["open"] == 0 and tm["booked"] == 0 else "full" if tm["open"] <= FULL
                             else "nearly_full" if tm["open"] <= NEARLY_FULL else "ok")
        if cap["status"] == "full":
            by_slot = collections.defaultdict(list)
            for e, t in mine:
                if et_day(e) == days[1]:
                    by_slot[(e.get("assignedUserId") or "", iso(e["startTime"]))].append((e, t))
            for _, calls in sorted(by_slot.items()):
                calls.sort(key=lambda x: x[0].get("dateAdded") or "")
                cap["hand_over"] += [row(e, t) for e, t in calls[1:]]   # the first booking keeps the closer
        capacity.append(cap)
    print(f"report: {len(unconfirmed)} unconfirmed today, {len(missing)} missing EOCs yesterday, capacity "
          + ", ".join(f'{c["tier"]} {c["status"] or c["error"] or "-"}' for c in capacity))
    return {"as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "today": today.isoformat(), "yesterday": yesterday.isoformat(),
            "thresholds": {"full": FULL, "nearly_full": NEARLY_FULL},
            "unconfirmed": sorted(unconfirmed, key=lambda r: r["call"]), "missing_eoc": sorted(missing, key=lambda r: r["call"]),
            "capacity": capacity}


ROAS_EVENTS = os.environ.get("ROAS_EVENTS_OUT") or os.path.join(__import__("tempfile").gettempdir(), "roas_events.json")


def load_roas_events():
    """Events saved by api/roas.js (already stripped of personal data), collected by scripts/fetch_roas.mjs."""
    if not os.path.exists(ROAS_EVENTS):
        return []
    seen, out = set(), []
    for ev in json.load(open(ROAS_EVENTS, encoding="utf8")):
        key = ev.get("deliveryId") or json.dumps(ev, sort_keys=True)
        # ROAS "send test" replays a canned sample (fitness questions, dated July 2026). Only a FORM-COMPLETION event can
        # be judged by age: booking events (cancel / reschedule / confirm) legitimately arrive days after the application
        # (critic #1). Test booking events are dropped later because their application predates FORWARD_FROM.
        done, got = (ev.get("submission") or {}).get("completedAtUtc"), ev.get("receivedAt")
        if ev.get("event") == "completed_lead" and done and got and iso(got) - iso(done) > dt.timedelta(days=2):
            continue
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
        a = apps.setdefault(sub["id"], {"sub": sub, "lead": ev.get("lead") or {}, "appts": {}, "seen": {}, "prospect_cancel": set()})
        if sub.get("answers"):
            a["sub"] = sub
        ap = ev.get("appointment")
        if ap and ap.get("providerBookingId"):
            gid = ap["providerBookingId"]
            a["appts"][gid] = ap  # latest event wins (rescheduled -> new time)
            a["seen"][gid] = ev.get("createdAtUtc") or ev.get("receivedAt") or ""
            if is_cancel(ap.get("status"), ev.get("event")):
                a["prospect_cancel"].add(gid)
    out, misses = [], 0
    for sid, a in apps.items():
        sub = a["sub"]
        done = sub.get("completedAtUtc") or sub.get("startedAtUtc")
        if not done or iso(done) < iso(FORWARD_FROM):
            continue  # before tracking started, or a ROAS test sample (dated July 2026)
        rec = {
            "id": rid_of(sid),
            "completed": sub.get("completedAtUtc") or sub.get("startedAtUtc"),
            "qualified": sub.get("qualification") == "qualified",
            "score": sub.get("score"),
            "ending": (sub.get("ending") or {}).get("name"),
            "answers": {x["label"]: answer_value(x) for x in sub.get("answers") or [] if answer_value(x) is not None},
            "utm": sub.get("utm") or {},
            "tz": safe_tz(a["lead"].get("timezone") or "America/New_York"),
            "booking": None,
        }
        if a["appts"]:
            # the live booking (critic #3): one nobody cancelled, most recent event first; a cancelled one only if all are
            def lead_cancelled(g):   # the booking record decides when there is one (ROAS cancel before the call, no EOC)
                r = by_ghl.get(g)
                return r.get("cancel_by") == "prospect" if r else g in a["prospect_cancel"]

            def dead(g):
                if (by_ghl.get(g) or {}).get("outcome") == "showed":
                    return False  # the EOC says the call happened, whatever GHL says
                return lead_cancelled(g) or is_cancel(ghl_status.get(g)) or (by_ghl.get(g) or {}).get("outcome") == "cancelled"
            gid = sorted(a["appts"], key=lambda g: (not dead(g), a["seen"].get(g, "")))[-1]
            ap = a["appts"][gid]
            b = by_ghl.get(gid) or {}
            if not b:
                misses += 1
            cancelled = dead(gid)
            call = b.get("call") or ap.get("scheduledAtUtc")
            rec["booking"] = {
                "tier": b.get("tier") or CALENDARS.get(ap.get("providerCalendarId"), "Other"),
                "booked": b.get("booked") or ap.get("createdAtUtc"),
                "call": call,
                # prospect = the lead cancelled (ROAS link or the closer's EOC); excluded = rescheduled / triage-DQ per
                # the EOC; self = the team cancelled (a no-show). A form saying the call happened clears any cancel.
                "cancel": (None if b.get("outcome") == "showed"
                           else "prospect" if lead_cancelled(gid)
                           else "excluded" if b.get("cancel_by") in EXCLUDED
                           else "self" if cancelled or b.get("outcome") == "cancelled" else None),
                "eoc": b.get("eoc"),
                "eoc_blank": bool(b.get("eoc_blank")),   # a blank form isn't a filed EOC (section 05)
                "confirmed": bool(b.get("confirmed")),
                "outcome": b.get("outcome") or ("upcoming" if call and iso(call) > now else "no_eoc"),
            }
        out.append(rec)
    if misses:  # a booking we can't find in GHL silently becomes a no-show: make it visible in the build log (critic #6)
        print(f"WARNING: {misses} application booking(s) not found among GHL calendar events (counted without Close data)")
    return sorted(out, key=lambda r: r["completed"] or "")


def main():
    prev = {}
    if os.path.exists(OUT):
        prev = {r["id"]: r for r in json.load(open(OUT, encoding="utf8")).get("bookings", [])}
    now = dt.datetime.now(dt.timezone.utc)
    base = iso(BASELINE)
    # a month before the baseline too: older bookings are never counted, only read as a person's history (booking type)
    lo, hi = int((base - dt.timedelta(days=35)).timestamp() * 1000), int((now + dt.timedelta(days=120)).timestamp() * 1000)
    loc = env("GHL_LOCATION_ID")
    events, older = [], []
    for cid, tier in CALENDARS.items():
        for e in ghl("/calendars/events", {"locationId": loc, "calendarId": cid, "startTime": lo, "endTime": hi}).get("events", []):
            if e.get("dateAdded"):
                (events if iso(e["dateAdded"]) >= base else older).append((e, tier))
    # per person: their latest LIVE booking (confirmation fields describe it) and, for every booking, when that
    # person booked next (bounds which Close opportunities belong to it)
    latest_by_contact, next_booked, last_seen = {}, {}, {}
    live_calls = collections.defaultdict(list)   # contact -> start times of their live (not cancelled) bookings
    for e, _ in events:
        if not is_cancel(e.get("appointmentStatus")):
            live_calls[e.get("contactId")].append(iso(e["startTime"]))
    # next live call AFTER this call (critic #7), not the next booking added; previous call (any booking) BEFORE it
    next_call = {e["id"]: min((t for t in live_calls[e.get("contactId")] if t > iso(e["startTime"])), default=None) for e, _ in events}
    all_calls = collections.defaultdict(list)
    for e, _ in events:
        all_calls[e.get("contactId")].append(iso(e["startTime"]))
    prev_call = {e["id"]: max((t for t in all_calls[e.get("contactId")] if t < iso(e["startTime"])), default=None) for e, _ in events}
    for e, _ in sorted(events, key=lambda x: x[0]["dateAdded"]):
        c = e.get("contactId")
        if c in last_seen:
            next_booked[last_seen[c]] = iso(e["dateAdded"])
        last_seen[c] = e["id"]
        if not is_cancel(e.get("appointmentStatus")):
            latest_by_contact[c] = e["id"]

    eocs, setter_books = load_eocs()

    def one(item):
        e, tier = item
        booked, call = iso(e["dateAdded"]), iso(e["startTime"])
        cancelled = is_cancel(e.get("appointmentStatus"))
        rid = rid_of(e["id"])  # GHL ids stay private
        old = prev.get(rid)
        settled = old and not old.get("eoc_blank") and (old["outcome"] in ("showed", "no_show") or (cancelled and old["outcome"] == "cancelled"))
        if settled and old.get("kind"):
            return old, False, e["id"], None
        rec = {"id": rid, "booked": booked.strftime("%Y-%m-%dT%H:%M:%SZ"), "call": call.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "tier": tier, "confirmed": False, "outcome": "cancelled" if cancelled else "unknown", "in_close": False,
               "eoc": None, "cancel_by": None}
        contact = ghl(f"/contacts/{e['contactId']}", {}).get("contact", {}) if e.get("contactId") else {}
        email = (contact.get("email") or "").strip().lower()
        lead = close("/lead/", {"query": f'email:"{email}"', "_limit": 1,
                                "_fields": f"id,custom.{CONFIRMED_BOOKING},custom.{LNS_CONFIRMATION}"})["data"] if email else []
        if settled:   # its outcome stays as it was; the lead id is only needed for the booking type
            return old, False, e["id"], (lead[0]["id"] if lead else None)
        if lead:
            L = lead[0]
            rec["in_close"] = True
            # the lead's confirmation fields describe its LATEST booking only (critic #2): an older booking of the same
            # person counts as confirmed only by a texted yes inside its own booking-to-call window
            latest = latest_by_contact.get(e.get("contactId")) == e["id"]
            rec["confirmed"] = bool((latest and (L.get(f"custom.{CONFIRMED_BOOKING}") == "Yes"
                                                 or L.get(f"custom.{LNS_CONFIRMATION}") == "CONFIRMED"))
                                    or texted_yes(L["id"], booked, call))
            form = eoc_for(eocs, L["id"], booked, call, next_call.get(e["id"]), tier, prev_call.get(e["id"])) if call < now else None
            if form:  # the EOC form itself is the truth (Airtable): Closer EOC for A/B, Setter (triage) EOC for C
                _, rec["eoc"], out, by = form
                rec["eoc_blank"] = out == "blank"   # filed but "Showed?" left empty: incomplete (morning report)
                if out == "showed":   # the form says the call happened: that wins, even over a GHL cancel (critic #2)
                    rec["outcome"], rec["cancel_by"] = "showed", None
                elif out == "excluded":
                    rec["outcome"], rec["cancel_by"] = "cancelled", by
                elif cancelled:
                    rec["cancel_by"] = "team"  # cancelled in GHL and the EOC doesn't say the lead did it
                else:
                    rec["outcome"] = "no_show"   # a no-show form, or a blank one (Nabeel: showed only if the EOC says so)
                    rec["cancel_by"] = by
            elif cancelled:
                rec["cancel_by"] = "team"
            elif tier == "C":  # setters file only the triage EOC; Close's opportunity reflects closers (critic #11)
                rec["outcome"] = "no_eoc" if call < now else "upcoming"
            else:
                rec["outcome"] = outcome_of(L["id"], booked, call, now, next_booked.get(e["id"]))
                if rec["outcome"] in ("showed", "no_show"):
                    rec["eoc"] = "close"   # the Closer EOC reached Close's opportunity but not (yet) Airtable
        elif not cancelled:
            rec["outcome"] = "not_in_close"
        return rec, True, e["id"], (lead[0]["id"] if lead else None)

    # 5 bookings at a time: fast enough for a Vercel build, gentle on Close's rate limit (429s are retried)
    with cf.ThreadPoolExecutor(5) as ex:
        results = list(ex.map(one, sorted(events, key=lambda x: x[0]["dateAdded"])))
    out = [r for r, _, _, _ in results]
    asked = sum(1 for _, a, _, _ in results if a)
    by_ghl = {gid: r for r, _, gid, _ in results}
    lead_of = {gid: lid for _, _, gid, lid in results}
    ghl_status = {e["id"]: (e.get("appointmentStatus") or "").lower() for e, _ in events}
    roas = load_roas_events()
    # a cancel through the ROAS link is the lead's own: left out of the show rate on the booking records too
    for ev in roas:
        ap = ev.get("appointment") or {}
        r, when = by_ghl.get(ap.get("providerBookingId")), ev.get("createdAtUtc") or ev.get("receivedAt")
        if (r and is_cancel(ap.get("status"), ev.get("event")) and r["outcome"] != "showed" and not r.get("eoc")
                and when and iso(when) < iso(r["call"]) and iso(r["call"]) >= iso(FORWARD_FROM)):
            r["outcome"], r["cancel_by"] = "cancelled", "prospect"

    # booking type per person, oldest first (a settled record keeps the type it was given)
    per_contact = collections.defaultdict(list)
    for e, tier in sorted(older + events, key=lambda x: x[0]["dateAdded"]):
        per_contact[e.get("contactId")].append({
            "id": e["id"], "tier": tier, "cal": e.get("calendarId"), "booked": iso(e["dateAdded"]), "call": iso(e["startTime"]),
            "source": (e.get("createdBy") or {}).get("source"), "cancelled": is_cancel(e.get("appointmentStatus")),
            "rec": by_ghl.get(e["id"])})
    memo, no_lead = {}, 0
    for c, hist in per_contact.items():
        if c is None:   # no contact: can't be linked to anyone's history (critic round 4, #8)
            for h in hist:
                if h["rec"] is not None and not h["rec"].get("kind"):
                    h["rec"]["kind"] = "triage" if h["tier"] == "C" else "direct"
            continue
        lid = next((lead_of[h["id"]] for h in hist if lead_of.get(h["id"])), None)
        forms = [(t, o, by) for t, form, o, by in eocs.get(lid, []) if form == "closer"] if lid else []
        tied = collections.defaultdict(list)
        closer_bk = [h for h in hist if h["tier"] in CLOSER_TIERS and h["cal"] not in FOLLOW_UP_CALENDARS]
        for t, bt in sorted(setter_books.get(lid, []) if lid else []):
            near = lambda lag: next((h for h in closer_bk if t - lag <= h["booked"] <= t + dt.timedelta(days=14)), None)  # noqa: E731
            target = near(dt.timedelta(hours=1)) or near(dt.timedelta(days=1))
            if target:
                tied[target["id"]].append((t, bt))
        for h in hist:
            if h["rec"] is not None and not h["rec"].get("kind"):
                no_lead += not lid
                h["rec"]["kind"] = classify(h, hist, forms, tied, memo)
    print("booking types:", str(dict(collections.Counter(r.get("kind") for r in out)))
          + (f" ({no_lead} typed without a Close lead: GHL history only)" if no_lead else ""))
    apps = build_apps(roas, by_ghl, ghl_status, now)
    try:   # the report never stops the daily build (critic r6 #1): the page hides the section without it
        report = build_report(events, by_ghl, lead_of, now)
    except Exception as ex:  # noqa: BLE001
        print("WARNING report failed:", type(ex).__name__, str(ex)[:200])
        report = None
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "baseline": BASELINE, "lns_live": LNS_LIVE,
               "forward_from": FORWARD_FROM, "bookings": out, "apps": apps, "report": report}, open(OUT, "w", encoding="utf8"), indent=1)
    print(f"{len(out)} bookings ({asked} refreshed, {len(out) - asked} settled and reused), {len(apps)} ROAS applications -> {OUT}")


if __name__ == "__main__":
    main()
