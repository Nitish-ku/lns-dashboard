// Build step 1: collect every stored ROAS event (api/roas.js) into one file OUTSIDE the published folder, so the raw
// events are never served; build_data.py turns them into anonymous application records.
//   ROAS_EVENTS_OUT (default: <tmp>/roas_events.json)
// Local runs without BLOB_READ_WRITE_TOKEN: reads ROAS_EVENTS_DIR (a folder of saved events) or writes an empty list.
import { get, list } from "@vercel/blob";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const out = process.env.ROAS_EVENTS_OUT || join(tmpdir(), "roas_events.json");
let events = [];

if (process.env.BLOB_READ_WRITE_TOKEN) {
  const blobs = [];
  let cursor;
  do {
    const page = await list({ prefix: "roas/", limit: 1000, cursor });
    blobs.push(...page.blobs);
    cursor = page.hasMore ? page.cursor : undefined;
  } while (cursor);
  for (let i = 0; i < blobs.length; i += 20) {
    // private store: read through the SDK (authenticated with BLOB_READ_WRITE_TOKEN), never a public URL
    const batch = await Promise.all(blobs.slice(i, i + 20).map(async (b) => {
      try {
        const r = await get(b.url, { access: "private" });
        return r && r.stream ? await new Response(r.stream).json() : null;
      } catch { return null; }
    }));
    events.push(...batch.filter(Boolean));
  }
} else if (process.env.ROAS_EVENTS_DIR) {
  for (const f of readdirSync(process.env.ROAS_EVENTS_DIR).filter((f) => f.endsWith(".json"))) {
    events.push(JSON.parse(readFileSync(join(process.env.ROAS_EVENTS_DIR, f), "utf8")));
  }
}

writeFileSync(out, JSON.stringify(events));
console.log(`${events.length} ROAS events -> ${out}`);
