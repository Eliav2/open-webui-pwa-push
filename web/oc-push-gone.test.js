// node --test web/oc-push-gone.test.js: a push service can retire a subscription the browser still holds
// (Chrome keeps listing it, no pushsubscriptionchange). The function then remembers it by fingerprint in the
// user's `gone` state (openclaw_web_push.py). On the next load the page replaces it with a new subscription
// instead of saving the dead one again.
import test from "node:test";
import assert from "node:assert";
import crypto from "node:crypto";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync(process.env.OC_PUSH || new URL("oc-push.js", import.meta.url), "utf8");
const VAPID = "BAAAAAAA";   // base64url, as the proxy fills it in
const FINGERPRINT_CHARS = 32;   // openclaw_web_push.py
const fingerprint = (e) => crypto.createHash("sha256").update(e).digest("hex").slice(0, FINGERPRINT_CHARS);
const OLD = "https://fcm.example/send/retired", NEW = "https://fcm.example/send/fresh";

const STATE = "web_push:web_push";   // the function's state in the user's settings (ELI-442)

// `state`: what the settings hold under STATE; `legacy`: what 0.6.0 kept in the user valves (null: no record).
function page(state, legacy = null) {
  const settings = { ui: { theme: "dark" }, [STATE]: state };
  const local = new Map([["token", "user-token"], ["ocPushUser", "u1"], ["ocPushDevice", "dev1"]]);
  const writes = [], unsubscribed = [];
  const keyBytes = Uint8Array.from(Buffer.from(VAPID, "base64url"));
  const subscription = (endpoint) => ({
    endpoint, options: { applicationServerKey: keyBytes.buffer },
    toJSON: () => ({ endpoint, keys: { p256dh: "p", auth: "a" } }),
    unsubscribe: async () => { unsubscribed.push(endpoint); return true; },
  });
  let current = subscription(OLD), subscribes = 0;
  const reg = {
    pushManager: { getSubscription: async () => current, subscribe: async () => { subscribes += 1; return (current = subscription(NEW)); } },
    update: async () => {}, getNotifications: async () => [],
  };
  const fetch = async (url, init = {}) => {
    const u = String(url);
    if (u.endsWith("/users/user/settings/update")) {
      const body = JSON.parse(init.body);
      writes.push(body[STATE]);
      Object.assign(settings, body);
      return new Response(JSON.stringify(settings));
    }
    if (u.endsWith("/users/user/settings")) return new Response(JSON.stringify(settings));
    if (u.endsWith("/valves/user")) return new Response(JSON.stringify(legacy));
    if (u.includes("/valves/user/")) throw new Error("the user valves are not written any more: " + u);
    if (u.endsWith("/api/v1/auths/")) return new Response(JSON.stringify({ id: "u1" }));
    return new Response("{}");
  };
  const element = () => ({
    style: {}, dataset: {}, children: [], setAttribute() {}, addEventListener() {}, appendChild(c) { return c; },
    prepend() {}, remove() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  });
  const sandbox = {
    navigator: {
      userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", onLine: true, maxTouchPoints: 5,
      serviceWorker: {
        register: async () => reg, ready: Promise.resolve(reg), getRegistration: async () => reg,
        addEventListener() {}, startMessages() {}, controller: null,
      },
    },
    PushManager: class {}, Notification: { permission: "granted", requestPermission: async () => "granted" },
    matchMedia: () => ({ matches: false }),
    localStorage: { getItem: (k) => local.get(k) ?? null, setItem: (k, v) => local.set(k, v), removeItem: (k) => local.delete(k) },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: {
      documentElement: {}, body: { appendChild: (c) => c, children: [] }, visibilityState: "visible", createElement: element,
      addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
    },
    history: { state: null, pushState() {}, replaceState() {} },
    location: { pathname: "/", search: "", hash: "", origin: "https://chat.example", href: "https://chat.example/" },
    fetch, caches: { has: async () => false, open: async () => ({ match: async () => undefined, put: async () => {}, delete: async () => false }) },
    MutationObserver: class { observe() {} disconnect() {} },
    WebSocket: class {}, addEventListener() {},
    setTimeout: (f, ms) => { const t = setTimeout(f, ms); t.unref(); return t; }, clearTimeout,
    URL, URLSearchParams, Headers, Request: class {}, Response, crypto: globalThis.crypto, TextEncoder,
    atob, queueMicrotask, console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(SRC.replaceAll("__VAPID_PUBLIC_KEY__", VAPID).replaceAll("__FUNCTION_ID__", "web_push"), sandbox);
  return { sandbox, writes, unsubscribed, get subscribes() { return subscribes; } };
}

const saved = (writes) => JSON.parse(writes.at(-1)?.subscriptions || "[]").map((s) => s.endpoint);

test("a subscription the push service retired is replaced, not saved again", async () => {
  const p = page({ subscriptions: "[]", gone: JSON.stringify([fingerprint(OLD)]) });
  assert.equal(await p.sandbox.ocPushReady, "saved");
  assert.deepEqual(p.unsubscribed, [OLD]);
  assert.deepEqual(saved(p.writes), [NEW]);
});

test("a live subscription is kept as it is", async () => {
  const p = page({ subscriptions: "[]", gone: JSON.stringify([fingerprint("https://fcm.example/send/other")]) });
  assert.equal(await p.sandbox.ocPushReady, "saved");
  assert.deepEqual(p.unsubscribed, []);
  assert.deepEqual(saved(p.writes), [OLD]);
});

test("at most one replacement per load: a new subscription also gone waits for the next load", async () => {
  const p = page({ subscriptions: "[]", gone: JSON.stringify([fingerprint(OLD), fingerprint(NEW)]) });
  assert.equal(await p.sandbox.ocPushReady, "gone");
  assert.equal(p.subscribes, 1);
  assert.deepEqual(p.unsubscribed, [OLD]);
  assert.deepEqual(saved(p.writes), [], "neither dead one saved");
});

test("the state of 0.6.0 (user valves, nothing under the new key yet) is read, and the write carries it over", async () => {
  const p = page(undefined, { subscriptions: "[]", mutes: "{\"c9\":0}", gone: JSON.stringify([fingerprint(OLD)]) });
  assert.equal(await p.sandbox.ocPushReady, "saved");
  assert.deepEqual(p.unsubscribed, [OLD], "the old valves' gone list is honoured");
  assert.deepEqual(saved(p.writes), [NEW]);
  assert.equal(p.writes.at(-1).mutes, "{\"c9\":0}", "a field only the old valves had is carried over");
});
