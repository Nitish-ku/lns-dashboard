// ROAS webhook receiver: one stored file per event (form completed, call booked / rescheduled / cancelled / confirmed).
// Add in ROAS (both forms): https://<site>/api/roas?k=<ROAS_WEBHOOK_SECRET>
// Personal data is stripped BEFORE storing: no name, email, phone, company, IP, device; typed answers keep only their
// length bucket. What is
// kept is what the dashboard groups by: answers to choice questions, score, qualification, tier ending, UTM, times.
import { put } from "@vercel/blob";

const PII_TYPES = new Set(["name", "email", "phone", "address", "url", "number"]);
const TEXT_TYPES = new Set(["short_text", "long_text", "text", "textarea", "paragraph"]);

// typed answers: only their LENGTH is kept (SalesKick-style "0-10 characters" rows), never the words
export function lengthBucket(v) {
  const n = String(v ?? "").trim().length;
  return n <= 10 ? "0-10 characters" : n <= 20 ? "11-20 characters" : n <= 50 ? "21-50 characters" : "50+ characters";
}

function clean(answer) {
  if (!answer || PII_TYPES.has(answer.type)) return null;
  if (TEXT_TYPES.has(answer.type)) {
    return { elementId: answer.elementId, label: answer.label, type: answer.type, value: lengthBucket(answer.value), values: null, score: answer.score ?? null };
  }
  const { elementId, label, type, value, values, score } = answer;
  return { elementId, label, type, value: value ?? null, values: values ?? null, score: score ?? null };
}

export function sanitize(p) {
  const d = p.data || {};
  const s = d.submission || {};
  const a = d.appointment || {};
  const u = s.utmData || {};
  return {
    deliveryId: p.id,
    event: p.event,
    receivedAt: new Date().toISOString(),
    createdAtUtc: p.createdAtUtc,
    form: p.form ? { id: p.form.id, name: p.form.name } : null,
    lead: d.lead ? { id: d.lead.id, timezone: d.lead.timezone, country: d.lead.country, createdAtUtc: d.lead.createdAtUtc, tags: d.lead.tags || [] } : null,
    eventName: d.eventName ?? null,
    appointment: d.appointment ? {
      id: a.id, status: a.status, scheduledAtUtc: a.scheduledAtUtc, durationMinutes: a.durationMinutes, timezone: a.timezone,
      providerCalendarId: a.providerCalendarId, providerBookingId: a.providerBookingId, createdAtUtc: a.createdAtUtc,
    } : null,
    submission: d.submission ? {
      id: s.id, status: s.status, qualification: s.qualification, score: s.score,
      startedAtUtc: s.startedAtUtc, completedAtUtc: s.completedAtUtc,
      ending: s.ending ? { id: s.ending.id, name: s.ending.name, type: s.ending.type } : null,
      answers: (s.answers || []).map(clean).filter(Boolean),
      utm: { utm_source: u.utm_source ?? null, utm_medium: u.utm_medium ?? null, utm_campaign: u.utm_campaign ?? null,
             utm_content: u.utm_content ?? null, utm_term: u.utm_term ?? null, country: u.country ?? null },
    } : null,
    splitTestVariant: d.splitTestVariant ?? null,
  };
}

export default async function handler(req, res) {
  if (req.method !== "POST") return res.status(405).json({ ok: false });
  if (!process.env.ROAS_WEBHOOK_SECRET || req.query.k !== process.env.ROAS_WEBHOOK_SECRET) return res.status(401).json({ ok: false });
  const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
  if (!body.event) return res.status(400).json({ ok: false, error: "no event" });
  const rec = sanitize(body);
  const day = (rec.createdAtUtc || rec.receivedAt).slice(0, 10);
  const kind = [rec.event, rec.appointment?.status].filter(Boolean).join("-").replace(/[^a-z0-9_-]/gi, "");
  const id = String(rec.deliveryId || rec.submission?.id || Date.now()).replace(/[^a-z0-9_-]/gi, "");
  await put(`roas/${day}/${kind}-${id}.json`, JSON.stringify(rec), {
    access: "private", addRandomSuffix: true, contentType: "application/json",
  });
  return res.status(200).json({ ok: true });
}
