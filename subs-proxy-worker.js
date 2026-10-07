// Moovidex subtitle CORS proxy — deploy FREE on Cloudflare Workers.
//
// 1. Go to https://dash.cloudflare.com -> Workers & Pages -> Create Worker
// 2. Delete the default code, paste THIS file, Deploy.
// 3. Copy your worker URL: https://<name>.<you>.workers.dev
// 4. On Voroa/Render, set env var:
//      SUBS_PROXY_URL=https://<name>.<you>.workers.dev
// 5. Redeploy the bot. Done — no API key needed.
//
// Why this works: OpenSubtitles 403s datacenter IPs (Render/Voroa).
// Requests now egress from Cloudflare's IP instead. Free tier =
// 100,000 requests/day, plenty for subtitles.

export default {
  async fetch(request) {
    const target = new URL(request.url).searchParams.get("url");
    if (!target || !target.startsWith("https://")) {
      return new Response("missing ?url=https://...", { status: 400 });
    }
    // Only allow OpenSubtitles hosts (don't run an open proxy).
    let host;
    try {
      host = new URL(target).hostname;
    } catch {
      return new Response("bad url", { status: 400 });
    }
    if (host !== "rest.opensubtitles.org" && host !== "dl.opensubtitles.org") {
      return new Response("host not allowed", { status: 403 });
    }
    const res = await fetch(target, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-User-Agent": "MoovidexBot/1.0",
        Accept: "application/json",
      },
    });
    return new Response(res.body, {
      status: res.status,
      headers: {
        "Content-Type":
          res.headers.get("Content-Type") || "application/octet-stream",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
