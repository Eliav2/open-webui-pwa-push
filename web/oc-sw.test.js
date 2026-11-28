// node --test web/oc-sw.test.js: the service worker's push handler, run in a
// sandbox with a fake registration (Chrome's tag rules: a new notification with a
// tag replaces the shown one with that tag; close() removes it).
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const SW = process.env.OC_SW || new URL("oc-sw.js", import.meta.url);
const MAX_LINES = 5;

function worker({ site = {} } = {}) {
  const listeners = {};
  const shown = new Map();   // tag -> notification
  let clock = 0;             // each notification's timestamp
  // Cache Storage: one store per name, urls as keys.
  const stores = new Map();
  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const m = stores.get(name);
      const key = (k) => new URL(typeof k === "string" ? k : k.url, "https://chat.example").href;
      return {
        async put(k, r) { m.set(key(k), r); },
        async match(k) { return m.get(key(k)); },
        async delete(k) { return m.delete(key(k)); },
        async keys() { return [...m.keys()].map((url) => ({ url })); },
      };
    },
  };
  // Open WebUI's socket as markRead uses it: each read event is recorded and answered.
  const reads = [];
  class WebSocket {
    constructor() { queueMicrotask(() => this.onmessage?.({ data: "0{}" })); }
    send(f) {
      if (f.startsWith("40")) return queueMicrotask(() => this.onmessage?.({ data: "40{}" }));
      if (f.startsWith("42")) {
        const [, ev] = JSON.parse(f.slice(2));
        reads.push(ev.chat_id);
        queueMicrotask(() => this.onmessage?.({ data: "42" + JSON.stringify(["events", { chat_id: ev.chat_id, data: { type: "chat:list" } }]) }));
      }
    }
    close() {}
  }
  // The server: `site` answers its paths (a function per path), anything else gets a token.
  const fetched = [];
  const opened = [];   // windows the worker opened (no page was open), by url
  // Open WebUI's state API: every call recorded; the user's settings and the function's 0.6.0 user valves
  // answer from `server` (a test sets them). Open WebUI 0.13's settings update replaces only the keys it is sent.
  const calls = [];
  const server = { settings: {}, legacyValves: null, legacyStatus: 200 };
  const fetch = async (url, init = {}) => {
    const path = new URL(url, "https://chat.example").pathname;
    fetched.push(path);
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ path, method: init.method || "GET", body });
    if (path === "/api/v1/users/user/settings") return new Response(JSON.stringify(server.settings), { status: 200 });
    if (path === "/api/v1/users/user/settings/update") {
      Object.assign(server.settings, body);
      return new Response(JSON.stringify(server.settings), { status: 200 });
    }
    if (/\/valves\/user$/.test(path)) return new Response(JSON.stringify(server.legacyValves), { status: server.legacyStatus });
    return site[path] ? site[path]() : new Response(JSON.stringify({ token: "t" }), { status: 200 });
  };
  const registration = {
    async getNotifications({ tag } = {}) {
      return [...shown.values()].filter((n) => !tag || n.tag === tag);
    },
    async showNotification(title, options) {
      const n = { title, ...options, timestamp: ++clock, close: () => shown.delete(options.tag) };
      shown.set(options.tag, n);
    },
  };
  const self = {
    navigator: { userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", maxTouchPoints: 0 },
    location: { origin: "https://chat.example" },
    registration,
    clients: { matchAll: async () => [], claim: async () => {}, openWindow: async (url) => { opened.push(url); } },
    addEventListener: (type, fn) => { listeners[type] = fn; },
  };
  vm.runInNewContext(fs.readFileSync(SW, "utf8"), { self, URL, console, setTimeout, clearTimeout, caches, WebSocket, fetch,
    Response, AbortSignal, queueMicrotask, btoa });
  async function push(payload) {
    let done;
    listeners.push({ data: { json: () => payload }, waitUntil: (pr) => { done = pr; } });
    await done;
  }
  // A notification event as Chrome sends it; resolves once the worker is done with it.
  async function fire(type, n, extra = {}) {
    const work = [];
    listeners[type]({ notification: n, action: "", ...extra, waitUntil: (pr) => work.push(pr) });
    await Promise.allSettled(work);
  }
  // A swipe: Android removes the notification, then Chrome fires notificationclose.
  const swipe = (n) => { n.close(); return fire("notificationclose", n); };
  return { push, shown, tag: (id) => shown.get("chat-" + id), channel: (id) => shown.get("channel-" + id), fire, swipe, reads, caches, fetched, calls, server, opened };
}

