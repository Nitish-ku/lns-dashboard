// Password gate (browser login box) for the whole site, set DASHBOARD_PASSWORD in Vercel. Any username works.
// /api/roas (ROAS webhook, own secret) and /api/refresh (Vercel cron, own secret) stay reachable.
export const config = { matcher: ["/((?!api/).*)"] };

export default function middleware(request) {
  const want = process.env.DASHBOARD_PASSWORD;
  if (!want) return new Response("Dashboard password not configured", { status: 503 });
  const auth = request.headers.get("authorization") || "";
  if (auth.startsWith("Basic ")) {
    const [, pass] = atob(auth.slice(6)).split(/:(.*)/s);
    if (pass === want) return;
  }
  return new Response("Password required", { status: 401, headers: { "WWW-Authenticate": 'Basic realm="Show Rate Ledger"' } });
}
