// AETHER outside monitor on Cloudflare Workers: your own healthchecks.io, no extra hardware.
// The trading server pings  https://<worker-host>/ping/<secret>  every minute (the engine already does this
// when HEALTHCHECK_URL is set). A cron trigger checks every minute; if pings stop for GRACE seconds you get a
// Telegram message. One Durable Object holds the state, so nothing depends on your Hetzner server.
//
// Routes: GET|POST /ping/<secret>[/start|/fail]   GET /status/<secret>   GET /healthz
// Secrets (wrangler secret put): DEADMAN_SECRET (>=16 chars), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
// Optional vars: GRACE_S (240), REMIND_S (21600), DAILY_HOUR_UTC (5, -1 = off)

const dur = (s) => { s = Math.max(0, Math.round(s)); return s < 120 ? `${s} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`; };

async function same(a, b) {                       // constant time: compare SHA-256 digests byte by byte
  const enc = new TextEncoder();
  const [x, y] = await Promise.all([crypto.subtle.digest("SHA-256", enc.encode(a)), crypto.subtle.digest("SHA-256", enc.encode(b))]);
  const p = new Uint8Array(x), q = new Uint8Array(y);
  let d = 0;
  for (let i = 0; i < p.length; i++) d |= p[i] ^ q[i];
  return d === 0;
}

export class Monitor {
  constructor(ctx, env, deps = {}) {
    this.ctx = ctx; this.env = env;
    this.now = deps.now || (() => Date.now() / 1000);
    this.fetch = deps.fetch || ((...a) => fetch(...a));
  }
  cfg() {
    const n = (v, d) => (v === undefined || v === "" || Number.isNaN(Number(v)) ? d : Number(v));
    return { grace: n(this.env.GRACE_S, 240), remind: n(this.env.REMIND_S, 21600), daily: n(this.env.DAILY_HOUR_UTC, 5) };
  }
  async load() {
    const s = (await this.ctx.storage.get("s")) || {};
    return { last_ping: null, status: "new", down_since: null, last_alert: null, created: this.now(), reason: "", outbox: [], daily: "", ...s };
  }
  async save(s) { await this.ctx.storage.put("s", s); }

  async ping(kind) {
    const s = await this.load(), now = this.now();
    if (kind === "fail") {
      if (s.status !== "down" || s.reason !== "reported failure") s.outbox.push("\u{1F534} aether: the server reported a failure (it pinged /fail). Check it now.");
      Object.assign(s, { status: "down", down_since: s.down_since || now, last_alert: now, reason: "reported failure", last_ping: now });
    } else {
      if (s.status === "down") s.outbox.push(`\u{1F7E2} aether is back after ${dur(now - (s.down_since || now))}.`);
      Object.assign(s, { status: "up", down_since: null, last_ping: now, last_alert: null, reason: "" });
    }
    await this.save(s);
    return this.flush(s);                          // deliver promptly instead of waiting for the next cron tick
  }

  async tick() {
    const s = await this.load(), now = this.now(), c = this.cfg();
    if (s.status === "up" && s.last_ping && now - s.last_ping > c.grace) {
      Object.assign(s, { status: "down", down_since: s.last_ping, last_alert: now, reason: "no check-in" });
      s.outbox.push(`\u{1F534} aether: no check-in for ${dur(now - s.last_ping)}. The server, its internet link, its tunnel or the engine may be down. Check the Hetzner console.`);
    } else if (s.status === "new" && now - s.created > c.grace * 3) {
      Object.assign(s, { status: "down", down_since: s.created, last_alert: now, reason: "never checked in" });
      s.outbox.push("\u{1F7E0} aether: no check-in has ever arrived. Is HEALTHCHECK_URL set on the server, and can it reach this monitor?");
    } else if (s.status === "down" && c.remind && s.last_alert && now - s.last_alert >= c.remind) {
      s.last_alert = now;
      s.outbox.push(`\u{1F534} aether is still down (${dur(now - (s.down_since || now))}, ${s.reason || "no check-in"}).`);
    }
    const d = new Date(now * 1000), day = d.toISOString().slice(0, 10);
    if (c.daily >= 0 && d.getUTCHours() === c.daily && s.daily !== day) {
      s.daily = day;
      s.outbox.push("\u{1F4A4} Monitor alive. aether: " + (s.last_ping ? `${s.status}, last check-in ${dur(now - s.last_ping)} ago` : "no ping yet"));
    }
    await this.save(s);
    return this.flush(s);
  }

  async flush(s) {
    let sent = 0;
    while (s.outbox.length && sent < 5) {
      let ok = false;
      try { ok = await this.telegram(s.outbox[0]); } catch (_) { ok = false; }
      if (!ok) break;                              // stays queued; retried on the next tick
      s.outbox.shift(); sent++;
    }
    if (sent) await this.save(s);
    return s;
  }

  async telegram(text) {
    const { TELEGRAM_BOT_TOKEN: t, TELEGRAM_CHAT_ID: c } = this.env;
    if (!t || !c) return false;
    const r = await this.fetch(`https://api.telegram.org/bot${t}/sendMessage`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chat_id: c, text: text.slice(0, 3900) }),
    });
    const j = await r.json().catch(() => ({}));
    return !!j.ok;
  }

  status(s) {
    const now = this.now(), c = this.cfg();
    return { name: "aether", status: s.status, up: s.status === "up", last_ping_age_s: s.last_ping ? Math.round(now - s.last_ping) : null,
      down_for_s: s.status === "down" && s.down_since ? Math.round(now - s.down_since) : null, grace_s: c.grace, queued_alerts: s.outbox.length };
  }

  async fetchHandler(request) {                    // called by the Worker only after the secret was verified
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    if (parts[0] === "ping") { await this.ping(parts[2] === "fail" ? "fail" : ""); return new Response("OK"); }
    if (parts[0] === "status") return new Response(JSON.stringify(this.status(await this.load())), { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
    if (parts[0] === "tick") { await this.tick(); return new Response("OK"); }
    return new Response("not found", { status: 404 });
  }
}

export default {
  async fetch(request, env) {
    const parts = new URL(request.url).pathname.split("/").filter(Boolean);
    if (parts.length === 1 && parts[0] === "healthz") return new Response("OK");
    const okShape = (parts[0] === "ping" && (parts.length === 2 || (parts.length === 3 && ["fail", "start"].includes(parts[2])))) || (parts[0] === "status" && parts.length === 2);
    const secret = env.DEADMAN_SECRET || "";
    if (!okShape || secret.length < 16 || !(await same(parts[1] || "", secret))) return new Response("not found", { status: 404 });
    return env.MONITOR.get(env.MONITOR.idFromName("aether")).fetch(request);
  },
  async scheduled(_event, env) {
    await env.MONITOR.get(env.MONITOR.idFromName("aether")).fetch("https://monitor/tick");
  },
};
