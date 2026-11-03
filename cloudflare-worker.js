// Svitlo Worker v1.1 | wakes the GitHub bot every 10 min and on Telegram messages
const REPO = "Earanak/svitlo-bot";
const WORKFLOW = "svitlo.yml";
const CHAT_ID = 741998828;

let lastWake = 0;
let lastUpdate = 0;

async function dispatch(env, reply, wait) {
  const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "svitlo-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main", inputs: { reply: reply ? "1" : "0", wait: wait ? String(wait) : "" } }),
  });
  return r.status;
}

async function tg(env, method, params) {
  const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return r.json();
}

async function secretOf(token) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 40);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env, false));
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const secret = await secretOf(env.TG_TOKEN || "");

    if (url.pathname === "/setup") {
      const hook = await tg(env, "setWebhook", {
        url: `${url.origin}/tg`, secret_token: secret,
        allowed_updates: ["message"], drop_pending_updates: true,
      });
      const gh = await dispatch(env, true);
      return new Response(
        `Telegram: ${hook.ok ? "OK" : "ERROR " + hook.description}\n` +
        `GitHub: ${gh === 204 ? "OK" : "ERROR " + gh}`,
        { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    }

    if (url.pathname === "/tg" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) {
        return new Response("forbidden", { status: 403 });
      }
      const u = await request.json();
      const m = u && u.message;
      const fresh = m && Date.now() / 1000 - m.date < 90;
      if (m && m.chat && m.chat.id === CHAT_ID && fresh && u.update_id > lastUpdate) {
        lastUpdate = u.update_id;
        if (Date.now() - lastWake > 45000) {
          lastWake = Date.now();
          ctx.waitUntil((async () => {
            const w = await tg(env, "sendMessage", {
              chat_id: CHAT_ID, disable_notification: true, text: "⏳ Обновляю, пришлю через ~1 мин" });
            const wid = w && w.result && w.result.message_id;
            const st = await dispatch(env, true, wid);
            if (st !== 204 && wid) {
              await tg(env, "editMessageText", { chat_id: CHAT_ID, message_id: wid,
                text: `⚠️ Не удалось разбудить бота (GitHub ${st})` });
            }
          })());
        }
      }
      return new Response("ok");
    }

    return new Response("svitlo worker");
  },
};
