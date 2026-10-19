// Password gate (browser login box) for the whole site, set DASHBOARD_PASSWORD in Vercel. Any username works.
// /api/roas (ROAS webhook, own secret) and /api/refresh (Vercel cron, own secret) stay outside it; everything else,
// including /api/today (live availability, team names and Close links), is behind the password.
// exact route names only, so a future api/roas-something is gated by default (critic r9 s1)
export const config = { matcher: ["/((?!api/(?:roas|refresh)(?:/|$)).*)"] };

const enc = new TextEncoder();
async function digest(s) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}
// constant-time comparison of fixed-length digests (critic #9; the Edge runtime has no timingSafeEqual)
async function same(a, b) {
  const [x, y] = await Promise.all([digest(a), digest(b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

export default async function middleware(request) {
  const want = process.env.DASHBOARD_PASSWORD;
  if (!want) return new Response("Dashboard password not configured", { status: 503 });
  const auth = request.headers.get("authorization") || "";
  if (auth.startsWith("Basic ")) {
    let decoded = "";
    try {
      // UTF-8 aware, so a password with non-ASCII characters also works
      decoded = new TextDecoder().decode(Uint8Array.from(atob(auth.slice(6)), (c) => c.charCodeAt(0)));
    } catch {
      decoded = "";
    }
    const pass = decoded.includes(":") ? decoded.slice(decoded.indexOf(":") + 1) : "";
    if (pass && (await same(pass, want))) return;
  }
  return new Response("Password required", { status: 401, headers: { "WWW-Authenticate": 'Basic realm="Show Rate Ledger", charset="UTF-8"' } });
}
