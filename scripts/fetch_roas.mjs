// Build step 1: collect every stored ROAS event (api/roas.js) into one file OUTSIDE the published folder, so the raw
// events are never served; build_data.py turns them into anonymous application records.
//   ROAS_EVENTS_OUT (default: <tmp>/roas_events.json)
// Local runs without blob credentials: reads ROAS_EVENTS_DIR (a folder of saved events) or writes an empty list.
import { get, list } from "@vercel/blob";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const out = process.env.ROAS_EVENTS_OUT || join(tmpdir(), "roas_events.json");
let events = [];

// Vercel: the store connection sets BLOB_STORE_ID and the SDK signs in with the build's OIDC token (no read-write
// token needed); BLOB_READ_WRITE_TOKEN also works. If listing fails the build fails, so stale data stays live.
if (process.env.BLOB_STORE_ID || process.env.BLOB_READ_WRITE_TOKEN) {
  const blobs = [];
  let cursor;
  do {
    const page = await list({ prefix: "roas/", limit: 1000, cursor });
    blobs.push(...page.blobs);
    cursor = page.hasMore ? page.cursor : undefined;
  } while (cursor);
  for (let i = 0; i < blobs.length; i += 20) {
    // private store: read through the SDK (authenticated with BLOB_READ_WRITE_TOKEN), never a public URL
    // a failed read is retried; if it still fails the BUILD fails, so yesterday's numbers stay live instead of
    // quietly publishing fewer applications (critic #5)
    const batch = await Promise.all(blobs.slice(i, i + 20).map(async (b) => {
      for (let attempt = 1; ; attempt++) {
        try {
          const r = await get(b.url, { access: "private" });
          if (!r) return null;  // deleted between list() and get(): nothing to count
          const text = await new Response(r.stream).text();
          try { return JSON.parse(text); }
          catch { console.error(`skipped unreadable event (not JSON): ${b.pathname}`); return null; }  // never retry
        } catch (err) {
          if (attempt >= 4) { console.error(`blob read failed after 4 tries: ${b.pathname}: ${err.message}`); process.exit(1); }
          await new Promise((ok) => setTimeout(ok, 1000 * 2 ** attempt));
        }
      }
    }));
    events.push(...batch.filter(Boolean));
  }
} else if (process.env.VERCEL) {
  // on Vercel the store must be connected; without it the page would silently show "Collecting" (critic #5)
  console.error("No Blob credentials (BLOB_STORE_ID / BLOB_READ_WRITE_TOKEN): connect the Blob store to this project");
  process.exit(1);
} else if (process.env.ROAS_EVENTS_DIR) {
  for (const f of readdirSync(process.env.ROAS_EVENTS_DIR).filter((f) => f.endsWith(".json"))) {
    events.push(JSON.parse(readFileSync(join(process.env.ROAS_EVENTS_DIR, f), "utf8")));
  }
}

writeFileSync(out, JSON.stringify(events));
console.log(`${events.length} ROAS events -> ${out}`);