const reply = (chat, body, title = "Trip plan") => ({ chat_id: chat, title, body, url: "/c/" + chat });

// An answer from this site as the browser gives it to the worker (same origin: "basic").
const own = (body, type) => Object.defineProperty(new Response(body, { status: 200, headers: { "content-type": type } }), "type", { value: "basic" });
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 1, 2, 250, 251, 252]);
const PNG_URL = "data:image/png;base64," + Buffer.from(PNG).toString("base64");
const SIGN_IN = () => own("<html>Sign in</html>", "text/html");
const manifest = (icons) => () => own(JSON.stringify({ name: "Open WebUI", icons }), "application/json");

test("the picture is Open WebUI's own app icon, from its manifest, read once; the badge is the browser's", async () => {
  const w = worker({ site: {
    "/manifest.json": manifest([{ src: "/static/small.png", sizes: "96x96" },
      { src: "/static/logo.png", sizes: "500x500", purpose: "any" }, { src: "/static/mask.png", sizes: "1024x1024", purpose: "maskable" }]),
    "/static/logo.png": () => own(PNG, "image/png"),
  } });
  await w.push(reply("a", "one"));
  await w.push(reply("b", "two"));
  assert.equal(w.tag("a").icon, PNG_URL);
  assert.equal(w.tag("b").icon, PNG_URL);
  assert.equal(w.tag("a").badge, undefined);
  assert.deepEqual(w.fetched, ["/manifest.json", "/static/logo.png"]);
});

test("a manifest without icons: Open WebUI's favicon", async () => {
  const w = worker({ site: { "/manifest.json": manifest([]), "/static/favicon.png": () => own(PNG, "image/png") } });
  await w.push(reply("a", "one"));
  assert.equal(w.tag("a").icon, PNG_URL);
});

test("a sign-in page instead of the icon: no picture, and the next push reads it again", async () => {
  const site = { "/manifest.json": SIGN_IN, "/static/favicon.png": SIGN_IN };
  const w = worker({ site });
  await w.push(reply("a", "one"));
  assert.equal(w.tag("a").icon, undefined);
  site["/static/favicon.png"] = () => own(PNG, "image/png");
  await w.push(reply("a", "two"));
  assert.equal(w.tag("a").icon, PNG_URL);
});

test("the function's NOTIFICATION_ICON / NOTIFICATION_BADGE: used as they are, Open WebUI's icon not read", async () => {
  const w = worker({ site: { "/manifest.json": manifest([{ src: "/static/logo.png" }]), "/static/logo.png": () => own(PNG, "image/png") } });
  await w.push({ ...reply("a", "one"), icon: "https://cdn.example/team.png", badge: "/static/badge.png" });
  assert.equal(w.tag("a").icon, "https://cdn.example/team.png");
  assert.equal(w.tag("a").badge, "/static/badge.png");
  assert.deepEqual(w.fetched, []);
});

test("one reply: the chat's title and the preview, as before", async () => {
  const w = worker();
  await w.push(reply("a", "First answer"));
  assert.equal(w.tag("a").title, "Trip plan");
  assert.equal(w.tag("a").body, "First answer");
});

test("replies in one chat stack in one notification: the count in the title, newest last", async () => {
  const w = worker();
  await w.push(reply("a", "First answer"));
  await w.push(reply("a", "Second answer"));
  await w.push(reply("a", "Third answer"));
  assert.equal(w.shown.size, 1);
  assert.equal(w.tag("a").title, "Trip plan · 3 new messages");
  assert.equal(w.tag("a").body, "First answer\nSecond answer\nThird answer");
  assert.equal(w.tag("a").renotify, true);
});

