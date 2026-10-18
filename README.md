# Show Rate Ledger

AI Launch Call bookings since the first personalised confirmation text (Oct 11 2026): bookings, confirmations
and shows by tier (A / B / C), week by week.

- `index.html`: the page (no build step, no libraries). Reads `data/bookings.json`.
- `scripts/build_data.py`: reads GHL (appointments, tier = calendar) and Close (confirmation, EOC outcome on the
  opportunity) and writes one anonymous record per booking. Read only. No names, emails or phone numbers.
- `.github/workflows/refresh.yml`: runs the script every day at 11:00 UTC and commits the new data; GitHub Pages
  republishes the page.

## Set up (once)
1. Repository Settings → Secrets and variables → Actions → New repository secret, three times:
   `CLOSE_API_KEY`, `GHL_API_KEY` (the read-only key), `GHL_LOCATION_ID`.
2. Settings → Pages → Build and deployment → Source: "Deploy from a branch", branch `main`, folder `/ (root)`.
3. Actions → "Refresh data" → Run workflow, to check it works.

## Run locally
```
CLOSE_API_KEY=... GHL_API_KEY=... GHL_LOCATION_ID=... python scripts/build_data.py
python -m http.server 8000   # then open http://localhost:8000
```

## Definitions
See section 06 on the page. In short: confirmed = Close "Confirmed Booking" Yes, LNS CONFIRMED, or a clear texted yes
before the call; showed / no-show = what the closers' EOC form set on the Close opportunity; show rate =
showed ÷ (showed + no-show).
