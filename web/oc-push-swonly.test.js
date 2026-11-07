// node --test web/oc-push-swonly.test.js: oc-push.js with and without push (its two
// placeholders filled in, left empty, or not replaced), run in a sandbox page of a
// signed-in user on Android Chrome that has not been asked for notifications yet.
// SW-only mode: the service worker is registered (offline open, kept chats), and
// nothing of push shows or asks the server.
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync(process.env.OC_PUSH || new URL("oc-push.js", import.meta.url), "utf8");
const KEY = "test-vapid-public-key";   // any given value: the page script only checks that it is there

function element(tag) {
  const el = {
    tagName: tag, id: "", style: {}, dataset: {}, children: [], textContent: "", className: "",
    setAttribute() {}, addEventListener() {}, appendChild(c) { el.children.push(c); return c; }, prepend() {},
    remove() { body.children = body.children.filter((c) => c !== el); }, querySelector: () => null,
    querySelectorAll: () => [], closest: () => null, contains: () => false, getBoundingClientRect: () => ({}),
  };
  return el;
}
const body = { children: [] };

function page(vapid, fn) {
  body.children = [];
  const local = new Map([["token", "user-token"]]), session = new Map(), calls = [], registered = [];
  const reg = {
    pushManager: { getSubscription: async () => null, subscribe: async () => { throw new Error("not in this test"); } },
    update: async () => {}, getNotifications: async () => [],
  };
  const sandbox = {
    navigator: {
      userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", onLine: true, maxTouchPoints: 5,
      serviceWorker: {
        register: async (url) => { registered.push(url); return reg; }, ready: Promise.resolve(reg),
        getRegistration: async () => reg, addEventListener() {}, startMessages() {}, controller: null,
      },
    },
    PushManager: class {}, Notification: { permission: "default", requestPermission: async () => "default" },
    matchMedia: () => ({ matches: false }),
    localStorage: { getItem: (k) => local.get(k) ?? null, setItem: (k, v) => local.set(k, v), removeItem: (k) => local.delete(k) },
    sessionStorage: { getItem: (k) => session.get(k) ?? null, setItem: (k, v) => session.set(k, v), removeItem: (k) => session.delete(k) },
    document: {
      documentElement: {}, body: Object.assign(body, { appendChild: (c) => { body.children.push(c); return c; } }),
      visibilityState: "visible", createElement: element, addEventListener() {},
      getElementById: (id) => body.children.find((c) => c.id === id) ?? null,
      querySelector: () => null, querySelectorAll: () => [],
    },
    history: { state: null, pushState() {}, replaceState() {} },
    location: { pathname: "/", search: "", hash: "", href: "https://chat.example/", origin: "https://chat.example", reload() {} },
    fetch: async (url) => { calls.push(String(url)); return { ok: true, json: async () => ({}), headers: new Headers() }; },
    caches: { has: async () => false, open: async () => ({ match: async () => undefined, put: async () => {}, delete: async () => false }) },
    MutationObserver: class { observe() {} disconnect() {} },
    WebSocket: class {}, addEventListener() {},
    URL, URLSearchParams, Headers, Request: class {}, Response, crypto, setTimeout, clearTimeout, queueMicrotask, console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  const code = SRC.replaceAll("__VAPID_PUBLIC_KEY__", vapid).replaceAll("__FUNCTION_ID__", fn);   // as the proxy does
  vm.runInNewContext(code, sandbox);
  return { sandbox, calls, registered, offer: () => sandbox.document.getElementById("oc-push-offer") };
}

test("push configured: the service worker, and the offer while the browser has not asked", async () => {
  const p = page(KEY, "web_push");
  assert.equal(await p.sandbox.ocPushReady, "no-permission");
  assert.deepEqual(p.registered, ["/oc-sw.js"]);
  assert.ok(p.offer(), "no offer");
});

for (const [name, vapid, fn] of [["left empty by the proxy", "", ""], ["not replaced", "__VAPID_PUBLIC_KEY__", "__FUNCTION_ID__"],
  ["only the key", KEY, ""], ["only the function id", "", "web_push"]]) {
  test(`SW-only (${name}): the service worker is registered, no offer, no call to the function`, async () => {
    const p = page(vapid, fn);
    assert.equal(await p.sandbox.ocPushReady, "push-off");
    assert.deepEqual(p.registered, ["/oc-sw.js"]);
    assert.equal(p.offer(), null, "the offer shows, and enabling would fail");
    await new Promise((r) => setImmediate(r));
    assert.deepEqual(p.calls.filter((u) => u.includes("/api/v1/functions/")), []);
  });
}