test("the body keeps the latest lines only, each on one trimmed line", async () => {
  const w = worker();
  for (let i = 1; i <= MAX_LINES + 2; i++) await w.push(reply("a", `  answer\n ${i}  `));
  const lines = w.tag("a").body.split("\n");
  assert.equal(lines.length, MAX_LINES);
  assert.equal(lines[0], "answer 3");
  assert.equal(lines.at(-1), `answer ${MAX_LINES + 2}`);
  assert.match(w.tag("a").title, new RegExp(`${MAX_LINES + 2} new messages`));
});

test("a long preview is cut to one line with an ellipsis", async () => {
  const w = worker();
  await w.push(reply("a", "word ".repeat(100)));
  await w.push(reply("a", "short"));
  const [first] = w.tag("a").body.split("\n");
  assert.ok(first.endsWith("…") && first.length < 200, first);
});

test("another chat has its own notification and count", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  await w.push(reply("a", "two"));
  await w.push(reply("b", "other", "Groceries"));
  assert.equal(w.shown.size, 2);
  assert.equal(w.tag("b").title, "Groceries");
  assert.equal(w.tag("b").body, "other");
});

test("a closed notification (chat opened, marked read, swiped) starts the stack again", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  await w.push(reply("a", "two"));
  w.tag("a").close();
  await w.push(reply("a", "three"));
  assert.equal(w.tag("a").title, "Trip plan");
  assert.equal(w.tag("a").body, "three");
});

test("without previews the body stays 'New reply' and the title still counts", async () => {
  const w = worker();
  await w.push({ chat_id: "a", title: "Trip plan", url: "/c/a" });
  await w.push({ chat_id: "a", title: "Trip plan", url: "/c/a" });
  assert.equal(w.tag("a").body, "New reply");
  assert.equal(w.tag("a").title, "Trip plan · 2 new messages");
});

test("a swiped notification marks its chat read (once, however many replies it stacked)", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  await w.push(reply("a", "two"));
  await w.swipe(w.tag("a"));
  assert.deepEqual(w.reads, ["a"]);
});

test("swipe_read off (the admin's valve): a swipe only hides it", async () => {
  const w = worker();
  await w.push({ ...reply("a", "one"), swipe_read: false });
  await w.swipe(w.tag("a"));
  assert.deepEqual(w.reads, []);
});

test("a notification this code closed (a tap; the chat opened, as oc-push.js does) is not a swipe", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  const tapped = w.tag("a");
  await w.fire("notificationclick", tapped, { action: "mute" });   // its own close; the mute's fetch is not the point
  await w.fire("notificationclose", tapped);   // should Chrome send a close for it
  await w.push(reply("b", "one"));
  const opened = w.tag("b");
  const own = await w.caches.open("oc-push-own-closes");   // oc-push.js's mark, then its close()
  await own.put("/oc-push/closed/" + opened.tag + "/" + opened.timestamp, new Response(""));
  opened.close();
  await w.fire("notificationclose", opened);
  assert.deepEqual(w.reads, []);
});

test("a notification replaced by the chat's next one is not a swipe; a later swipe of the new one is", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  const old = w.tag("a");
  await w.push(reply("a", "two"));   // same tag: Chrome replaces it
  await w.fire("notificationclose", old);
  assert.deepEqual(w.reads, []);
  await w.swipe(w.tag("a"));
  assert.deepEqual(w.reads, ["a"]);
});

test("an own close of an earlier notification does not hide a swipe of the chat's next one", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  const first = w.tag("a");
  const own = await w.caches.open("oc-push-own-closes");
  await own.put("/oc-push/closed/" + first.tag + "/" + first.timestamp, new Response(""));
  first.close();   // Chrome sent no close for it: its mark stays
  await w.push(reply("a", "two"));
  await w.swipe(w.tag("a"));
  assert.deepEqual(w.reads, ["a"]);
});

test("a swipe, then the chat's next notification before the worker handles the close: still read", async () => {
  const w = worker();
  await w.push(reply("a", "one"));
  const swiped = w.tag("a");
  swiped.close();                      // gone from the shade
  await w.push(reply("a", "two"));     // the next reply's notification, first
  await w.fire("notificationclose", swiped);
  assert.deepEqual(w.reads, ["a"]);
});

