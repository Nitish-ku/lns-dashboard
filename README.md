# Show Rate Ledger

Applications, booked calls, confirmations and shows for AI Partner's AI Launch Call, broken down by form answer,
tier, booking timing, call time and ad. Password-protected (`DASHBOARD_PASSWORD`).

## How it works
- `api/roas.js`: ROAS webhook receiver. Strips personal data (name, email, phone, IP, device, free-text answers) and
  stores each event in a **private** Vercel Blob store.
- `scripts/fetch_roas.mjs` (build step 1): collects the stored events into a temp file outside the published folder.
- `scripts/build_data.py` (build step 2): reads GHL (bookings; tier = calendar) and Close (confirmation; Closer EOC
  outcome on the opportunity), joins the ROAS applications, writes anonymous records to `data/bookings.json`.
- `index.html`: the page, no framework. `middleware.js`: password gate (the two API routes stay reachable).
- `vercel.json`: build = the two scripts, publishes only `public/` (page + data). Daily cron 11:00 UTC calls
  `api/refresh.js`, which triggers a fresh deployment through a Deploy Hook.

## Counting rules (Nabeel, 2026-10-19)
- Black and white: a call **showed** only when its EOC says so; every other past call is a no-show, including calls
  the team cancelled. A call the lead cancelled through the ROAS link is left out. Calls still to come are left out.
- Show rate counts Tier A and B (Closer EOC). Tier C joins once Setter EOC data is connected.
- Baseline (Oct 11-18) uses the same rules; cancellations there all count as no-shows (who cancelled wasn't recorded).

## Deploy settings (Vercel)
Env vars: see `.env.example`. Storage: one Blob store (private) connected to the project.

## Run locally
```
cp .env.example .env   # fill in
npm install
node scripts/fetch_roas.mjs && python scripts/build_data.py
python -m http.server 8000   # http://localhost:8000 (no password locally)
```
