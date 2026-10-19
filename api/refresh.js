// Daily Vercel Cron (vercel.json, 11:00 UTC = 7am ET): triggers a fresh deployment, whose build reads GHL + Close
// again and writes new numbers. Only Vercel's cron can call it: it sends "Authorization: Bearer <CRON_SECRET>".
import { sameSecret } from "./roas.js";

export default async function handler(req, res) {
  const auth = req.headers.authorization || "";
  if (!auth.startsWith("Bearer ") || !sameSecret(auth.slice(7), process.env.CRON_SECRET)) {
    return res.status(401).json({ ok: false });
  }
  if (!process.env.DEPLOY_HOOK_URL) return res.status(500).json({ ok: false, error: "DEPLOY_HOOK_URL not set" });
  const r = await fetch(process.env.DEPLOY_HOOK_URL, { method: "POST" });
  return res.status(r.ok ? 200 : 502).json({ ok: r.ok });
}
