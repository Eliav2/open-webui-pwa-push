// e2e only (stack.sh puts it in front of the stack's copy of oc-sw.js; never in web/ or a deploy):
// each respondWith and waitUntil the worker has not finished, so a test that finds a new worker
// still waiting can ask the old one what it is busy with ("oc-e2e-inflight").
const ocE2eInflight = new Map();
let ocE2eNext = 0;
const ocE2eTrack = (what, p) => {
  const id = ++ocE2eNext;
  ocE2eInflight.set(id, Date.now() + " " + what);
  Promise.resolve(p).catch(() => {}).finally(() => ocE2eInflight.delete(id));
};
const ocE2eRespondWith = FetchEvent.prototype.respondWith;
FetchEvent.prototype.respondWith = function (p) {
  ocE2eTrack("respondWith " + this.request.method + " " + this.request.url, p);
  return ocE2eRespondWith.call(this, p);
};
const ocE2eWaitUntil = ExtendableEvent.prototype.waitUntil;
ExtendableEvent.prototype.waitUntil = function (p) {
  ocE2eTrack("waitUntil " + this.type + " " + (this.request ? this.request.url : ""), p);
  return ocE2eWaitUntil.call(this, p);
};
// A test that needs a reload to leave before the worker is active holds its install (a cache of that
// name, made by the test's page) until it says go.
const OC_E2E_HOLD_INSTALL = "oc-e2e-hold-install";
let ocE2eInstallGo = null;
self.addEventListener("install", (e) => e.waitUntil(caches.has(OC_E2E_HOLD_INSTALL).then((hold) =>
  hold && new Promise((go) => { ocE2eInstallGo = go; }))));
// A test that needs this worker busy holds one of its fetches (OC_E2E_HOLD_FETCH) until it says go.
// Registered before oc-sw.js's own: that one never sees the request.
const OC_E2E_HOLD_FETCH = "/oc-e2e-hold-fetch";
let ocE2eFetchGo = null;
// Or answers it at once and keeps the event open (waitUntil) instead (OC_E2E_HOLD_WAIT).
const OC_E2E_HOLD_WAIT = "/oc-e2e-hold-wait";
self.addEventListener("fetch", (e) => {
  const path = new URL(e.request.url).pathname;
  if (path !== OC_E2E_HOLD_FETCH && path !== OC_E2E_HOLD_WAIT) return;
  e.stopImmediatePropagation();
  const held = new Promise((go) => { ocE2eFetchGo = go; });
  if (path === OC_E2E_HOLD_WAIT) {
    e.waitUntil(held);
    e.respondWith(new Response("go"));
  } else e.respondWith(held.then(() => new Response("go")));
});
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "oc-e2e-install-go" && ocE2eInstallGo) ocE2eInstallGo();
  if (e.data && e.data.type === "oc-e2e-fetch-go" && ocE2eFetchGo) ocE2eFetchGo();
  if (e.data && e.data.type === "oc-e2e-inflight")
    e.source.postMessage({ type: "oc-e2e-inflight", list: [...ocE2eInflight.values()] });
  // A waiting worker asked to skip waiting again: does it take over with the page still open?
  if (e.data && e.data.type === "oc-e2e-skip") e.waitUntil(self.skipWaiting());
});
