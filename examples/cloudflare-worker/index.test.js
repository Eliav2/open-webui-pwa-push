// node --test examples/cloudflare-worker/index.test.js: the Worker example, with the two
// scripts it imports as text (wrangler's Text rule) given as strings.
import test from "node:test";
import assert from "node:assert";
import fs from "node:fs";

const SRC = fs.readFileSync(new URL("src/index.js", import.meta.url), "utf8");
const SW = "const OFFLINE = '__OC_OFFLINE__', CHATS = '__OC_CHAT_CACHE__';";
const PAGE = 'const VAPID = "__VAPID_PUBLIC_KEY__", FN = "__FUNCTION_ID__";';
const OWN_LOADER = "/* Open WebUI's own loader.js */";

async function worker() {
  const code = SRC.replace(/^import SW from .*$/m, `const SW = ${JSON.stringify(SW)};`)
    .replace(/^import PAGE from .*$/m, `const PAGE = ${JSON.stringify(PAGE)};`);
  return (await import("data:text/javascript," + encodeURIComponent(code))).default;
}

async function get(path, env) {
  const origin = globalThis.fetch;
  globalThis.fetch = async () => new Response(OWN_LOADER);   // Open WebUI behind the Worker
  try {
    const res = await (await worker()).fetch(new Request("https://chat.example" + path), env);
    return { status: res.status, type: res.headers.get("content-type"), body: await res.text() };
  } finally {
    globalThis.fetch = origin;
  }
}

test("push configured: the page script gets the key and the function id", async () => {
  const r = await get("/static/loader.js", { VAPID_PUBLIC_KEY: "BKEY", FUNCTION_ID: "web_push" });
  assert.equal(r.status, 200);
  assert.ok(r.body.startsWith(OWN_LOADER));
  assert.ok(r.body.endsWith('const VAPID = "BKEY", FN = "web_push";'), r.body);
});

test("SW-only (no VAPID_PUBLIC_KEY, no FUNCTION_ID): both scripts served, the push values empty, never 'undefined'", async () => {
  const page = await get("/static/loader.js", { OFFLINE: "1", CHAT_CACHE: "1" });
  assert.equal(page.status, 200);
  assert.match(page.type, /javascript/);
  assert.ok(page.body.endsWith('const VAPID = "", FN = "";'), page.body);
  assert.ok(!page.body.includes("undefined"), page.body);
  const sw = await get("/oc-sw.js", { OFFLINE: "1", CHAT_CACHE: "1" });
  assert.equal(sw.status, 200);
  assert.equal(sw.body, "const OFFLINE = '1', CHATS = '1';");
});