test("Mute writes the function's state in the user's settings, never its user valves (ELI-442)", async () => {
  const w = worker();
  w.server.settings = { ui: { theme: "dark" }, "web_push:fn1": { subscriptions: "[{\"endpoint\":\"e1\"}]", mutes: "{}" } };
  w.server.legacyValves = { folder_mutes: "{\"f1\":0}", mutes: "{\"old\":0}" };
  await w.push({ ...reply("a", "one"), fn: "fn1", actions: ["mute"] });
  await w.fire("notificationclick", w.tag("a"), { action: "mute" });
  assert.ok(!w.calls.some((c) => c.method === "POST" && /valves/.test(c.path)), "no write to user valves");
  const state = w.server.settings["web_push:fn1"];
  assert.ok(JSON.parse(state.mutes).a > 0, "the chat is muted");
  assert.equal(state.subscriptions, "[{\"endpoint\":\"e1\"}]", "the devices are kept");
  assert.equal(state.folder_mutes, "{\"f1\":0}", "a field only the 0.6.0 user valves had is carried over");
  assert.deepEqual(w.server.settings.ui, { theme: "dark" }, "the rest of the settings is untouched");
});

test("Mute writes nothing when the old user valves cannot be read (they may hold the devices)", async () => {
  const w = worker();
  w.server.legacyValves = { subscriptions: "[{\"endpoint\":\"e1\"}]" };
  w.server.legacyStatus = 503;
  await w.push({ ...reply("a", "one"), fn: "fn1", actions: ["mute"] });
  await w.fire("notificationclick", w.tag("a"), { action: "mute" });
  assert.ok(!w.calls.some((c) => c.method === "POST" && /settings\/update/.test(c.path)),
    "a write with the devices missing would wipe them");
});

// A channel message (ELI-548): no chat_id, so no buttons and no read; its own tag per channel.
const message = (channel, body, title = "#Japan") => ({ kind: "channel.message", channel_id: channel, title, body, url: "/channels/" + channel });

test("a channel message shows its title, 'New message' without a preview, and no buttons", async () => {
  const w = worker();
  await w.push({ ...message("ch1"), fn: "fn1", actions: ["reply", "mute"] });
  const n = w.channel("ch1");
  assert.equal(n.title, "#Japan");
  assert.equal(n.body, "New message");
  assert.deepEqual(n.actions, []);
  assert.equal(n.tag, "channel-ch1");
  assert.equal(n.data.chat_id, "");
});

test("messages in one channel stack in one notification; another channel and a chat with the same id have their own", async () => {
  const w = worker();
  await w.push(message("a", "one"));
  await w.push(message("a", "two"));
  await w.push(message("b", "other", "#Food"));
  await w.push(reply("a", "a chat reply"));
  assert.equal(w.channel("a").title, "#Japan · 2 new messages");
  assert.equal(w.channel("a").body, "one\ntwo");
  assert.equal(w.channel("b").title, "#Food");
  assert.equal(w.tag("a").title, "Trip plan");
  assert.equal(w.shown.size, 3);
});

test("a tap on a channel notification opens /channels/<id>", async () => {
  const w = worker();
  await w.push(message("ch1", "hi"));
  await w.fire("notificationclick", w.channel("ch1"));
  assert.deepEqual(w.opened, ["https://chat.example/channels/ch1"]);
});

test("swiping a channel notification marks nothing read (a channel is not a chat)", async () => {
  const w = worker();
  await w.push(message("ch1", "hi"));
  await w.swipe(w.channel("ch1"));
  assert.deepEqual(w.reads, []);
  assert.equal(w.calls.filter((c) => c.method === "POST").length, 0);
});

test("a closed channel notification starts its stack again", async () => {
  const w = worker();
  await w.push(message("ch1", "one"));
  await w.push(message("ch1", "two"));
  w.channel("ch1").close();
  await w.push(message("ch1", "three"));
  assert.equal(w.channel("ch1").title, "#Japan");
  assert.equal(w.channel("ch1").body, "three");
});

test("a push with neither chat_id nor channel_id still shows, under one shared tag, as before", async () => {
  const w = worker();
  await w.push({ title: "x", url: "/" });
  assert.equal([...w.shown.keys()].join(), "chat-none");
});
