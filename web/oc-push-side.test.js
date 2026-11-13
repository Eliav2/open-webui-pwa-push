// node --test web/oc-push-side.test.js: a side chat's notification opens its parent with ?oc-side=<side chat>.
// On a phone Open WebUI's panel is a sheet, closed on every load: the page script opens it (the navbar's Tabs
// button, found by structure: its label is translated) and selects the side chat's tab (the tab's element id is
// Open WebUI's `chat-panel-<tab id>`, the tab found in the parent's saved view). On a screen that shows the panel
// it only selects the tab.
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";
import vm from "node:vm";

const SRC = fs.readFileSync(process.env.OC_PUSH || new URL("oc-push.js", import.meta.url), "utf8");
const PARENT = "parent01", SIDE = "side0001", TAB = "tab-side-1", MAIN_TAB = "main-chat";

// `panel`: "sheet" (a phone: the tab's element is not shown until the Tabs button is pressed) or "shown".
// `tabs`: the parent's saved view.
function page({ panel, tabs, search = "?oc-side=" + SIDE, activeId = TAB }) {
  const local = new Map([["token", "user-token"]]), session = new Map(), swListeners = {}, observers = new Set();
  const location = {
    pathname: "/c/" + PARENT, search, hash: "", origin: "https://chat.example",
    get href() { return this.origin + this.pathname + this.search + this.hash; },
    reload() {}, replace() {},
  };
  const reg = {
    pushManager: { getSubscription: async () => null, subscribe: async () => { throw new Error("not in this test"); } },
    update: async () => {}, getNotifications: async () => [],
  };
  const element = () => ({
    style: {}, dataset: {}, children: [], setAttribute() {}, addEventListener() {}, appendChild(c) { return c; },
    prepend() {}, remove() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  });
  const changed = () => observers.forEach((cb) => cb([]));
  const state = { sheetOpen: panel === "shown", clicks: [], selected: activeId };
  const toggle = {
    getAttribute: (n) => (n === "aria-expanded" ? String(state.sheetOpen) : null),
    click() { state.clicks.push("tabs-button"); state.sheetOpen = true; changed(); },
  };
  const tabEl = {
    getAttribute: (n) => (n === "aria-selected" ? String(state.selected === TAB) : null),
    getClientRects: () => (state.sheetOpen ? [{}] : []),
    click() { state.clicks.push("side-tab"); state.selected = TAB; changed(); },
  };
  const views = [];
  const fetch = async (url) => {
    if (String(url).endsWith("/api/v1/chats/" + PARENT + "/view")) {
      views.push(String(url));
      return new Response(JSON.stringify({ panels: { tabs, activeId } }), { status: 200 });
    }
    return new Response("{}", { status: 200 });
  };
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
      addEventListener() {},
      getElementById: (id) => (id === "chat-panel-" + TAB ? tabEl : null),
      // The Tabs button is the navbar's one button that has aria-expanded and no menu behind it.
      querySelector: (sel) => (sel.includes("aria-expanded") && sel.includes("nav") ? toggle : null),
      querySelectorAll: () => [],
    },
    history: {
      state: null, pushState() {},
      replaceState(_s, _t, url) { const u = new URL(url, location.origin); location.pathname = u.pathname; location.search = u.search; },
    },
    location, fetch,
    caches: { has: async () => false, open: async () => ({ match: async () => undefined, put: async () => {}, delete: async () => false }) },
    MutationObserver: class { constructor(cb) { this.cb = cb; } observe() { observers.add(this.cb); } disconnect() { observers.delete(this.cb); } },
    WebSocket: class {}, addEventListener() {},
    setTimeout: (f, ms) => { const t = setTimeout(f, ms); t.unref(); return t; }, clearTimeout,
    URL, URLSearchParams, Headers, Request: class {}, Response, crypto, queueMicrotask, console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(SRC.replaceAll("__VAPID_PUBLIC_KEY__", "test-vapid-public-key").replaceAll("__FUNCTION_ID__", "web_push"), sandbox);
  return { sandbox, location, state, views };
}

const until = async (check) => {
  for (let i = 0; i < 1000 && !check(); i++) await new Promise((r) => setImmediate(r));
  assert.ok(check(), "never happened");
};
const settle = async () => { for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r)); };

const SIDE_TABS = [{ id: MAIN_TAB, kind: "chat" }, { id: TAB, kind: "side", chatId: SIDE }];

test("a phone: the sheet is opened on the side chat's tab and the parameter is taken off the URL", async () => {
  const p = page({ panel: "sheet", tabs: SIDE_TABS, activeId: MAIN_TAB });
  await p.sandbox.ocPushReady;
  await until(() => p.state.selected === TAB);
  assert.deepEqual(p.state.clicks, ["tabs-button", "side-tab"]);
  assert.equal(p.location.search, "");
});

test("a phone, the side chat already the active tab: the sheet opens, the tab is not pressed again", async () => {
  const p = page({ panel: "sheet", tabs: SIDE_TABS });
  await p.sandbox.ocPushReady;
  await until(() => p.state.sheetOpen);
  await settle();
  assert.deepEqual(p.state.clicks, ["tabs-button"]);
});

test("a screen that shows the panel: only the tab is selected, the Tabs button is left alone", async () => {
  const p = page({ panel: "shown", tabs: SIDE_TABS, activeId: MAIN_TAB });
  await p.sandbox.ocPushReady;
  await until(() => p.state.selected === TAB);
  assert.deepEqual(p.state.clicks, ["side-tab"]);
});

test("no side tab for that chat in the saved view: nothing is pressed, the parameter still goes", async () => {
  const p = page({ panel: "sheet", tabs: [{ id: MAIN_TAB, kind: "chat" }] });
  await p.sandbox.ocPushReady;
  await until(() => p.location.search === "");
  await settle();
  assert.deepEqual(p.state.clicks, []);
});

test("a chat opened with no ?oc-side reads no view and presses nothing", async () => {
  const p = page({ panel: "sheet", tabs: SIDE_TABS, search: "" });
  await p.sandbox.ocPushReady;
  await settle();
  assert.deepEqual(p.views, []);
  assert.deepEqual(p.state.clicks, []);
});
