// node --test web/oc-push-reply.test.js: a reply that could not be sent (the service worker opened
// the chat with ?oc-reply=unsent, its text in the drafts cache), on a chat the worker answered from
// its kept copy. The fresh copy comes before Open WebUI has drawn the message box: the page reloads
// for it, and the reload must take the reply along, or it is never put in the box.
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync(process.env.OC_PUSH || new URL("oc-push.js", import.meta.url), "utf8");
const CHAT = "12345678";
const KEPT_HEADER = "x-oc-kept", CHAT_FRESH = "oc-chat-fresh";   // oc-sw.js

function page() {
  const local = new Map([["token", "user-token"]]), session = new Map(), swListeners = {}, nav = [];
  const location = {
    pathname: "/c/" + CHAT, search: "?oc-reply=unsent", hash: "", origin: "https://chat.example",
    get href() { return this.origin + this.pathname + this.search + this.hash; },
    reload() { nav.push(["reload"]); }, replace(url) { nav.push(["replace", url]); },
  };
  const draft = new Response(JSON.stringify({ text: "Yes please", at: 1, id: "reply-1" }));
  const reg = {
    pushManager: { getSubscription: async () => null, subscribe: async () => { throw new Error("not in this test"); } },
    update: async () => {}, getNotifications: async () => [],
  };
  const element = () => ({
    style: {}, dataset: {}, children: [], setAttribute() {}, addEventListener() {}, appendChild(c) { return c; },
    prepend() {}, remove() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  });
  // The page's own fetch: the chat's read answered from the kept copy, the rest empty.
  const fetch = async (url) => new Response("{}", { status: 200, headers: String(url).endsWith("/api/v1/chats/" + CHAT) ? { [KEPT_HEADER]: "1" } : {} });
  const sandbox = {
    navigator: {
      userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", onLine: true, maxTouchPoints: 5,
      serviceWorker: {
        register: async () => reg, ready: Promise.resolve(reg), getRegistration: async () => reg,
        addEventListener: (type, fn) => { (swListeners[type] ||= []).push(fn); }, startMessages() {}, controller: null,
      },
    },
    PushManager: class {}, Notification: { permission: "default", requestPermission: async () => "default" },
    matchMedia: () => ({ matches: false }),
    localStorage: { getItem: (k) => local.get(k) ?? null, setItem: (k, v) => local.set(k, v), removeItem: (k) => local.delete(k) },
    sessionStorage: { getItem: (k) => session.get(k) ?? null, setItem: (k, v) => session.set(k, v), removeItem: (k) => session.delete(k) },
    document: {
      documentElement: {}, body: { appendChild: (c) => c, children: [] }, visibilityState: "visible", createElement: element,
      addEventListener() {}, getElementById: () => null,
      querySelector: () => null,   // Open WebUI has not drawn the message box yet
      querySelectorAll: () => [],
    },
    history: {
      state: null, pushState() {},
      replaceState(_s, _t, url) { const u = new URL(url, location.origin); location.pathname = u.pathname; location.search = u.search; },
    },
    location, fetch,
    caches: {
      has: async () => false,
      open: async (name) => ({ match: async () => (name === "oc-push-drafts" ? draft.clone() : undefined), put: async () => {}, delete: async () => false }),
    },
    MutationObserver: class { observe() {} disconnect() {} },
    WebSocket: class {}, addEventListener() {},
    // The box's own wait (15 s) must not keep the test process alive.
    setTimeout: (f, ms) => { const t = setTimeout(f, ms); t.unref(); return t; }, clearTimeout,
    URL, URLSearchParams, Headers, Request: class {}, Response, crypto, queueMicrotask, console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(SRC.replaceAll("__VAPID_PUBLIC_KEY__", "test-vapid-public-key").replaceAll("__FUNCTION_ID__", "web_push"), sandbox);
  const fresh = () => (swListeners.message || []).forEach((fn) => fn({ data: { type: CHAT_FRESH, path: "/api/v1/chats/" + CHAT } }));
  return { sandbox, location, nav, fresh };
}

const until = async (check) => {
  for (let i = 0; i < 1000 && !check(); i++) await new Promise((r) => setImmediate(r));
  assert.ok(check(), "never happened");
};

test("the fresh copy before the message box is drawn: the reload keeps ?oc-reply", async () => {
  const p = page();
  await p.sandbox.ocPushReady;
  await until(() => p.location.search === "");   // the page script took the reply off the URL
  await p.sandbox.fetch("/api/v1/chats/" + CHAT);   // Open WebUI's read: the kept copy
  p.fresh();   // the service worker: the network's copy is in
  assert.deepEqual(p.nav, [["replace", "/c/" + CHAT + "?oc-reply=unsent"]]);
});
