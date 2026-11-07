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
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "oc-e2e-inflight")
    e.source.postMessage({ type: "oc-e2e-inflight", list: [...ocE2eInflight.values()] });
});
