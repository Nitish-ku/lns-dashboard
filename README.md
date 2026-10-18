# Show Rate Ledger

AI Launch Call bookings since the first personalised confirmation text (Oct 11 2026): bookings, confirmations
and shows by tier (A / B / C), week by week.

- `index.html`: the page (no framework, no chart library). Reads `data/bookings.json`.
- `scripts/build_data.py`: reads GHL (appointments; tier = calendar) and Close (confirmation; EOC outcome on the
  opportunity) and writes one anonymous record per booking to `data/bookings.json`. Read only. No names, emails or
  phone numbers. The data file is generated at build time and never committed (this repo is public).
- `vercel.json`: build command = the script; a daily cron (11:00 UTC, 7am ET) calls `api/refresh.js`, which triggers
  a fresh deployment through a Deploy Hook, so the numbers rebuild every morning.

## Deploy on Vercel (once)
1. Import the repo. Framework preset: **Other** (vercel.json sets the build).
2. Settings → Environment Variables (Production): `CLOSE_API_KEY`, `GHL_API_KEY`, `GHL_LOCATION_ID`, `CRON_SECRET`
   (any long random string). See `.env.example`.
3. Deploy.
4. Settings → Git → Deploy Hooks → create one (branch `main`), copy its URL into a new env var `DEPLOY_HOOK_URL`,
   then redeploy once so the cron can use it.

## Run locally
```
cp .env.example .env   # fill in the values
python scripts/build_data.py
python -m http.server 8000   # open http://localhost:8000
```

## Definitions
See section 06 on the page. Confirmed = Close "Confirmed Booking" Yes, LNS CONFIRMED, or a clear texted yes before
the call. Showed / no-show = what the closers' EOC form set on the Close opportunity. Show rate = showed ÷
(showed + no-show).
