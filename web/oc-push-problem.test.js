// node --test web/oc-push-problem.test.js: the function could not send this user's notifications
// (openclaw_web_push.py writes why into the user's push_problem valve, e.g. no usable VAPID subject).
// The page says so where the user sees it, and the line stays until it is tapped.
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync(process.env.OC_PUSH || new URL("oc-push.js", import.meta.url), "utf8");
const CHAT = "12345678";
const PROBLEM = "Notifications can't be sent yet: the admin must set VAPID_SUBJECT.";
const DEVICE = JSON.stringify([{ endpoint: "https://push.example/1", keys: {} }]);

function page(userValves) {
  const local = new Map([["token", "user-token"]]), session = new Map(), shown = [], timers = [];
  const location = {
    pathname: "/c/" + CHAT, search: "", hash: "", origin: "https://chat.example",
    get href() { return this.origin + this.pathname + this.search + this.hash; }, reload() {}, replace() {},
  };
  const reg = {
    pushManager: { getSubscription: async () => null, subscribe: async () => { throw new Error("not in this test"); } },
    update: async () => {}, getNotifications: async () => [],
  };
  const element = () => {
    const el = {
      style: {}, dataset: {}, children: [], setAttribute() {}, addEventListener() {}, appendChild(c) { return c; },
      prepend() {}, remove() { el.removed = true; }, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
    };
    return el;
  };
  const fetch = async (url) => new Response(JSON.stringify(String(url).includes("/valves/user") ? userValves : {}), { status: 200 });
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
    sessionStorage: { getItem: (k) => session.get(k) ?? null, setItem: (k, v) => session.set(k, v), removeItem: (k) => session.delete(k) },
    document: {
      documentElement: {}, body: { appendChild: (c) => { shown.push(c); return c; }, children: [] }, visibilityState: "visible",
      createElement: element, addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
    },
    history: { state: null, pushState() {}, replaceState() {} },
    location, fetch,
    caches: { has: async () => false, open: async () => ({ match: async () => undefined, put: async () => {}, delete: async () => false }) },
    MutationObserver: class { observe() {} disconnect() {} },
    WebSocket: class {}, addEventListener() {},
    // Timers are recorded, never run: a line that goes away on its own has one.
    setTimeout: (f, ms) => { timers.push(ms); return timers.length; }, clearTimeout() {},
    URL, URLSearchParams, Headers, Request: class {}, Response, crypto, queueMicrotask, console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(SRC.replaceAll("__VAPID_PUBLIC_KEY__", "test-vapid-public-key").replaceAll("__FUNCTION_ID__", "web_push"), sandbox);
  const notices = () => shown.filter((el) => el.id === "oc-push-notice");
  return { sandbox, notices };
}

const settle = async () => { for (let i = 0; i < 200; i++) await new Promise((r) => setImmediate(r)); };

test("nothing can be sent: the page says why, and the line stays until tapped", async () => {
  const p = page({ subscriptions: DEVICE, push_problem: PROBLEM });
  await p.sandbox.ocPushReady;
  await settle();
  const [n, ...more] = p.notices();
  assert.ok(n, "no notice");
  assert.equal(more.length, 0);
  assert.equal(n.textContent, PROBLEM);
  await settle();
  assert.ok(!n.removed, "went away on its own");
  n.onclick();
  assert.ok(n.removed);
});

test("no problem, or no device on this account: no line", async () => {
  for (const v of [{ subscriptions: DEVICE }, { subscriptions: DEVICE, push_problem: "" }, { push_problem: PROBLEM }]) {
    const p = page(v);
    await p.sandbox.ocPushReady;
    await settle();
    assert.deepEqual(p.notices(), [], JSON.stringify(v));
  }
});
