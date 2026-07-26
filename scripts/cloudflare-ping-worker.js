// Cloudflare Worker — будит BANANAWOW каждые 5 мин (free, не засыпает как Render)
//
// 1) https://dash.cloudflare.com → Workers & Pages → Create Worker
// 2) Вставь ВЕСЬ этот файл → Deploy
// 3) Замени TARGET на свой Render URL (…/api/ping)
// 4) Settings → Triggers → Cron Triggers → Add:
//    */5 * * * *
//
// После деплоя Render: скопируй https://ВАШ-СЕРВИС.onrender.com/api/ping

const TARGET = "https://YOUR-SERVICE.onrender.com/api/ping";

async function ping() {
  const started = Date.now();
  try {
    const res = await fetch(TARGET, {
      method: "GET",
      headers: { "User-Agent": "bananawow-cf-keepalive/1.0" },
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
