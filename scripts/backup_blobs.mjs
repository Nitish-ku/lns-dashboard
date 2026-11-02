// READ-ONLY backup of the private Blob store (every stored ROAS event) to a local folder.
// Uses only list() and get(); never put/del. Token from BLOB_READ_WRITE_TOKEN.
//   BLOB_READ_WRITE_TOKEN=... node scripts/backup_blobs.mjs <out dir>
import { get, list } from "@vercel/blob";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const out = process.argv[2];
if (!out) throw new Error("usage: node scripts/backup_blobs.mjs <out dir>");
const blobs = [];
let cursor;
do {
  const page = await list({ limit: 1000, cursor });
  blobs.push(...page.blobs);
  cursor = page.hasMore ? page.cursor : undefined;
} while (cursor);
let bytes = 0;
for (let i = 0; i < blobs.length; i += 20) {
  await Promise.all(blobs.slice(i, i + 20).map(async (b) => {
    for (let attempt = 1; ; attempt++) {
      try {
        const r = await get(b.url, { access: "private" });
        const text = await new Response(r.stream).text();
        const p = join(out, b.pathname);
        mkdirSync(dirname(p), { recursive: true });
        writeFileSync(p, text);
        bytes += text.length;
        return;
      } catch (e) {
        if (attempt >= 4) throw e;
        await new Promise((s) => setTimeout(s, 1000 * attempt));
      }
    }
  }));
}
writeFileSync(join(out, "_manifest.json"), JSON.stringify(blobs.map((b) => ({ pathname: b.pathname, size: b.size, uploadedAt: b.uploadedAt })), null, 1));
console.log(`backed up ${blobs.length} blobs (${bytes} bytes) to ${out}`);
