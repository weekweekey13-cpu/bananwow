// Cloudflare Worker — будит Render каждые 5 мин (бесплатно, сам не спит)
//
// 1) https://dash.cloudflare.com → Workers & Pages → Create Worker
// 2) Вставь ВЕСЬ этот код → Deploy
// 3) Settings → Triggers → Cron Triggers → Add:  */5 * * * *

const TARGET = "https://bananwow.onrender.com/api/ping";

async function ping() {
  const started = Date.now();
  try {
    const res = await fetch(TARGET, {
      method: "GET",
      headers: { "User-Agent": "bananwow-keepalive/1.0" },
    });
    const text = await res.text();
    return {
      ok: res.ok,
      status: res.status,
      ms: Date.now() - started,
      body: text.slice(0, 200),
      target: TARGET,
      at: new Date().toISOString(),
    };
  } catch (e) {
    return {
      ok: false,
      error: String(e && e.message ? e.message : e),
      ms: Date.now() - started,
      target: TARGET,
      at: new Date().toISOString(),
    };
  }
}

export default {
  async fetch() {
    const result = await ping();
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(ping());
  },
};
