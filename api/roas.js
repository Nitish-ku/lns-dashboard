// ROAS webhook receiver: one stored file per event (form completed, call booked / rescheduled / cancelled / confirmed).
// Add in ROAS (both forms): https://<site>/api/roas?k=<ROAS_WEBHOOK_SECRET>
// Personal data is stripped BEFORE storing. ALLOWLIST (critic #4): only choice answers keep their value; typed answers
// keep only their length bucket; anything else (name, email, phone, date, url, file, unknown types) is dropped. Also
// kept: score, qualification, tier ending, UTM (capped), times. Never: name, email, phone, company, IP, device, tags.
import { put } from "@vercel/blob";
import { createHash, timingSafeEqual } from "node:crypto";

const CHOICE_TYPES = new Set(["single_choice", "multiple_choice", "multi_choice", "dropdown", "select", "yes_no", "boolean", "rating", "scale", "opinion_scale", "multi_select", "checkbox", "checkboxes", "radio", "picture_choice"]);
const TEXT_TYPES = new Set(["short_text", "long_text", "text", "textarea", "paragraph"]);
const LINKISH = /@|https?:\/\/|www\./i;                                   // email or link
const RANGE = /^\s*\$?[\d.,]+\s*[kKmM]?\s*[-\u2013to]+\s*\$?[\d.,]+\s*[kKmM+]?\s*$/;    // "10000 - 50000", "2024 - 2025"
// looks personal: an email/link, an "Other: ..." write-in, or a phone-like run of 9+ digits that isn't a number range
const looksPersonal = (s) => LINKISH.test(s) || /^\s*other\b/i.test(s) || ((s.match(/\d/g) || []).length >= 9 && !RANGE.test(s));

// typed answers: only their LENGTH is kept (SalesKick-style "0-10 characters" rows), never the words
export function lengthBucket(v) {
  const n = String(v ?? "").trim().length;
  return n <= 10 ? "0-10 characters" : n <= 20 ? "11-20 characters" : n <= 50 ? "21-50 characters" : "50+ characters";
}

// a choice value is kept only if it looks like a preset option (an "Other: call me at ..." write-in is bucketed)
function choiceValue(v) {
  if (v == null) return null;
  const s = String(v);
  return s.length > 80 || looksPersonal(s) ? lengthBucket(s) : s;
}

function clean(answer) {
  if (!answer || !answer.label) return null;
  const base = { elementId: answer.elementId, label: String(answer.label).slice(0, 300), type: answer.type, score: answer.score ?? null };
  if (CHOICE_TYPES.has(answer.type)) {
    const values = Array.isArray(answer.values) ? answer.values.map(choiceValue) : null;
    return { ...base, value: choiceValue(answer.value), values };
  }
  if (TEXT_TYPES.has(answer.type)) return { ...base, value: lengthBucket(answer.value), values: null };
  return null;  // name, email, phone, date, url, number, file, address and any type we don't know
}

const cap = (v, n = 120) => (v == null ? null : String(v).slice(0, n));

export function sanitize(p) {
  const d = p.data || {};
  const s = d.submission || {};
  const a = d.appointment || {};
  const u = s.utmData || {};
  return {
    deliveryId: cap(p.id, 100),
    event: cap(p.event, 60),
    receivedAt: new Date().toISOString(),
    createdAtUtc: cap(p.createdAtUtc, 40),
    form: p.form ? { id: cap(p.form.id, 80), name: cap(p.form.name) } : null,
    lead: d.lead ? { id: cap(d.lead.id, 80), timezone: cap(d.lead.timezone, 60), country: cap(d.lead.country, 8), createdAtUtc: cap(d.lead.createdAtUtc, 40) } : null,
    eventName: cap(d.eventName),
    appointment: d.appointment ? {
      id: cap(a.id, 80), status: cap(a.status, 40), scheduledAtUtc: cap(a.scheduledAtUtc, 40), durationMinutes: a.durationMinutes ?? null,
      timezone: cap(a.timezone, 60), providerCalendarId: cap(a.providerCalendarId, 80), providerBookingId: cap(a.providerBookingId, 80),
      createdAtUtc: cap(a.createdAtUtc, 40),
    } : null,
    submission: d.submission ? {
      id: cap(s.id, 80), status: cap(s.status, 40), qualification: cap(s.qualification, 40), score: s.score ?? null,
      startedAtUtc: cap(s.startedAtUtc, 40), completedAtUtc: cap(s.completedAtUtc, 40),
      ending: s.ending ? { id: cap(s.ending.id, 80), name: cap(s.ending.name), type: cap(s.ending.type, 40) } : null,
      answers: (s.answers || []).map(clean).filter(Boolean),
      utm: { utm_source: cap(u.utm_source), utm_medium: cap(u.utm_medium), utm_campaign: cap(u.utm_campaign),
             utm_content: cap(u.utm_content), utm_term: cap(u.utm_term), country: cap(u.country, 8) },
    } : null,
    splitTestVariant: cap(d.splitTestVariant),
  };
}

// constant-time secret check (critic #9): compare fixed-length digests
export function sameSecret(given, want) {
  if (!want || typeof given !== "string") return false;
  const h = (x) => createHash("sha256").update(x).digest();
  return timingSafeEqual(h(given), h(want));
}

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ ok: false });
  if (!sameSecret(req.query.k, process.env.ROAS_WEBHOOK_SECRET)) return res.status(401).json({ ok: false });
  let body;
  try {
    body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
  } catch {
    return res.status(400).json({ ok: false, error: "invalid json" });
  }
  if (!body || !body.event) return res.status(400).json({ ok: false, error: "no event" });
  const rec = sanitize(body);
  const day = String(rec.createdAtUtc || rec.receivedAt).slice(0, 10).replace(/[^0-9-]/g, "") || rec.receivedAt.slice(0, 10);
  const kind = [rec.event, rec.appointment?.status].filter(Boolean).join("-").replace(/[^a-z0-9_-]/gi, "");
  const id = String(rec.deliveryId || rec.submission?.id || Date.now()).replace(/[^a-z0-9_-]/gi, "");
  await put(`roas/${day}/${kind}-${id}.json`, JSON.stringify(rec), {
    access: "private", addRandomSuffix: true, contentType: "application/json",
  });
  return res.status(200).json({ ok: true });
}
