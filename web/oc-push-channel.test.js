// node --test web/oc-push-channel.test.js: oc-push.js on a channel page (ELI-548), run in a sandbox page.
// A channel on screen has been read: its notification on this device is closed (the tag is oc-sw.js's
// `channel-<id>`), as a chat's is. A channel is not a chat: nothing else of the chat page applies.
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const PUSH = process.env.OC_PUSH || new URL("oc-push.js", import.meta.url);

function page(pathname) {
  const listeners = {}, asked = [], closed = [], marked = [], workerMessages = [];
  const element = () => ({
    style: {}, dataset: {}, children: [], click() {}, setAttribute() {}, addEventListener() {}, appendChild(c) { return c; },
    prepend() {}, remove() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  });
  const reg = {
    pushManager: { getSubscription: async () => null }, update: async () => {},
    getNotifications: async ({ tag }) => { asked.push(tag); return [{ tag, timestamp: 1, close: () => closed.push(tag) }]; },
  };
  const sandbox = {
    navigator: {
      userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/140", onLine: true, connection: { addEventListener() {} },
      serviceWorker: {
        controller: null, register: async () => reg, ready: Promise.resolve(reg), getRegistration: async () => reg,
        addEventListener: (type, fn) => { (workerMessages[type] ||= []).push(fn); }, startMessages() {},
      },
    },
    PushManager: class {}, Notification: { permission: "default", requestPermission: async () => "default" },
    caches: { has: async () => false, open: async () => ({ match: async () => undefined, put: async (k) => { marked.push(k); }, delete: async () => false }) },
    document: {
      documentElement: {}, body: { appendChild: (c) => c, children: [] }, visibilityState: "visible", createElement: element,
      addEventListener() {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
    },
    matchMedia: () => ({ matches: false }),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    history: { state: null, pushState() {}, replaceState() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    MutationObserver: class { observe() {} disconnect() {} },
    WebSocket: class {},
    location: { pathname, search: "", hash: "", href: "https://chat.example" + pathname, origin: "https://chat.example", reload() {} },
    fetch: async () => ({ ok: true, status: 200 }),
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    setTimeout: (f, ms) => { const t = setTimeout(f, ms); t.unref(); return t; }, clearTimeout,
    URL, URLSearchParams, Headers, Request: class {}, Response, crypto: globalThis.crypto, TextEncoder, atob, queueMicrotask, console,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(fs.readFileSync(PUSH, "utf8").replaceAll("__VAPID_PUBLIC_KEY__", "").replaceAll("__FUNCTION_ID__", "web_push"), sandbox);
  const settle = () => new Promise((r) => setImmediate(r));
  return { sandbox, asked, closed, marked, workerMessages, settle };
}

test("a channel page closes that channel's notification, marked as this code's own close", async () => {
  const p = page("/channels/ch1");
  await p.sandbox.ocPushReady; await p.settle();
  assert.deepEqual(p.asked, ["channel-ch1"]);
  assert.deepEqual(p.closed, ["channel-ch1"]);
  assert.deepEqual(p.marked, ["/oc-push/closed/channel-ch1/1"]);
});

test("a chat page still closes the chat's notification only", async () => {
  const p = page("/c/abc");
  await p.sandbox.ocPushReady; await p.settle();
  assert.deepEqual(p.asked, ["chat-abc"]);
});

test("a page that is neither asks for no notification", async () => {
  const p = page("/");
  await p.sandbox.ocPushReady; await p.settle();
  assert.deepEqual(p.asked, []);
});

test("a tap that opens a channel in the page opens the link and reports the channel, not a mangled chat id", async () => {
  const p = page("/");
  await p.sandbox.ocPushReady; await p.settle();
  const acks = [];
  for (const fn of p.workerMessages.message) fn({ data: { type: "oc-open-chat", id: "1", url: "/channels/ch1" }, source: { postMessage: (m) => acks.push(m) } });
  assert.equal(acks[0].type, "oc-open-chat-ack");
  assert.equal(p.sandbox.ocPushOpened, "ch1");
});
