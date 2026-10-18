import assert from "node:assert/strict";
import { Monitor } from "./worker.js";
const store = new Map(), sent = [];
let t = 1_800_000_000, tg = true;
const ctx = { storage: { get: async (k) => store.get(k), put: async (k, v) => store.set(k, structuredClone(v)) } };
const m = new Monitor(ctx, { TELEGRAM_BOT_TOKEN: "x", TELEGRAM_CHAT_ID: "1", DAILY_HOUR_UTC: "-1" },
  { now: () => t, fetch: async (_u, o) => { if (!tg) return { json: async () => ({ ok: false }) }; sent.push(JSON.parse(o.body).text); return { json: async () => ({ ok: true }) }; } });
await m.ping(); t += 200; await m.tick(); assert.equal(sent.length, 0);          // inside grace
t += 100; await m.tick(); assert.equal(sent.length, 1); assert.match(sent[0], /no check-in/);
t += 60; await m.tick(); assert.equal(sent.length, 1);                              // not repeated
t += 600; await m.ping(); assert.match(sent.at(-1), /back after/); assert.equal(m.status(await m.load()).up, true);
await m.ping("fail"); assert.match(sent.at(-1), /reported a failure/);
await m.ping(); tg = false; t += 400; await m.tick();
assert.equal((await m.load()).outbox.length, 1);                                    // Telegram down: kept, not lost
tg = true; t += 15; await m.tick(); assert.equal((await m.load()).outbox.length, 0);
console.log("worker ok");
// The runtime calls fetch() on the Durable Object: make sure the routes exist under that exact name.
const get = async (p) => m.fetch(new Request("https://monitor" + p));
assert.equal(typeof m.fetch, "function");
assert.equal((await get("/ping")).status, 200);
const st = await (await get("/status")).json(); assert.equal(st.up, true);
assert.equal((await get("/tick")).status, 200);
assert.equal((await get("/other")).status, 404);
console.log("do fetch ok");
