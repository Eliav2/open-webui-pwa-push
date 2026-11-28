"""Web Push end to end: real Google Chrome, the real push service (FCM), the
Cloudflare Worker example under `wrangler dev`, a throwaway Open WebUI.

    e2e/stack.sh up <run_dir> <openclaw_pipe.py build>
    E2E_RUN=<run_dir> WEB_PUSH_E2E=1 xvfb-run -a uv run --with playwright==1.64.0 --with pytest pytest -q e2e/test_web_push_e2e.py
    e2e/stack.sh down <run_dir>

Push needs Google Chrome (Chromium builds have no push service), a persistent
profile and background networking, so this runs headed under Xvfb. Every wait
is on state: a service worker decision (`oc-push` message), a notification,
a saved subscription. None waits on time.
"""
import base64
import collections
import hashlib
import http.server
import json
import os
import re
import subprocess
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

# Runs only against a stack this machine started (stack.sh up); the unit job skips it.
if not os.environ.get("WEB_PUSH_E2E"):
    pytest.skip("set WEB_PUSH_E2E=1 with e2e/stack.sh up", allow_module_level=True)
from playwright.sync_api import sync_playwright  # noqa: E402

OWUI = os.environ.get("E2E_OWUI", "http://127.0.0.1:18780")
BASE = os.environ.get("E2E_BASE", "http://localhost:18787")   # the Worker
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHROME = os.environ.get("E2E_CHROME", "/usr/bin/google-chrome")
WAIT_MS = 60000  # a safety net for each state wait, never expected to expire
DIAG_LOG_S = 300    # push_diagnostics: the function's log lines of the last 5 minutes
DIAG_CHARS = 6000   # and at most this much of each chrome:// page
FINGERPRINT_CHARS = 32   # openclaw_web_push.py's: the same fingerprints as its `gone` valve
CHAT_LOAD_DELAY_MS = 200  # chat F arrives just after the reply is in the box (inside Open WebUI's 500 ms draft-save debounce)
MENU_DRAWN_WAIT_MS = 500  # a few frames: the script adds its item on the frame after the menu appears
DRAFT_RESTORE_WAIT_MS = 4000  # Open WebUI has loaded the chat and handled its own draft by then
SECRETS = ("STUB-SECRET-REPLY", "PIPE-SECRET-REPLY")


def api(path, body=None, token=None, method=None):
    req = urllib.request.Request(OWUI + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or "null")


@pytest.fixture(scope="module")
def owui():
    # The stack's first user is its admin; a later run signs in as it.
    cred = {"email": "admin@e2e.example.com", "password": "e2e-pass-1"}
    try:
        token = api("/api/v1/auths/signin", cred)["token"]
    except urllib.error.HTTPError:
        token = api("/api/v1/auths/signup", {"name": "E2E", **cred})["token"]
    # The admin's "What's New" dialog would cover the page: mark this version seen.
    api("/api/v1/users/user/settings/update", {"ui": {"showChangelog": False}}, token)
    # Open WebUI keeps sign-up in its saved config (the env var is only the first default):
    # on, with new accounts as plain users, for the sign-up test.
    cfg = api("/api/v1/auths/admin/config", token=token)
    if not cfg["ENABLE_SIGNUP"] or cfg["DEFAULT_USER_ROLE"] != "user":
        api("/api/v1/auths/admin/config", {**cfg, "ENABLE_SIGNUP": True, "DEFAULT_USER_ROLE": "user"}, token)
    have = {f["id"] for f in api("/api/v1/functions/", token=token)}
    for fid, name, path in (("web_push", "Web Push", os.path.join(ROOT, "openclaw_web_push.py")),
                            ("stub", "Stub", os.path.join(HERE, "stub_pipe.py")),
                            ("slow", "Slow", os.path.join(HERE, "slow_pipe.py")),
                            ("openclaw_gateway", "OpenClaw", os.path.join(HERE, "fake_openclaw_pipe.py"))):
        form = {"id": fid, "name": name, "content": open(path).read(), "meta": {"description": name}}
        api(f"/api/v1/functions/id/{fid}/update" if fid in have else "/api/v1/functions/create", form, token)
        if not api(f"/api/v1/functions/id/{fid}", token=token)["is_active"]:
            api(f"/api/v1/functions/id/{fid}/toggle", {}, token)
    api("/api/v1/functions/id/web_push/valves/update",
        {"VAPID_PRIVATE_KEY_FILE": "/mnt/keys/vapid_private.pem", "VAPID_SUBJECT": "mailto:e2e@example.com"}, token)
    return token


def new_chat(token, model, title):
    """A saved chat with a user message and the assistant message to answer it."""
    u, a = str(uuid.uuid4()), str(uuid.uuid4())
    msgs = {u: {"id": u, "role": "user", "content": "hi", "parentId": None, "childrenIds": [a]},
            a: {"id": a, "role": "assistant", "content": "", "parentId": u, "childrenIds": [], "model": model}}
    chat = api("/api/v1/chats/new", {"chat": {"title": title, "models": [model],
                                              "history": {"messages": msgs, "currentId": a},
                                              "messages": list(msgs.values())}}, token)
    return chat["id"], a


def reply(token, model, chat_id, message_id):
    """The chat request the page sends; Open WebUI answers in a background task."""
    return api("/api/chat/completions", {"model": model, "messages": [{"role": "user", "content": "hi"}],
                                         "chat_id": chat_id, "id": message_id, "session_id": "e2e",
                                         "stream": True}, token)


VALVES_READ = "/api/v1/functions/id/web_push/valves/user"   # where 0.6.0 kept the state: only read now
SETTINGS = "/api/v1/users/user/settings"
STATE_KEY = "web_push:web_push"   # the function's state in the user's settings (ELI-442)


def state(token):
    """The function's state for the user, as the function and the page read it: its key in the user's
    settings over what 0.6.0 kept in the user valves (a fresh Open WebUI has none of those)."""
    return {**(api(VALVES_READ, token=token) or {}), **(api(SETTINGS, token=token).get(STATE_KEY) or {})}


def put_state(token, change):
    """Change some fields of the state; the rest of the user's settings is not sent."""
    api(SETTINGS + "/update", {STATE_KEY: {**state(token), **change}}, token)


def subscriptions(token, page):
    """This browser's entries in the user's subscription list."""
    device = page.evaluate("() => localStorage.getItem('ocPushDevice')")
    return [s for s in json.loads(state(token).get("subscriptions") or "[]") if s.get("device") == device]


DECISIONS = """(() => { window.ocPushSeen = []; navigator.serviceWorker.addEventListener('message',
  (e) => { if (e.data && e.data.type === 'oc-push') window.ocPushSeen.push(e.data); });
  navigator.serviceWorker.startMessages(); })();"""
NOTES = "async () => (await (await navigator.serviceWorker.ready).getNotifications()).map(n => ({title: n.title, body: n.body, tag: n.tag, url: n.data && n.data.url, icon: n.icon, badge: n.badge}))"
CLEAR = "async () => (await (await navigator.serviceWorker.ready).getNotifications()).forEach(n => n.close())"


# A push to a just-saved subscription can come NEW_TOKEN_SETTLE_S late: FCM refuses a brand-new token
# for a few seconds and the function sends that notification again once the token is this old.
NEW_TOKEN_SETTLE_S = int(re.search(r"^NEW_TOKEN_SETTLE_S = (\d+)$", open(os.path.join(ROOT, "openclaw_web_push.py")).read(),
                                   re.M).group(1))
PUSH_DELIVERY_S = 30   # FCM to Chrome once it has taken the token: a safety net, never expected to expire
PUSH_WAIT_MS = (NEW_TOKEN_SETTLE_S + PUSH_DELIVERY_S) * 1000


def seen_for(page, chat_id, n=1):
    try:
        page.wait_for_function("([c, n]) => (window.ocPushSeen || []).filter(d => d.payload.chat_id === c).length >= n",
                               arg=[chat_id, n], timeout=PUSH_WAIT_MS)
    except Exception:
        print(push_diagnostics(page))
        raise
    return page.evaluate("(c) => window.ocPushSeen.filter(d => d.payload.chat_id === c)", chat_id)


def fingerprint(endpoint):
    """A push endpoint, short and comparable, without the token itself in the output."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:FINGERPRINT_CHARS] if endpoint else "none"


def push_diagnostics(page):
    """A push that never came: Chrome's own view of push (its registrations and their log, what it
    received) and of the service workers, with this page's subscription, for the failure's output."""
    endpoint = page.evaluate(ENDPOINT)
    st = state(page.evaluate("() => localStorage.getItem('token')"))
    saved = json.loads(st.get("subscriptions") or "[]")
    out = ["--- push diagnostics", "page subscription: " + fingerprint(endpoint),
           "saved subscriptions: " + ", ".join(fingerprint(x["endpoint"]) for x in saved),
           "gone (the function's 404/410s): " + ", ".join(json.loads(st.get("gone") or "[]")),
        "function log: " + "\n".join(l for l in owui_log(time.time() - DIAG_LOG_S).split("\n") if "web push" in l)]
    for url in ("chrome://gcm-internals", "chrome://serviceworker-internals"):
        tab = page.context.new_page()
        try:
            tab.goto(url)
            tab.wait_for_function("() => document.body.innerText.length > 0", timeout=WAIT_MS)
            out += ["--- " + url, tab.evaluate("() => document.body.innerText")[:DIAG_CHARS]]
        except Exception as e:
            out.append(f"--- {url}: {e}")
        finally:
            tab.close()
    return "\n".join(out)


def behind(ctx, page):
    """Another tab in front, so Open WebUI's page loses focus, as on a phone's home screen. A real
    tab through CDP, with Playwright's focus emulation off (it keeps every page focused). Call the result again to
    put it in front again. Each CDP session is detached once used: one left attached to the page
    undoes ctx.set_offline on its next load (navigator.onLine true, so no offline pill)."""
    cdp = ctx.new_cdp_session(page)
    tab = cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
    cdp.detach()
    def front():
        s = ctx.new_cdp_session(page)
        try:
            s.send("Emulation.setFocusEmulationEnabled", {"enabled": False})   # bring_to_front turns it on again
            s.send("Target.activateTarget", {"targetId": tab})
            page.wait_for_function("() => !document.hasFocus()", timeout=WAIT_MS)
        finally:
            s.detach()
    front()
    return front


STATE_POLL_S = 0.3   # a poll of a state the page holds, never a wait for time


def settled(page, check, arg=None):
    """Waits until an async check in the page is true. Playwright's wait_for_function does not
    wait for a Promise (a Promise is truthy), so a check on caches is polled here instead."""
    end = time.time() + WAIT_MS / 1000
    while time.time() < end:
        if page.evaluate(check, arg):
            return
        time.sleep(STATE_POLL_S)
    print(sw_diagnostics(page))
    raise AssertionError("never settled: " + check[:120])


def controlled(page):
    """Waits until the service worker controls the page (its first load ran before it did). The
    worker claims the pages open when it activates (oc-sw.js clients.claim()); a document that
    began loading before then and came after the claim (a reload of a page whose app failed to
    load) is controlled only from its next navigation, as the service worker lifecycle defines."""
    try:
        settled(page, "() => navigator.serviceWorker.getRegistration('/').then((r) => !!(r && r.active))")
        if not page.evaluate("() => !!navigator.serviceWorker.controller"):
            note("controlled", "an active worker, this document not claimed: its next navigation")
            page.reload()
        page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=WAIT_MS)
    except Exception:
        print(sw_diagnostics(page))
        raise


SW_STATE = """async () => { const r = await navigator.serviceWorker.getRegistration('/');
  const w = (x) => x && {state: x.state, url: x.scriptURL};
  return JSON.stringify({controller: w(navigator.serviceWorker.controller), active: w(r?.active),
    waiting: w(r?.waiting), installing: w(r?.installing), caches: await caches.keys(),
    registrations: (await navigator.serviceWorker.getRegistrations()).length,
    ocPushState: window.ocPushState ?? null, visible: document.visibilityState, url: location.href}); }"""


def sw_diagnostics(page):
    """A service worker that never settled (or a page it never updated): the page's view of its
    registration and caches, and Chrome's (each version's status, its clients, running or not)."""
    out = ["--- service worker diagnostics"]
    try:
        out.append("page: " + page.evaluate(SW_STATE))
    except Exception as e:
        out.append(f"page: {e}")
    tab = page.context.new_page()
    try:
        tab.goto("chrome://serviceworker-internals")
        tab.wait_for_function("() => document.body.innerText.length > 0", timeout=WAIT_MS)
        out.append(tab.evaluate("() => document.body.innerText")[:DIAG_CHARS])
    except Exception as e:
        out.append(f"serviceworker-internals: {e}")
    finally:
        tab.close()
    return "\n".join(out)


def ready(page):
    page.wait_for_function("() => window.ocPushState !== undefined", timeout=WAIT_MS)
    return page.evaluate("() => window.ocPushState")


# chrome://gcm-internals is Chrome's own view of its push connection. A fresh profile checks in with
# Google, then its first connection is often refused (LOGIN_FAILURE) and retried after a backoff of about
# 20 s, so a push sent right after `subscribe()` resolves can find no live connection. The page shows the
# connection state, the apps registered with it and a newest-first connection log; a push can arrive once
# the state is CONNECTED, this origin's app is registered, and the newest log entry is a success (the log
# only holds events from after this tab opened: an empty one means the connection was already up).
GCM_LIVE = r"""(origin) => {
  const t = document.body.innerText;
  const log = (t.split(/Connection Log\s+Time\s+Event\s+Details\s+/)[1] || '').split('\n')[0];
  return /Connection State\s+CONNECTED/.test(t) && ((t.match(/Registered App Ids\t([^\n]*)/) || [])[1] || '').includes('wp:' + origin + '/') && (log === '' || log.startsWith('Registration Log') || /Connection succeeded/.test(log));
}"""


def push_connection_live(ctx, origin):
    """Waits until Chrome can receive a push for `origin`. Call it after every new subscription,
    before the server sends: FCM keeps a message for a registered device, but only a connected one
    receives it, and the first connection of a new profile can take a backoff to come up."""
    tab = ctx.new_page()
    try:
        tab.goto("chrome://gcm-internals")
        try:
            settled(tab, GCM_LIVE, origin)
        except AssertionError:
            print("Chrome's push connection never came up:", tab.evaluate("() => document.body.innerText")[:2500])
            raise
    finally:
        tab.close()


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as p:
        yield p


TRACE_MAX = 300   # the page events a failed test reports (the last ones)
TRACE = collections.deque(maxlen=TRACE_MAX)
PENDING = {}      # the page's requests with no answer yet -> their url
TRACED = []       # the pages traced (the module's browser page stays across tests)
SW_PENDING = {}   # the service workers' own requests with no answer yet -> when and what


def note(kind, detail):
    TRACE.append(f"{time.time():.3f} {kind} {detail}")


def asked(r):
    return f"{time.time():.3f} {r.method} {r.url}"


def trace_workers(ctx):
    """The service workers' own requests (to the network), beside the pages'."""
    def started(r):
        if r.service_worker is not None:
            SW_PENDING[r] = asked(r)
    def failed(r):
        if SW_PENDING.pop(r, None) is not None:
            note("sw.failed", f"{r.method} {r.url} {r.failure}")
    ctx.on("request", started)
    ctx.on("requestfinished", lambda r: SW_PENDING.pop(r, None))
    ctx.on("requestfailed", failed)


def trace(page):
    """A page's loads, failed requests and errors, for the report of a test that fails."""
    def navigated(frame):
        if frame == page.main_frame:
            note("nav", frame.url)
    def failed(r):
        PENDING.pop(r, None)
        note("failed", f"{r.method} {r.url} {r.failure}")
    def answered(res):
        # A chat's reads: which copy each got (the kept one is marked), and from whom.
        if TRACED_READS.search(urllib.parse.urlsplit(res.url).path):
            h = res.headers
            note("answer", f"{res.status} {res.request.method} {res.url} sw={res.from_service_worker}"
                           f" {TRACE_HEADERS}={[h.get(k) for k in TRACE_HEADERS.split(',')]}")
    def console(m):
        if m.type in ("error", "warning"):
            note("console." + m.type, m.text[:TRACE_TEXT])
    TRACED.append(page)
    page.on("framenavigated", navigated)
    page.on("domcontentloaded", lambda p: note("domcontentloaded", p.url))
    page.on("load", lambda p: note("load", p.url))
    page.on("request", lambda r: PENDING.__setitem__(r, asked(r)))
    page.on("requestfinished", lambda r: PENDING.pop(r, None))
    page.on("requestfailed", failed)
    page.on("console", console)
    page.on("pageerror", lambda e: note("pageerror", str(e)[:TRACE_TEXT]))
    page.on("response", answered)


TRACE_TEXT = 300   # characters kept of a message
TRACED_READS = re.compile(r"^/api/(v1/chats/[\w-]+|tasks/chat/[\w-]+)$")   # a chat's own reads
TRACE_HEADERS = "x-oc-kept,x-oc-open"   # oc-sw.js KEPT_HEADER, OPEN_READ


@pytest.fixture(autouse=True)
def page_trace(request):
    """On a failure: what the pages did last, what they still wait for, and their service worker."""
    TRACE.clear()
    PENDING.clear()
    SW_PENDING.clear()
    TRACED[:] = [p for p in TRACED if not p.is_closed()]
    yield
    rep = getattr(request.node, "rep_call", None)
    if rep is None or not rep.failed:
        return
    print("--- page trace (last events)")
    print("\n".join(TRACE))
    print("--- requests still without an answer")
    print("\n".join(PENDING.values()) or "(none)")
    print("--- the service workers' requests still without an answer")
    print("\n".join(SW_PENDING.values()) or "(none)")
    for page in list(TRACED):   # (the diagnostics' own tab joins the list)
        if not page.is_closed():
            print(sw_diagnostics(page))


def chrome(pw, profile=None):
    """Google Chrome with its own new profile (or `profile`, to open it again), as a phone-sized window."""
    ctx = pw.chromium.launch_persistent_context(
        profile or tempfile.mkdtemp(prefix="webpush-e2e-"), executable_path=CHROME, headless=False,
        args=["--no-sandbox"], viewport={"width": 390, "height": 844},
        ignore_default_args=["--disable-background-networking", "--disable-component-update", "--disable-sync"])
    ctx.set_default_navigation_timeout(WAIT_MS)   # a load waits as long as any other state here
    ctx.add_init_script(PAGE_READS)
    for page in ctx.pages:
        trace(page)
    ctx.on("page", trace)
    trace_workers(ctx)
    return ctx


@pytest.fixture(scope="module")
def browser(owui, pw):
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    ctx.add_init_script(DECISIONS)
    # Open WebUI's sign-in sets this cookie in a browser; the service worker's
    # actions authenticate with it (same origin, no token of their own).
    ctx.add_cookies([{"name": "token", "value": owui, "url": BASE}])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    yield ctx, page
    ctx.close()


def test_a_fresh_sign_up_gets_the_offer_without_a_reload(owui, pw):
    # Open WebUI signs up (and in) on the client: it stores the token and navigates without
    # loading a page. The page script loaded signed out must notice and offer, once.
    ctx = chrome(pw)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/auth")
        assert ready(page) == "no-token"
        page.evaluate("() => { window.ocSameLoad = true }")
        page.get_by_role("button", name="Sign up", exact=True).click()
        page.locator("#name").fill("Fresh")
        page.locator("#email").fill(f"fresh-{uuid.uuid4().hex[:8]}@e2e.example.com")
        page.locator("input[type=password]").first.fill("e2e-pass-2")
        page.get_by_role("button", name="Create Account").click()
        page.locator("#oc-push-offer").wait_for(timeout=WAIT_MS)
        assert page.evaluate("() => window.ocSameLoad") is True, "the page was reloaded"
        assert page.evaluate("() => window.ocPushState") == "no-permission"
        assert page.locator("#oc-push-offer").count() == 1
    finally:
        ctx.close()


def test_enable_subscribe_notify_suppress_and_resubscribe(owui, browser):
    ctx, page = browser
    token = owui
    # 1. The offer appears while the permission is not decided; Enable subscribes.
    page.goto(BASE + "/")
    assert ready(page) == "no-permission"
    page.locator("#oc-push-offer").wait_for(timeout=WAIT_MS)
    if os.environ.get("E2E_SHOTS"):
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)   # Open WebUI drawn around it
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-offer.png"))
    # What the user's Allow in the prompt does: notifications and push for this origin.
    ctx.grant_permissions(["notifications"], origin=BASE)
    # The permission lasts as long as this session (detached, it is "default" again), so it stays
    # attached, on a tab of its own: one on the page undoes ctx.set_offline on its next load.
    ctx.new_cdp_session(ctx.new_page()).send("Browser.setPermission", {
        "permission": {"name": "push", "userVisibleOnly": True}, "setting": "granted", "origin": BASE})
    page.bring_to_front()
    page.get_by_role("button", name="Enable notifications for replies").click()
    page.wait_for_function("() => window.ocPushState !== 'no-permission'", timeout=WAIT_MS)
    assert page.evaluate("() => window.ocPushState") == "saved"
    (sub,) = subscriptions(token, page)
    print("step 1 saved", fingerprint(sub["endpoint"]), time.strftime("%H:%M:%S"))   # for push_diagnostics
    assert sub["endpoint"].startswith("https://fcm.googleapis.com/"), sub["endpoint"][:40]
    assert sub["keys"]["p256dh"] and sub["device"]
    push_connection_live(ctx, BASE)

    # 2. A plain model's reply, while Open WebUI is in the background: Open WebUI's own chat.finished -> a notification.
    other = behind(ctx, page)
    a_chat, a_msg = new_chat(token, "stub", "Chat A")
    reply(token, "stub", a_chat, a_msg)
    (d,) = seen_for(page, a_chat)
    assert d["shown"] and d["payload"]["source"] == "api"
    assert d["payload"] == {"v": 2, "kind": "chat.finished", "source": "api", "chat_id": a_chat,
                            "message_id": a_msg, "title": "Chat A", "url": f"/c/{a_chat}", "fn": "web_push", "actions": ["reply", "mute"]}
    notes = page.evaluate(NOTES)   # the worker posts its decision once the notification is shown
    assert any(n["tag"] == f"chat-{a_chat}" and n["url"] == f"/c/{a_chat}" for n in notes), notes
    assert not any(s in json.dumps(notes) for s in SECRETS)
    page.evaluate(CLEAR)

    # 3. An OpenClaw reply in the chat on screen: Open WebUI's own event (source api) is skipped by
    #    the function, the Pipe's (source openclaw-pipe) is sent, and the worker drops it (on screen).
    b_chat, b_msg = new_chat(token, "openclaw_gateway.fake", "Chat B")
    page.goto(f"{BASE}/c/{b_chat}")
    assert ready(page) == "kept"
    page.bring_to_front()
    reply(token, "openclaw_gateway.fake", b_chat, b_msg)
    (d,) = seen_for(page, b_chat)
    assert d["payload"]["source"] == "openclaw-pipe" and d["shown"] is False, d
    assert not any(s in json.dumps(d) for s in SECRETS)
    # Another chat while Open WebUI is in front (chat B open): nothing either, as in a messaging app.
    # A later chat's decision arrives after B's two events.
    c_chat, c_msg = new_chat(token, "openclaw_gateway.fake", "Chat C")
    reply(token, "openclaw_gateway.fake", c_chat, c_msg)
    (d,) = seen_for(page, c_chat)
    assert d["payload"]["source"] == "openclaw-pipe" and d["shown"] is False, d
    assert [x["payload"]["source"] for x in page.evaluate("(c) => window.ocPushSeen.filter(d => d.payload.chat_id === c)", b_chat)] == ["openclaw-pipe"]
    assert page.evaluate(NOTES) == []
    # The same, with Open WebUI in the background: shown.
    other()
    e_chat, e_msg = new_chat(token, "openclaw_gateway.fake", "Chat E")
    reply(token, "openclaw_gateway.fake", e_chat, e_msg)
    (d,) = seen_for(page, e_chat)
    assert d["shown"] and d["payload"]["source"] == "openclaw-pipe"
    page.evaluate(CLEAR)

    # 4. Open WebUI's update path: every service worker unregistered, the page reloaded. The next
    #    load registers again with a new endpoint, which replaces this device's entry.
    page.evaluate("async () => { for (const r of await navigator.serviceWorker.getRegistrations()) await r.unregister(); }")
    page.goto(BASE + "/")
    assert ready(page) == "saved"
    (sub2,) = subscriptions(token, page)
    print("step 4 saved", fingerprint(sub2["endpoint"]), time.strftime("%H:%M:%S"))
    assert sub2["endpoint"] != sub["endpoint"] and sub2["device"] == sub["device"]
    push_connection_live(ctx, BASE)
    other()
    d_chat, d_msg = new_chat(token, "stub", "Chat D")
    reply(token, "stub", d_chat, d_msg)
    (d,) = seen_for(page, d_chat)
    assert d["shown"]


def test_a_subscription_the_push_service_retired_is_replaced_on_the_next_load(owui, browser):
    """A push service can retire a subscription the browser keeps (Chrome still lists it, no
    pushsubscriptionchange). The function then drops it and remembers its fingerprint in `gone`
    (GoneTests; the round trip with a real 410); the page's next load replaces it instead of saving
    the dead one again, and pushes come through. Here the function's record is written as it writes
    it, for the subscription this browser holds and still believes in."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    (old,) = subscriptions(token, page)
    v = state(token)
    put_state(token, {"gone": json.dumps([fingerprint(old["endpoint"])]), "subscriptions": json.dumps(
        [s for s in json.loads(v["subscriptions"]) if s["endpoint"] != old["endpoint"]])})
    page.goto(BASE + "/")
    assert ready(page) == "saved"
    (new,) = subscriptions(token, page)
    assert new["endpoint"] != old["endpoint"] and new["device"] == old["device"]
    assert page.evaluate(ENDPOINT) == new["endpoint"], "the page holds the new one"
    push_connection_live(ctx, BASE)
    behind(ctx, page)
    r_chat, r_msg = new_chat(token, "stub", "Chat Replaced")
    reply(token, "stub", r_chat, r_msg)
    (d,) = seen_for(page, r_chat)
    assert d["shown"]
    page.evaluate(CLEAR)
    page.bring_to_front()


ENDPOINT = "async () => (await (await navigator.serviceWorker.getRegistration('/'))?.pushManager.getSubscription())?.endpoint"


def history(token, chat_id):
    return api(f"/api/v1/chats/{chat_id}", token=token)["chat"]["history"]


def act(page, action, data, reply=None):
    """What a tap on the notification's action does: the service worker's own
    handler (`act`), asked through a message; returns its result."""
    return page.evaluate("""([action, data, reply]) => new Promise((ok) => {
        navigator.serviceWorker.addEventListener('message', function f(e) {
          if (e.data && e.data.type === 'oc-push-action') { navigator.serviceWorker.removeEventListener('message', f); ok(e.data.result); }
        });
        navigator.serviceWorker.controller.postMessage({type: 'oc-push-act', action, data, reply});
      })""", [action, data, reply])


def wait_done(token, chat_id, message_id):
    end = time.time() + WAIT_MS / 1000
    while time.time() < end:
        m = history(token, chat_id)["messages"].get(message_id) or {}
        if m.get("done") and m.get("content"):
            return m
        time.sleep(0.3)   # a poll of the saved state, never a wait for time
    raise AssertionError(f"{message_id} not done")


def owui_log(since):
    return subprocess.run(["docker", "logs", "--since", str(int(since) - 1), os.environ.get("E2E_NAME", "owui-webpush-e2e")],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout


def owui_icon():
    """Open WebUI's app icon as the service worker shows it: the manifest's first picture, a data: URL."""
    icon = json.load(urllib.request.urlopen(BASE + "/manifest.json"))["icons"][0]["src"]
    res = urllib.request.urlopen(BASE + icon)
    return f"data:{res.headers.get_content_type()};base64," + base64.b64encode(res.read()).decode()


def test_preview_reply_mute_and_the_bell(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    other = behind(ctx, page)

    # B. Preview on: the start of the reply, as plain text, is the body.
    api("/api/v1/functions/id/web_push/valves/update",
        {**api("/api/v1/functions/id/web_push/valves", token=token), "PREVIEW_CHARS": 600}, token)
    try:
        p_chat, p_msg = new_chat(token, "stub", "Chat P")
        reply(token, "stub", p_chat, p_msg)
        (d,) = seen_for(page, p_chat)
        assert d["shown"] and d["payload"]["body"].startswith("STUB-SECRET-REPLY"), d
        assert d["payload"]["fn"] == "web_push"
        notes = page.evaluate(NOTES)
        assert any(n["tag"] == f"chat-{p_chat}" and n["body"].startswith("STUB-SECRET-REPLY") for n in notes), notes
        # The picture is Open WebUI's own icon, the badge the browser's.
        (note,) = [n for n in notes if n["tag"] == f"chat-{p_chat}"]
        assert note["icon"] == owui_icon(), note["icon"][:80]
        assert note["badge"] == "", note["badge"]
        page.evaluate(CLEAR)
    finally:
        api("/api/v1/functions/id/web_push/valves/update",
            {**api("/api/v1/functions/id/web_push/valves", token=token), "PREVIEW_CHARS": 0}, token)

    # C. Reply from the notification: a user message in the chat under its leaf, answered.
    data = {"chat_id": p_chat, "url": f"/c/{p_chat}", "fn": "web_push"}
    leaf = history(token, p_chat)["currentId"]
    assert act(page, "reply", data, "a reply from the shade") == "sent"
    h = history(token, p_chat)
    (u,) = [m for m in h["messages"].values() if m["role"] == "user" and m["content"] == "a reply from the shade"]
    assert u["parentId"] == leaf
    (a_id,) = u["childrenIds"]
    assert wait_done(token, p_chat, a_id)["content"].startswith("STUB-SECRET-REPLY")

    # C. Reply while the chat's run is going: in the chat at once, under the running reply, nothing stopped.
    s_chat, s_msg = new_chat(token, "slow", "Chat S")
    reply(token, "slow", s_chat, s_msg)
    assert not history(token, s_chat)["messages"][s_msg].get("done")
    assert act(page, "reply", {"chat_id": s_chat, "url": f"/c/{s_chat}", "fn": "web_push"}, "while it runs") == "sent"
    h = history(token, s_chat)
    (u,) = [m for m in h["messages"].values() if m["role"] == "user" and m["content"] == "while it runs"]
    assert u["parentId"] == s_msg and not h["messages"][s_msg].get("done")
    assert wait_done(token, s_chat, s_msg)["content"] == "SLOW-REPLY done"
    assert wait_done(token, s_chat, u["childrenIds"][0])["content"] == "SLOW-REPLY done"

    # C. A reply that cannot be sent (the chat's model is gone): the chat opens with the text in the box.
    f_chat, f_msg = new_chat(token, "stub", "Chat F")
    api(f"/api/v1/chats/{f_chat}", {"chat": {"models": ["no-such-model"]}}, token)
    # The worker sends this page to the chat, so its answer never comes back here: wait for the chat page.
    page.evaluate("(m) => navigator.serviceWorker.controller.postMessage(m)",
                  {"type": "oc-push-act", "action": "reply", "reply": "keep this text",
                   "data": {"chat_id": f_chat, "url": f"/c/{f_chat}", "fn": "web_push"}})
    page.wait_for_url(f"**/c/{f_chat}*", timeout=WAIT_MS)
    page.wait_for_function("() => window.ocPushReply && window.ocPushReply.text === 'keep this text'", timeout=WAIT_MS)
    assert "/c/" + f_chat in page.url and "oc-reply" not in page.url
    assert page.locator("#chat-input").inner_text().strip() == "keep this text"   # once
    assert "not sent" in page.locator("#oc-push-notice").inner_text()
    assert not [m for m in history(token, f_chat)["messages"].values() if m.get("content") == "keep this text"]

    # D. Mute 8 h from the notification: kept server-side beside the subscriptions; nothing is sent.
    before = state(token)["subscriptions"]
    assert act(page, "mute", data) == "muted"
    v = state(token)
    assert v["subscriptions"] == before
    until = json.loads(v["mutes"])[p_chat]
    assert 8 * 3600 - 60 < until - time.time() <= 8 * 3600
    m_msg = str(uuid.uuid4())
    t_mute = time.time()
    h = history(token, p_chat)
    leaf = h["currentId"]
    u_id = str(uuid.uuid4())
    api("/api/chat/completions", {"model": "stub", "chat_id": p_chat, "id": m_msg, "parent_id": leaf, "stream": True,
                                  "user_message": {"id": u_id, "parentId": leaf, "childrenIds": [], "role": "user",
                                                   "content": "muted?", "models": ["stub"]},
                                  "message_ids": [{"model_id": "stub", "message_id": m_msg, "modelIdx": 0}],
                                  "session_id": "e2e"}, token)
    wait_done(token, p_chat, m_msg)
    end = time.time() + WAIT_MS / 1000
    while f"chat is muted, nothing sent chat_id={p_chat}" not in owui_log(t_mute):
        assert time.time() < end, "the function never skipped the muted chat"
        time.sleep(0.3)
    assert not [x for x in page.evaluate("() => window.ocPushSeen") if x["payload"]["chat_id"] == p_chat and x["payload"]["message_id"] == m_msg]

    # D. The bell in the chat: shows the mute, changes it, survives Open WebUI redrawing its header.
    page.bring_to_front()
    page.goto(f"{BASE}/c/{p_chat}")
    bell = page.locator("#oc-push-bell")
    bell.wait_for(timeout=WAIT_MS)
    assert bell.get_attribute("title").startswith("Muted until")
    assert bell.evaluate("(b) => !!(b.compareDocumentPosition(document.getElementById('chat-context-menu-button')) & Node.DOCUMENT_POSITION_FOLLOWING) && b.closest('nav') !== null")
    assert bell.evaluate("(b) => b.className === document.getElementById('chat-context-menu-button').className")
    bell.click()
    page.get_by_role("menuitem", name="Unmute").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Notifications on'", timeout=WAIT_MS)
    assert p_chat not in json.loads(state(token)["mutes"])
    bell.click()
    page.get_by_role("menuitem", name="Mute always").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Muted'", timeout=WAIT_MS)
    assert json.loads(state(token)["mutes"])[p_chat] == 0
    if os.environ.get("E2E_SHOTS"):
        bell.click()
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-bell.png"))
        page.keyboard.press("Escape")
    # Open WebUI redraws the header for another chat: the bell follows, for that chat.
    page.goto(f"{BASE}/c/{s_chat}")
    page.wait_for_function(f"() => document.getElementById('oc-push-bell')?.dataset.chat === {json.dumps(s_chat)}", timeout=WAIT_MS)
    assert page.locator("#oc-push-bell").get_attribute("title") == "Notifications on"
    # Right to left: the menu opens inside the window, under the bell.
    page.evaluate("() => document.documentElement.setAttribute('dir', 'rtl')")
    page.locator("#oc-push-bell").click()
    box = page.locator("#oc-push-mute-menu").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"], box
    page.keyboard.press("Escape")
    assert page.locator("#oc-push-mute-menu").count() == 0
    page.evaluate("() => document.documentElement.removeAttribute('dir')")
    put_state(token, {"mutes": "{}"})
    other()


def muted_log(since, text):
    end = time.time() + WAIT_MS / 1000
    while text not in owui_log(since):
        assert time.time() < end, f"the function never logged {text!r}"
        time.sleep(0.3)   # a poll of the container's log, never a wait for time


def test_one_notification_per_chat_and_the_folder_mute(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    other = behind(ctx, page)
    page.evaluate(CLEAR)

    # A. Two replies in one chat leave one notification (the tag is the chat's).
    o_chat, o_msg = new_chat(token, "stub", "Chat O")
    reply(token, "stub", o_chat, o_msg)
    seen_for(page, o_chat)
    o2 = str(uuid.uuid4())
    leaf = history(token, o_chat)["currentId"]
    api("/api/chat/completions", {"model": "stub", "chat_id": o_chat, "id": o2, "parent_id": leaf, "stream": True,
                                  "user_message": {"id": str(uuid.uuid4()), "parentId": leaf, "childrenIds": [], "role": "user",
                                                   "content": "again", "models": ["stub"]},
                                  "message_ids": [{"model_id": "stub", "message_id": o2, "modelIdx": 0}], "session_id": "e2e"}, token)
    assert all(d["shown"] for d in seen_for(page, o_chat, 2))
    assert [n["tag"] for n in page.evaluate(NOTES) if n["tag"] == f"chat-{o_chat}"] == [f"chat-{o_chat}"]
    page.evaluate(CLEAR)

    # B. Folder mute. A chat in folder Child, which is inside folder Parent.
    parent = api("/api/v1/folders/", {"name": "Parent"}, token)["id"]
    child = api("/api/v1/folders/", {"name": "Child", "parent_id": parent}, token)["id"]
    q_chat, _ = new_chat(token, "stub", "Chat Q")
    api(f"/api/v1/chats/{q_chat}/folder", {"folder_id": child}, token)
    before = state(token)["subscriptions"]

    def answer_and_check(muted_by):
        t = time.time()
        msg = str(uuid.uuid4())
        leaf = history(token, q_chat)["currentId"]
        api("/api/chat/completions", {"model": "stub", "chat_id": q_chat, "id": msg, "parent_id": leaf, "stream": True,
                                      "user_message": {"id": str(uuid.uuid4()), "parentId": leaf, "childrenIds": [], "role": "user",
                                                       "content": "folder?", "models": ["stub"]},
                                      "message_ids": [{"model_id": "stub", "message_id": msg, "modelIdx": 0}], "session_id": "e2e"}, token)
        if muted_by:
            wait_done(token, q_chat, msg)
            muted_log(t, f"folder {muted_by[:8]} is muted, nothing sent chat_id={q_chat}")
            assert not [x for x in page.evaluate("() => window.ocPushSeen") if x["payload"].get("message_id") == msg]
        else:
            page.wait_for_function("(m) => window.ocPushSeen.some(d => d.payload.message_id === m && d.shown)", arg=msg, timeout=WAIT_MS)

    # The bell's menu mutes the chat's own folder; the bell says so.
    page.bring_to_front()
    page.goto(f"{BASE}/c/{q_chat}")
    bell = page.locator("#oc-push-bell")
    bell.wait_for(timeout=WAIT_MS)
    bell.click()
    page.get_by_role("menuitem", name="Mute folder always").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Muted (folder Child)'", timeout=WAIT_MS)
    v = state(token)
    assert json.loads(v["folder_mutes"]) == {child: 0} and v["subscriptions"] == before
    other()
    answer_and_check(child)

    # A mute of the folder above covers it; the menu offers to unmute that folder.
    put_state(token, {"folder_mutes": json.dumps({parent: 0})})
    answer_and_check(parent)
    page.bring_to_front()
    page.goto(f"{BASE}/c/{q_chat}")
    page.wait_for_function("() => document.getElementById('oc-push-bell')?.title === 'Muted (folder Parent)'", timeout=WAIT_MS)
    bell.click()
    assert "(by Parent)" in page.locator("#oc-push-mute-menu").inner_text()
    if os.environ.get("E2E_SHOTS"):
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-folder-mute.png"))
    page.get_by_role("menuitem", name="Unmute folder Parent").click()
    page.wait_for_function("() => document.getElementById('oc-push-bell').title === 'Notifications on'", timeout=WAIT_MS)
    assert json.loads(state(token)["folder_mutes"]) == {}
    other()
    answer_and_check(None)


def read(page, chat_id):
    """The page script says it closed `chat_id`'s notifications; returns the tags still showing, read
    at once (Chrome on Linux closes notifications by itself after a while, so only an immediate read
    tells what the page closed)."""
    page.wait_for_function("(c) => window.ocPushRead === c", arg=chat_id, timeout=WAIT_MS)
    return sorted(n["tag"] for n in page.evaluate(NOTES))


def test_opening_a_chat_clears_its_notifications(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    other = behind(ctx, page)
    page.evaluate(CLEAR)
    chats = {}
    for name in ("X", "Y", "Z"):
        chat, msg = new_chat(token, "stub", "Chat " + name)
        reply(token, "stub", chat, msg)
        (d,) = seen_for(page, chat)
        assert d["shown"]
        chats[name] = chat
    tag = {k: f"chat-{v}" for k, v in chats.items()}
    assert sorted(n["tag"] for n in page.evaluate(NOTES)) == sorted(tag.values())
    # Opened by any route, not only the notification: a page load ...
    page.bring_to_front()
    page.goto(f"{BASE}/c/{chats['X']}")
    assert read(page, chats["X"]) == sorted([tag["Y"], tag["Z"]])
    # ... or Open WebUI's own navigation to another chat (SvelteKit's pushState); closed even when
    # Cache Storage refuses (the page script marks its closes there first, oc-sw.js swipe = read).
    page.evaluate("() => { window.ocOpen = CacheStorage.prototype.open; CacheStorage.prototype.open = () => Promise.reject(new Error('no storage')); }")
    page.evaluate("(c) => history.pushState({}, '', '/c/' + c)", chats["Y"])
    try:
        assert read(page, chats["Y"]) == [tag["Z"]]
    finally:
        page.evaluate("() => { CacheStorage.prototype.open = window.ocOpen; }")
    # Hidden, a chat on the page is not read; shown again on it, it is. Chrome under Xvfb does not
    # reliably hide a page (another tab, a minimized window), so the page's visibility is set here:
    # what the page script reads (visibilityState) and the event it listens to.
    vis = """(v) => { Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => v});
                      document.dispatchEvent(new Event('visibilitychange')); }"""
    page.evaluate(vis, "hidden")
    page.evaluate("(c) => history.replaceState({}, '', '/c/' + c)", chats["Z"])
    assert page.evaluate("() => window.ocPushRead") == chats["Y"]
    page.evaluate(vis, "visible")
    assert read(page, chats["Z"]) == []
    page.evaluate("() => delete document.visibilityState")
    other()


def unread(token, chat_id):
    """The sidebar's blue dot (Open WebUI 0.13): the chat changed after it was last read."""
    (c,) = [c for c in api("/api/v1/chats/?page=1", token=token) if c["id"] == chat_id]
    return c.get("last_read_at") is None or c["updated_at"] > c["last_read_at"]


def test_mark_as_read_from_the_notification(owui, browser):
    ctx, page = browser
    token = owui
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    api("/api/v1/functions/id/web_push/valves/update",
        {**api("/api/v1/functions/id/web_push/valves", token=token), "NOTIFICATION_ACTIONS": "reply,read"}, token)
    try:
        other = behind(ctx, page)
        r_chat, r_msg = new_chat(token, "stub", "Chat R")
        reply(token, "stub", r_chat, r_msg)
        (d,) = seen_for(page, r_chat)
        assert d["shown"] and d["payload"]["actions"] == ["reply", "read"]
        wait_done(token, r_chat, r_msg)
        # Unread, as a reply that came while you were away (here it may share the chat's creation second).
        api(f"/api/v1/chats/{r_chat}/unread", {}, token)
        assert unread(token, r_chat)
        # Mark as read: the dot goes, through Open WebUI's own read event, without opening the chat.
        data = {"chat_id": r_chat, "url": f"/c/{r_chat}", "fn": "web_push"}
        assert act(page, "read", data) == "read"
        assert not unread(token, r_chat)
        assert "/c/" not in page.url
        # Signed out (no session cookie): not marked, the chat opens instead, which reads it.
        api(f"/api/v1/chats/{r_chat}/unread", {}, token)
        assert unread(token, r_chat)
        cookies = ctx.cookies(BASE)
        ctx.clear_cookies(name="token")
        try:
            page.evaluate("(m) => navigator.serviceWorker.controller.postMessage(m)",
                          {"type": "oc-push-act", "action": "read", "data": data})
            page.wait_for_url(f"**/c/{r_chat}", timeout=WAIT_MS)
        finally:
            ctx.add_cookies([c for c in cookies if c["name"] == "token"])
        other()
    finally:
        api("/api/v1/functions/id/web_push/valves/update",
            {**api("/api/v1/functions/id/web_push/valves", token=token), "NOTIFICATION_ACTIONS": "reply,mute"}, token)


def tap(page, action, data, reply=None):
    """A tap on the notification (or one of its buttons), as the service worker handles it; returns at once."""
    page.evaluate("(m) => navigator.serviceWorker.controller.postMessage(m)",
                  {"type": "oc-push-act", "action": action, "data": data, "reply": reply})


def test_a_tap_opens_the_chat_without_a_reload(owui, browser):
    """An open Open WebUI window goes to the chat in the app (no document load): timeOrigin is the page's identity."""
    ctx, page = browser
    token = owui
    a_chat, _ = new_chat(token, "stub", "Chat TA")
    b_chat, _ = new_chat(token, "stub", "Chat TB")
    page.goto(f"{BASE}/c/{b_chat}")
    assert ready(page) in ("kept", "saved")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    origin = page.evaluate("() => performance.timeOrigin")
    # On chat B, the notification for chat A.
    tap(page, "", {"chat_id": a_chat, "url": f"/c/{a_chat}"})
    page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=a_chat, timeout=WAIT_MS)
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    assert page.evaluate("() => performance.timeOrigin") == origin, "the page was loaded again"
    assert page.evaluate("() => window.ocPushOpened") == a_chat
    # The same chat's notification: nothing to do.
    tap(page, "", {"chat_id": a_chat, "url": f"/c/{a_chat}"})
    page.wait_for_function("() => window.ocPushOpenedSame === true", timeout=WAIT_MS)
    assert page.evaluate("() => performance.timeOrigin") == origin
    # Reply that cannot be sent: the chat opens in the app, the text in the message box.
    f_chat, _ = new_chat(token, "stub", "Chat TF")
    api(f"/api/v1/chats/{f_chat}", {"chat": {"models": ["no-such-model"]}}, token)
    # A guard, not a fail-first: chat F has a draft of its own in this tab (Open WebUI 0.13 keeps one
    # per chat in sessionStorage and puts it back while it loads the chat); the reply must stay.
    page.evaluate("(c) => sessionStorage.setItem('chat-input-' + c, JSON.stringify({prompt: 'an older draft'}))", f_chat)
    # Chat F arrives (and its draft goes back in) just after the reply is in the box.
    page.evaluate("""([c, ms]) => { const f = window.fetch; window.fetch = async (u, o) => {
        if (String(u).endsWith('/api/v1/chats/' + c)) await new Promise((ok) => setTimeout(ok, ms));
        return f(u, o); }; }""", [f_chat, CHAT_LOAD_DELAY_MS])
    tap(page, "reply", {"chat_id": f_chat, "url": f"/c/{f_chat}", "fn": "web_push"}, "kept, not reloaded")
    page.wait_for_function("() => window.ocPushReply && window.ocPushReply.text === 'kept, not reloaded'", timeout=WAIT_MS)
    assert "/c/" + f_chat in page.url and "oc-reply" not in page.url
    page.wait_for_timeout(DRAFT_RESTORE_WAIT_MS)
    assert page.locator("#chat-input").inner_text().strip() == "kept, not reloaded", \
        "chat F's own draft replaced the reply, or it went in twice"
    assert page.evaluate("(c) => caches.open('oc-push-drafts').then(d => d.match('/oc-push/draft/' + c)).then(r => !r)", f_chat), \
        "the cached reply was kept after it was put in the box"
    assert page.evaluate("() => performance.timeOrigin") == origin, "the page was loaded again"


def side_chat(token, parent, question):
    """An Open WebUI 0.13 side chat (/btw) as its panel saves one: a chat of its own naming the parent, plus a
    side tab for it in the user's saved view of the parent (the shape of a real one, read from a live row)."""
    u, msg = str(uuid.uuid4()), str(uuid.uuid4())
    msgs = {u: {"id": u, "role": "user", "content": question, "parentId": None, "childrenIds": [msg]},
            msg: {"id": msg, "role": "assistant", "content": "", "parentId": u, "childrenIds": [], "model": "stub"}}
    side = api("/api/v1/chats/new", {"chat": {"title": "Side SQ", "models": ["stub"], "originalChatId": parent,
                                              "branchPointMessageId": None, "history": {"messages": msgs, "currentId": msg},
                                              "messages": list(msgs.values())}}, token)["id"]
    tab = str(uuid.uuid4())
    api(f"/api/v1/chats/{parent}/view", {"state": {"schemaVersion": 1, "selectedTerminalId": None, "panels": {
        "tabs": [{"id": "main-chat", "kind": "chat", "label": "Chat"},
                 {"id": tab, "kind": "side", "label": question, "chatId": side, "sideChatNumber": 1}],
        "activeId": tab, "history": ["main-chat"], "groups": [{"id": "default", "tabIds": ["main-chat", tab], "activeId": tab}],
        "activeGroupId": "default", "layout": {"type": "group", "groupId": "default"}}}}, token)
    return side, msg


def test_a_side_chats_notification_opens_its_parent_with_the_side_chat_in_the_panel(owui, browser):
    """A reply in a side chat: the tap opens the parent chat, its panel showing the side chat, not the side
    chat as a chat of its own. Mute, read and reply still act on the side chat."""
    ctx, page = browser
    token = owui
    parent, _ = new_chat(token, "stub", "Chat SP")
    side, msg = side_chat(token, parent, "a side question SQ")   # its own text, not its title: the sidebar lists that
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    behind(ctx, page)
    page.evaluate(CLEAR)
    reply(token, "stub", side, msg)
    [seen] = seen_for(page, side)
    assert (seen["payload"]["url"], seen["payload"]["chat_id"]) == (f"/c/{parent}?oc-side={side}", side)
    assert [n["url"] for n in page.evaluate(NOTES) if n["tag"] == f"chat-{side}"] == [f"/c/{parent}?oc-side={side}"]
    tap(page, "", {"chat_id": side, "url": seen["payload"]["url"]})
    page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=parent, timeout=WAIT_MS)
    # On a phone (under 768 px) Open WebUI's panel is a sheet, closed on each load: the page script opens
    # it on the side chat's tab (no press of the navbar's Tabs button here). The sheet shows the side chat's
    # answer (its question counts as context there), not the parent's empty one.
    sheet = page.get_by_role("dialog")
    sheet.get_by_role("tab", name="Side SQ", selected=True).wait_for(timeout=WAIT_MS)
    sheet.get_by_text(SECRETS[0]).first.wait_for(timeout=WAIT_MS)
    shot(page, "side-chat-tap")
    # A reply that could not be sent waits in the side chat's own box: that one opens the side chat.
    assert act(page, "reply", {"chat_id": side, "url": f"/c/{parent}", "fn": "web_push"}, "") == "compose"
    page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=side, timeout=WAIT_MS)


def test_a_side_chats_tap_with_the_parent_already_open_shows_the_side_chat(owui, browser):
    """The parent is on screen with its sheet closed (on a phone the sheet lists only the side tabs, the main
    chat being the page itself): the tap opens the sheet on the side chat's tab, with no load."""
    ctx, page = browser
    token = owui
    parent, _ = new_chat(token, "stub", "Chat SO")
    side, _ = side_chat(token, parent, "an open side question SO")
    page.goto(BASE + f"/c/{parent}")
    assert ready(page) in ("kept", "saved")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    assert page.get_by_role("dialog").count() == 0, "the sheet starts closed"
    origin = page.evaluate("() => performance.timeOrigin")
    tap(page, "", {"chat_id": side, "url": f"/c/{parent}?oc-side={side}"})
    page.get_by_role("dialog").get_by_role("tab", name="Side SQ", selected=True).wait_for(timeout=WAIT_MS)
    assert page.evaluate("() => performance.timeOrigin") == origin, "the page was loaded again"


def test_the_page_script_finds_open_webuis_tabs_button_and_tab_by_structure(owui, browser):
    """The DOM contract of oc-push.js pendingSide, pinned against the real Open WebUI: its Tabs button is
    found without its (translated) label, and a side chat's tab has the id `chat-panel-<tab id>`."""
    ctx, page = browser
    token = owui
    parent, _ = new_chat(token, "stub", "Chat SC")
    side, _ = side_chat(token, parent, "a contract side question SC")
    page.goto(BASE + f"/c/{parent}")
    assert ready(page) in ("kept", "saved")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    selector = re.search(r'TABS_BUTTON = "([^"]+)"', open(os.path.join(ROOT, "web", "oc-push.js")).read()).group(1)
    found = page.evaluate("(s) => [...document.querySelectorAll(s)].map((b) => b.getAttribute('aria-label'))", selector)
    assert found == ["Tabs"], found
    view = api(f"/api/v1/chats/{parent}/view", None, token, method="GET")
    tab = next(t for t in view["state"]["panels"]["tabs"] if t["kind"] == "side" and t["chatId"] == side)
    page.locator(selector).click()
    page.locator(f"[id='chat-panel-{tab['id']}']").wait_for(timeout=WAIT_MS)


def test_an_unsent_reply_opened_by_a_full_load_is_put_in_the_box_once(owui, pw):
    """No Open WebUI window open: the service worker opens the chat in a new one (a full load), with the reply
    that could not be sent waiting in its drafts cache and ?oc-reply=unsent. SvelteKit puts the URL it loaded back
    after the page script took the query off, and a second run put the same text in the box again (TEXTTEXT).
    Set up as the worker leaves it when the send fails (act(), the reply branch)."""
    token = owui
    chat, _ = new_chat(token, "stub", "Chat Once")
    ctx = chrome(pw)
    try:
        ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
        ctx.add_cookies([{"name": "token", "value": token, "url": BASE}])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/")
        controlled(page)
        page.evaluate("""([c, text]) => caches.open('oc-push-drafts').then((d) =>
            d.put('/oc-push/draft/' + c, new Response(JSON.stringify({text, at: Date.now()}))))""", [chat, "only once"])
        page.goto(f"{BASE}/c/{chat}?oc-reply=unsent")
        page.wait_for_function("() => !!window.ocPushReply", timeout=WAIT_MS)
        page.wait_for_timeout(DRAFT_RESTORE_WAIT_MS)   # Open WebUI has loaded the chat and handled its own draft
        assert page.locator("#chat-input").inner_text().strip() == "only once"
        assert "not sent" in page.locator("#oc-push-notice").inner_text()
        assert "oc-reply" not in page.url
    finally:
        ctx.close()


# Open WebUI 0.13's "Open Sidebar" buttons: the chat navbar's on a phone-sized window
# (#sidebar-toggle-button, only on a chat), the sidebar rail's on a wide one (#sidebar-open-button).
OPEN_SIDEBAR = "[...document.querySelectorAll('button[aria-label^=\"Open Sidebar\"]')].filter((b) => b.checkVisibility())"


def sidebar_link(page, chat_id):
    """A chat's entry in the sidebar, the sidebar opened. Whether the sidebar starts open is Open
    WebUI's own saved state (localStorage.sidebar), which earlier tests leave either way: waits for
    the entry or a closed sidebar, then opens it with the button this layout shows."""
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)   # Open WebUI drawn
    link = page.locator(f'a[href="/c/{chat_id}"]')
    page.wait_for_function(f"""(c) => document.querySelector(`a[href="/c/${{c}}"]`)?.checkVisibility()
        || {OPEN_SIDEBAR}.length > 0""", arg=chat_id, timeout=WAIT_MS)
    if not link.is_visible():
        page.locator('button[aria-label^="Open Sidebar"]').filter(visible=True).first.click()
    return link


def chat_menu(page, chat_id):
    """Open WebUI's own chat menu (⋯) for a chat in the sidebar; returns the menu."""
    item = sidebar_link(page, chat_id)
    item.wait_for(timeout=WAIT_MS)
    # The chat's ⋯ (in the DOM, shown on hover), pressed as a pointer does.
    item.evaluate("""(a) => { let box = a, b = null;
        while (box && !(b = box.querySelector('button[aria-haspopup]'))) box = box.parentElement;
        for (const t of ['pointerdown', 'pointerup']) b.dispatchEvent(new PointerEvent(t, {bubbles: true}));
        b.click(); }""")
    menu = page.locator("[role=menu]")
    menu.wait_for(timeout=WAIT_MS)
    return menu


def test_mark_as_read_from_the_chat_menu(owui, browser):
    """The DOM contract with Open WebUI 0.13.0's chat menu: an upgrade that moves it fails here."""
    ctx, page = browser
    token = owui
    m_chat, m_msg = new_chat(token, "stub", "Chat M")
    reply(token, "stub", m_chat, m_msg)
    wait_done(token, m_chat, m_msg)
    api(f"/api/v1/chats/{m_chat}/unread", {}, token)
    assert unread(token, m_chat)
    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 844})   # the sidebar open beside the chat
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    menu = chat_menu(page, m_chat)
    own = menu.locator("button[role=menuitem]")
    item = menu.get_by_role("menuitem", name="Mark as read")
    item.wait_for(timeout=WAIT_MS)
    # Beside Open WebUI's own "Mark as unread", styled as its items.
    assert item.evaluate("(b) => b.previousElementSibling.querySelector('path').getAttribute('d').startsWith('M8 12L11 15L16 10')")
    assert item.evaluate("(b) => b.className === b.previousElementSibling.className")
    if os.environ.get("E2E_SHOTS"):
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-menu-mark-read.png"))
    item.click()
    end = time.time() + WAIT_MS / 1000
    while unread(token, m_chat):
        assert time.time() < end, "the chat was not marked read"
        time.sleep(0.3)   # a poll of the chat list, never a wait for time
    assert page.locator("[role=menu]").count() == 0, "the menu stayed open"
    assert f"/c/{m_chat}" not in page.url, "the chat was opened"
    assert own.count() == 0

    # Fail safe: a menu without Open WebUI 0.13's structure gets nothing, and one console line.
    logs = []
    page.on("console", lambda m: logs.append(m.text))
    # A chat item whose ⋯ opens a menu Open WebUI 0.13 would not draw (twice: still one line).
    page.evaluate("""(c) => { const item = document.createElement('div'); item.id = 'fake-item';
        item.innerHTML = `<div id="sidebar-chat-item"><a href="/c/${c}">x</a></div><button aria-haspopup="true">⋯</button>`;
        document.body.appendChild(item);
        for (const id of ['not-owui', 'not-owui-2']) {
          item.querySelector('button').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
          const m = document.createElement('div'); m.setAttribute('role', 'menu'); m.id = id;
          m.innerHTML = '<button role="menuitem">Something</button>'; document.body.appendChild(m);
        } }""", m_chat)
    page.wait_for_function("() => document.querySelectorAll('#not-owui, #not-owui-2').length === 2", timeout=WAIT_MS)
    page.wait_for_function("() => window.ocPushMenuContract === 'missing'", timeout=WAIT_MS)
    assert page.locator("#not-owui [role=menuitem], #not-owui-2 [role=menuitem]").count() == 2
    assert len([l for l in logs if "chat menu" in l]) == 1, logs
    # Open WebUI's structure and icon, but the label is not where the script looks: nothing either.
    page.evaluate("""(icon) => { window.ocPushMenuContract = 'ok'; const b = document.querySelector('#fake-item button');
        b.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
        const m = document.createElement('div'); m.setAttribute('role', 'menu'); m.id = 'bad-label';
        m.innerHTML = `<div class="app-dropdown-menu"><button role="menuitem"><svg><path d="${icon} Z"></path></svg>`
          + `<span>Mark as unread</span><div></div></button></div>`;
        document.body.appendChild(m); }""", "M8 12L11 15L16 10")
    page.wait_for_function("() => document.querySelector('#bad-label')?.dataset.ocPush === 'seen'", timeout=WAIT_MS)
    page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
    assert page.locator("#bad-label [role=menuitem]").count() == 1, "an item was added to a menu with an unknown label"
    assert page.evaluate("() => window.ocPushMenuContract") == "missing"
    assert len([l for l in logs if "chat menu" in l]) == 1, logs
    page.evaluate("() => document.querySelectorAll('#not-owui, #not-owui-2, #bad-label, #fake-item').forEach(e => e.remove())")
    page.set_viewport_size({"width": 390, "height": 844})


def native_menu(page, trigger_js, arg):
    """Open one of Open WebUI's own ⋯ menus, pressed as a pointer does; returns the menu."""
    # Every menu before it is closed (ours close Open WebUI's when they act).
    page.wait_for_function("() => !document.querySelector('[role=menu]')", timeout=WAIT_MS)
    page.evaluate("""([find, arg]) => { const b = (new Function('arg', 'return ' + find))(arg);
        for (const t of ['pointerdown', 'pointerup']) b.dispatchEvent(new PointerEvent(t, {bubbles: true}));
        b.click(); }""", [trigger_js, arg])
    menu = page.locator("[role=menu]").first
    menu.wait_for(timeout=WAIT_MS)
    return menu


def shot(page, name):
    """A still for the PR (E2E_SHOTS=<dir>), taken before the check so an old build gives its own."""
    if os.environ.get("E2E_SHOTS"):
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], name + ".png"))


def close_menus(page):
    """Escape, sent where Open WebUI's menus listen (focus is not in a menu opened by dispatched events),
    then a press outside, as a user closes one, while a menu is still open."""
    page.wait_for_function("""() => { if (!document.querySelector('[role=menu]')) return true;
        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
        for (const t of ['pointerdown', 'pointerup']) document.body.dispatchEvent(new PointerEvent(t, {bubbles: true}));
        return false; }""", timeout=WAIT_MS, polling=MENU_DRAWN_WAIT_MS)


FOLDER_ROW_TRIGGER = "document.getElementById('folder-' + arg + '-button').querySelector('button[aria-haspopup]')"
CHAT_TRIGGER = """(() => { let box = document.querySelector('a[href="/c/' + arg + '"]'), b = null;
    while (box && !(b = box.querySelector('button[aria-haspopup]'))) box = box.parentElement; return b; })()"""
ITEMS = "(m) => [...m.querySelectorAll('.app-dropdown-menu > button[role=menuitem]')].map(b => b.textContent.trim())"


NO_SUBSCRIPTIONS = "[]"   # the function's default for the subscriptions valve
# A device of the user's that gets pushes, for a test that needs one to exist and sends it nothing.
TAP_DEVICE = {"endpoint": "https://push.invalid/e2e-phone-tap", "keys": {"p256dh": "", "auth": ""},
              "device": "e2e-phone-tap", "ua": "e2e", "at": 0}


def test_a_real_tap_on_a_folders_menu_is_seen_on_a_phone(owui, pw):
    """A phone and a real touch: the tap on a folder's ⋯ in the sidebar lands on the folder's link
    (Open WebUI makes it a menu trigger too, aria-haspopup), not on the ⋯ button, and the same folder
    menu opens. The page script must know it is that folder's menu, or "Mute folder" never comes.
    A mouse lands on the button, and the other tests press the button itself."""
    token = owui
    folder = api("/api/v1/folders/", {"name": "Tap " + uuid.uuid4().hex[:4]}, token)["id"]
    chat, _ = new_chat(token, "stub", "Chat Tap")
    api(f"/api/v1/chats/{chat}/folder", {"folder_id": folder}, token)
    # Mute is offered only to a user with a device that gets pushes (oc-push.js hasDevices). This
    # test's own, not one an earlier test left: nothing replies while it runs, so nothing is sent to it.
    before = state(token)
    saved = before.get("subscriptions") or NO_SUBSCRIPTIONS   # a user who never saved any has none set
    put_state(token, {"subscriptions": json.dumps(json.loads(saved) + [TAP_DEVICE])})
    ctx = pw.chromium.launch_persistent_context(
        tempfile.mkdtemp(prefix="webpush-e2e-"), executable_path=CHROME, headless=False, args=["--no-sandbox"],
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    # Signed in as Open WebUI's sign-in leaves a browser: the token in localStorage and in the cookie.
    # Without the cookie the first /api/config is anonymous (no features), and the sidebar can come
    # up with no Folders section at all.
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
    ctx.add_cookies([{"name": "token", "value": token, "url": BASE}])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_role("button", name="Open Sidebar").first.click()
        row = page.locator(f"#folder-{folder}-button")
        page.wait_for_function(f"""() => document.getElementById('folder-{folder}-button')
            || [...document.querySelectorAll('button')].some((b) => b.innerText.trim() === 'Folders')""", timeout=WAIT_MS)
        if not row.count():
            page.get_by_role("button", name="Folders", exact=True).first.click()
        row.wait_for(timeout=WAIT_MS)
        page.evaluate("""() => addEventListener('pointerdown', (e) => { window.ocTapTarget = e.target.closest('a')
            ? 'link' : e.target.closest('button') ? 'button' : e.target.tagName; }, true)""")
        # The Folders section slides open (a Svelte transition, a CSS animation): a tap while it runs
        # lands on the folder's link and the ⋯ never takes the click (measured). Tap once it is done.
        page.wait_for_function("""(id) => { for (let e = document.getElementById(id); e; e = e.parentElement)
            if (e.getAnimations().length) return false; return true; }""", arg=f"folder-{folder}-button", timeout=WAIT_MS)
        row.locator("button[aria-haspopup]").first.tap(force=True)   # where the ⋯ is drawn
        menu = page.locator("[role=menu]").first
        menu.wait_for(timeout=WAIT_MS)
        page.wait_for_function("() => window.ocPushFolderMenuContract === 'ok'", timeout=WAIT_MS)
        assert menu.evaluate("(m) => m.dataset.ocPush") == "seen"
        # The contract is known at once; the items come once the user's folders are read (a request).
        menu.locator("[data-oc-push-item=mute]").wait_for(timeout=WAIT_MS)
        assert menu.evaluate(ITEMS) == ["New Chat", "Create Folder", "Mark all as read", "Mute folder",
                                        "Edit", "Export", "Delete"]
    finally:
        ctx.close()
        put_state(token, {"subscriptions": saved})


def test_mute_from_open_webuis_own_menus(owui, browser):
    """G: Mute in a chat's ⋯ and a folder's ⋯ (sidebar and folder page), with the DOM contract;
    H3: a folder's name is isolated in the menu's sentences."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 844})
    name = "מסמכים חשובים של הבית והמשפחה " + uuid.uuid4().hex[:4]   # long, right to left
    folder = api("/api/v1/folders/", {"name": name}, token)["id"]
    g_chat, _ = new_chat(token, "stub", "Chat G")
    api(f"/api/v1/chats/{g_chat}/folder", {"folder_id": folder}, token)
    page.goto(BASE + "/")
    assert ready(page) in ("kept", "saved")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    if page.locator("#sidebar-open-button").is_visible():
        page.locator("#sidebar-open-button").click()
    row = page.locator(f"#folder-{folder}-button")
    if not row.is_visible():
        page.get_by_text("Folders", exact=True).first.click()
    row.wait_for(timeout=WAIT_MS)
    try:
        # The folder's ⋯ in the sidebar: "Mute folder" right after "Mark all as read", native items untouched.
        menu = native_menu(page, FOLDER_ROW_TRIGGER, folder)
        shot(page, "web-push-g-folder-menu")
        item = menu.get_by_role("menuitem", name="Mute folder")
        item.wait_for(timeout=WAIT_MS)
        items = menu.evaluate(ITEMS)
        assert items.index("Mute folder") == items.index("Mark all as read") + 1, items
        assert [i for i in items if i != "Mute folder"] == ["New Chat", "Create Folder", "Mark all as read", "Edit", "Export", "Delete"], items
        assert item.evaluate("(b) => b.className === b.previousElementSibling.className")
        # Open WebUI redraws the menu while it is open: ours comes back.
        menu.evaluate("(m) => m.querySelectorAll('[data-oc-push-item]').forEach(e => e.remove())")
        menu.get_by_role("menuitem", name="Mute folder").wait_for(timeout=WAIT_MS)
        menu.get_by_role("menuitem", name="Mute folder").click()
        mm = page.locator("#oc-push-mute-menu")
        mm.wait_for(timeout=WAIT_MS)
        # Ours closes Open WebUI's menu, as its own items do, and the focus is on the first choice.
        page.wait_for_function("() => !document.querySelector('[role=menu]:not(#oc-push-mute-menu)')", timeout=WAIT_MS)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        assert page.evaluate("() => document.activeElement === document.querySelector('#oc-push-mute-menu [role=menuitem]')")
        # H3: the folder's name is isolated in the sentence.
        assert mm.locator("bdi").first.inner_text() == name
        shot(page, "web-push-g-folder-mute-menu")
        mm.get_by_role("menuitem", name="Mute folder always").click()
        page.wait_for_function(f"() => window.ocPushMuted?.id === {json.dumps(folder)}", timeout=WAIT_MS)
        assert json.loads(state(token)["folder_mutes"]) == {folder: 0}
        # Muted: the item says so the next time.
        menu = native_menu(page, FOLDER_ROW_TRIGGER, folder)
        menu.get_by_role("menuitem", name="Muted").wait_for(timeout=WAIT_MS)
        close_menus(page)

        # The folder's own page: its ⋯ gets the item too.
        page.locator(f'a[href="/folders/{folder}"]').click()
        page.wait_for_function(f"() => location.pathname === '/folders/{folder}'", timeout=WAIT_MS)   # in the app, no load
        page_trigger = ("[...document.querySelectorAll('button[aria-haspopup]')].find(b => !b.closest('#sidebar') "
                        "&& !b.closest('#message-input-container') && b.getAttribute('aria-label') === 'Folder options')")
        page.wait_for_function(f"() => !!({page_trigger})", timeout=WAIT_MS)
        menu = native_menu(page, page_trigger, None)
        menu.get_by_role("menuitem", name="Muted").wait_for(timeout=WAIT_MS)
        close_menus(page)

        # The chat's ⋯ in the sidebar: "Mute notifications" after Mark as read; it opens the mute menu with the folder.
        page.goto(BASE + "/")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        if not page.locator(f'a[href="/c/{g_chat}"]').is_visible():
            row.locator("button[aria-expanded]").first.click()
        page.locator(f'a[href="/c/{g_chat}"]').wait_for(timeout=WAIT_MS)
        menu = native_menu(page, CHAT_TRIGGER, g_chat)
        shot(page, "web-push-g-chat-menu")
        item = menu.locator("[data-oc-push-item=mute]")
        item.wait_for(timeout=WAIT_MS)
        items = menu.evaluate(ITEMS)
        assert items.index("Mark as read") == items.index("Mark as unread") + 1, items
        assert items[items.index("Mark as read") + 1] == item.inner_text().strip(), items
        assert item.inner_text().strip().startswith("Muted"), "the chat is in a muted folder"
        item.click()
        mm.wait_for(timeout=WAIT_MS)
        page.wait_for_function("() => !document.querySelector('[role=menu]:not(#oc-push-mute-menu)')", timeout=WAIT_MS)
        assert "This chat" in mm.inner_text() and mm.locator("bdi").count() >= 1
        mm.get_by_role("menuitem", name="Mute for 1 hour").click()
        page.wait_for_function(f"() => window.ocPushMuted?.id === {json.dumps(g_chat)}", timeout=WAIT_MS)
        assert abs(json.loads(state(token)["mutes"])[g_chat] - (time.time() + 3600)) < 60
        # Moved to another folder: the chat's menu offers the new folder, not the one it was in.
        second = api("/api/v1/folders/", {"name": "Second " + uuid.uuid4().hex[:4]}, token)
        api(f"/api/v1/chats/{g_chat}/folder", {"folder_id": second["id"]}, token)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        if not page.locator(f'a[href="/c/{g_chat}"]').count():   # the sidebar redrew it into the new folder
            page.locator(f'#folder-{second["id"]}-button button[aria-expanded]').first.click()
            page.locator(f'a[href="/c/{g_chat}"]').wait_for(timeout=WAIT_MS)
        menu = native_menu(page, CHAT_TRIGGER, g_chat)
        menu.locator("[data-oc-push-item=mute]").click()
        mm.wait_for(timeout=WAIT_MS)
        assert second["name"] in mm.inner_text() and name not in mm.inner_text(), mm.inner_text()
        close_menus(page)

        # Fail safe: a folder ⋯ whose menu is not Open WebUI 0.13's gets nothing, one console line.
        logs = []
        page.on("console", lambda m: logs.append(m.text))
        page.evaluate("""() => { const row = document.createElement('div'); row.id = 'folder-fake-button';
            row.innerHTML = '<button aria-haspopup="true">⋯</button>'; document.body.appendChild(row);
            for (const id of ['bad-folder', 'bad-folder-2']) {
              row.querySelector('button').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}));
              const m = document.createElement('div'); m.setAttribute('role', 'menu'); m.id = id;
              m.innerHTML = '<button role="menuitem">Something</button>'; document.body.appendChild(m);
            } }""")
        page.wait_for_function("() => window.ocPushFolderMenuContract === 'missing'", timeout=WAIT_MS)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        assert page.locator("#bad-folder [role=menuitem], #bad-folder-2 [role=menuitem]").count() == 2
        assert len([l for l in logs if "folder menu" in l]) == 1, logs
        page.evaluate("() => document.querySelectorAll('#bad-folder, #bad-folder-2, #folder-fake-button').forEach(e => e.remove())")
    finally:
        put_state(token, {"mutes": "{}", "folder_mutes": "{}"})
        page.set_viewport_size({"width": 390, "height": 844})

def test_the_mute_menu_on_top_with_a_gutter_and_the_day(owui, browser):
    """H2: an Open WebUI toast over the bell's menu does not take its taps; H4: on a phone the menu keeps
    a gutter at both edges; H5: a mute that ends on another day says which day."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    page.set_viewport_size({"width": 1280, "height": 844})
    g_chat, _ = new_chat(token, "stub", "Chat H")
    # A long folder name makes the menu's items long (on a phone the menu was flush with the left edge then).
    long_folder = api("/api/v1/folders/", {"name": "תיקייה עם שם ארוך מאוד של פרויקט הבית " + uuid.uuid4().hex[:4]}, token)["id"]
    api(f"/api/v1/chats/{g_chat}/folder", {"folder_id": long_folder}, token)
    mm = page.locator("#oc-push-mute-menu")
    broken = []   # all three are checked, so a run names each one that breaks
    try:
        # H2: a toast of Open WebUI over the menu does not take its taps (the menu is in the top layer).
        page.goto(f"{BASE}/c/{g_chat}")
        bell = page.locator("#oc-push-bell")
        bell.wait_for(timeout=WAIT_MS)
        bell.evaluate("(b) => b.click()")
        mm.wait_for(timeout=WAIT_MS)
        # Added after the menu, so only the top layer keeps the menu above it.
        page.evaluate("""() => { const t = document.createElement('div'); t.id = 'fake-toast';
            t.style.cssText = 'position:fixed;inset:0 0 auto 0;height:60vh;z-index:2147483647;background:#f008';
            document.body.appendChild(t); }""")
        shot(page, "web-push-h2-toast")
        if not mm.evaluate("""(m) => { const i = m.querySelector('[role=menuitem]'); const r = i.getBoundingClientRect();
            return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) === i; }"""):
            broken.append("H2: the toast covers the menu")
        close_menus(page)
        page.evaluate("() => document.getElementById('fake-toast').remove()")
        # Escape closes it, and the next press on the bell opens it again.
        bell.click()
        mm.wait_for(timeout=WAIT_MS)
        page.keyboard.press("Escape")
        mm.wait_for(state="detached", timeout=WAIT_MS)
        bell.click()
        try:
            mm.wait_for(timeout=MENU_DRAWN_WAIT_MS * 4)
            close_menus(page)
        except Exception:  # noqa: BLE001 - listed below
            broken.append("Escape: the next bell press did not open the menu")

        # H5: a mute that ends on another day says which day.
        later = int(time.time()) + 2 * 86400
        put_state(token, {"mutes": json.dumps({g_chat: later}), "folder_mutes": "{}"})
        page.goto(f"{BASE}/c/{g_chat}")
        page.wait_for_function("() => (document.getElementById('oc-push-bell')?.title || '').startsWith('Muted until')", timeout=WAIT_MS)
        day = page.evaluate("(t) => new Date(t * 1000).toLocaleString([], {weekday: 'short'})", later)
        title = page.locator("#oc-push-bell").get_attribute("title")
        if day not in title:
            broken.append(f"H5: no day ({day}) in {title!r}")

        # H4: on a phone the menu keeps a gutter at both edges.
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{BASE}/c/{g_chat}")
        bell.wait_for(timeout=WAIT_MS)
        bell.click()
        box = mm.bounding_box()
        shot(page, "web-push-h4-h5-phone")
        if not (box["x"] >= 8 and box["x"] + box["width"] <= 390 - 8):
            broken.append(f"H4: no gutter, menu at {box}")
        close_menus(page)
        # A short window (a phone on its side): the menu stays inside it and scrolls.
        page.set_viewport_size({"width": 390, "height": 260})
        bell.click()
        box = mm.bounding_box()
        if not (box["y"] >= 8 and box["y"] + box["height"] <= 260 - 8):
            broken.append(f"short window: menu out of it, at {box}")
        close_menus(page)
        assert not broken, broken
    finally:
        put_state(token, {"mutes": "{}", "folder_mutes": "{}"})
        page.set_viewport_size({"width": 390, "height": 844})


OFFLINE_CACHES = "() => caches.keys()"


def warm(page, chat):
    """The chat opened online under the service worker, so its reads and files are kept."""
    page.goto(f"{BASE}/c/{chat}")
    controlled(page)
    page.reload()   # the first load ran before the worker controlled the page
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    settled(page, """(c) => caches.keys().then(async (ks) => {
        for (const k of ks.filter((k) => k.startsWith('oc-api-')))
            if (await (await caches.open(k)).match(location.origin + '/api/v1/chats/' + c)) return true;
        return false; })""", chat)


def test_offline_open_shows_the_chat_read_last(owui, browser):
    """ELI-352 phase 1: with no network, a reload opens the app and the chat read last, read-only."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    o_chat, o_msg = new_chat(token, "stub", "Chat Offline")
    reply(token, "stub", o_chat, o_msg)
    wait_done(token, o_chat, o_msg)
    text = [m for m in api(f"/api/v1/chats/{o_chat}", token=token)["chat"]["history"]["messages"].values()
            if m["role"] == "assistant"][0]["content"][:40]
    warm(page, o_chat)
    ctx.set_offline(True)
    try:
        page.reload(timeout=WAIT_MS)
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_text(text.strip().splitlines()[0][:30], exact=False).first.wait_for(timeout=WAIT_MS)
        if os.environ.get("E2E_SHOTS"):
            page.screenshot(path=os.path.join(os.environ["E2E_SHOTS"], "web-push-offline-chat.png"))
    finally:
        ctx.set_offline(False)


def test_offline_keeps_no_redirect_and_drops_an_old_builds_files(owui, browser):
    """A redirect (an expired Cloudflare Access session) is never kept as the app's page; a new page
    (a new Open WebUI build) drops the hashed files of the old one."""
    ctx, page = browser
    token = owui
    page.bring_to_front()
    r_chat, _ = new_chat(token, "stub", "Chat Redirect")
    warm(page, r_chat)
    kept = "() => caches.open('oc-shell-v1').then((c) => c.match('/')).then((r) => r ? r.text() : '')"
    page_before = page.evaluate(kept)
    assert "/_app/immutable/" in page_before
    # Access's answer to an expired session: a redirect to its login page.
    login = "<html><body>Cloudflare Access login</body></html>"
    ctx.route("**/access-login", lambda r: r.fulfill(status=200, content_type="text/html", body=login))
    to_login = lambda r: r.fulfill(status=302, headers={"location": f"{BASE}/access-login"})
    # Access answers every request of an expired session so, /api/version too.
    ctx.route(f"**/c/{r_chat}", to_login)
    ctx.route("**/api/version", to_login)
    try:
        page.goto(f"{BASE}/c/{r_chat}")
        page.wait_for_url("**/access-login", timeout=WAIT_MS)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)
        assert "Access login" not in page.evaluate(kept)
    finally:
        ctx.unroute(f"**/c/{r_chat}"); ctx.unroute("**/api/version"); ctx.unroute("**/access-login")
    # An old build's file and the page it came with; the next page load drops it.
    page.goto(f"{BASE}/c/{r_chat}")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.evaluate("""() => caches.open('oc-shell-v1').then(async (c) => {
        await c.put('/_app/immutable/old-build.js', new Response('// old'));
        await c.put('/_oc/page-files', new Response('/_app/immutable/old-build.js')); })""")
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    settled(page, """() => caches.open('oc-shell-v1').then((c) => c.match('/_app/immutable/old-build.js'))
        .then((r) => !r)""")


# --- ELI-352 PR 2: the error page after a network change, a newer Open WebUI, the
# offline pill, another user on the same browser, the kill switch.

APP_FILE = "**/_app/immutable/nodes/0.*"
CUT_ATTEMPTS = 2   # the service worker asks a read cut on its way once more (oc-sw.js again())   # Open WebUI's root layout: without it the app cannot start
ERROR_PAGE = "() => !!document.querySelector('body > .error > .status')"
RELOADED_FLAG = "() => sessionStorage.getItem('ocReloadedAfterError')"
# The kept copy of that file (offline open on) would hide a network failure: the test drops it.
DROP_APP_FILE = """() => caches.open('oc-shell-v1').then(async (c) => {
    for (const k of await c.keys()) if (k.url.includes('/nodes/0.')) await c.delete(k); })"""
SHELL_VERSION = "() => caches.open('oc-shell-v1').then((c) => c.match('/_oc/version')).then((r) => r ? r.text() : null)"
# Offline open's caches (oc-sw.js SHELL, API_PREFIX); the push side's own (oc-push-*: an unsent
# reply, the notifications it closed) are not offline open's and stay.
NO_OFFLINE_CACHES = "() => caches.keys().then((ks) => !ks.some((k) => k.startsWith('oc-shell-') || k.startsWith('oc-api-')))"


def offline_on():
    """The stack's Worker serves oc-sw.js with offline open on (E2E_OFFLINE, default 1)."""
    with urllib.request.urlopen(BASE + "/oc-sw.js", timeout=30) as r:
        return 'OFFLINE_SETTING = "1"' in r.read().decode()


needs_offline = pytest.mark.skipif("not offline_on()", reason="offline open is off on this stack (E2E_OFFLINE=0)")
needs_offline_off = pytest.mark.skipif("offline_on()", reason="offline open is on on this stack (E2E_OFFLINE=1)")


def chats_on():
    """The stack's Worker serves oc-sw.js with kept chats on (E2E_CHAT_CACHE, default 1; with offline open)."""
    with urllib.request.urlopen(BASE + "/oc-sw.js", timeout=30) as r:
        sw = r.read().decode()
    return 'OFFLINE_SETTING = "1"' in sw and 'CHAT_CACHE_SETTING = "1"' in sw


needs_chats = pytest.mark.skipif("not chats_on()", reason="kept chats are off on this stack (E2E_CHAT_CACHE=0)")


def opened(page, chat):
    """The chat opened under the service worker (both modes)."""
    page.goto(f"{BASE}/c/{chat}")
    controlled(page)
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)


def page_loads(page):
    """The page's loads from here on (each navigation request of its main frame)."""
    loads = []
    def seen(r):
        if r.is_navigation_request() and r.frame == page.main_frame:
            loads.append(r.url)
    page.on("request", seen)
    return loads, lambda: page.remove_listener("request", seen)


def test_the_error_page_after_a_network_change_reloads_once_into_the_chat(owui, browser):
    """A network change while the app loads fails one of Open WebUI's files and SvelteKit shows
    "500 Internal Error" for good. The page script reloads once, the server being reachable, and
    the chat opens; a load that gets to the app clears the guard."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Network Change")
    opened(page, chat)
    failed = []
    def fail_once(route):
        # The first load's request fails: the page's own, or (offline open on) the service
        # worker's and the one retry it makes.
        mine = [k for k in failed if k == bool(route.request.service_worker)]
        if len(mine) >= (CUT_ATTEMPTS if route.request.service_worker else 1):
            return route.continue_()
        failed.append(bool(route.request.service_worker))
        route.abort("internetdisconnected")   # what ERR_NETWORK_CHANGED does to the import
    loads, stop = page_loads(page)
    page.evaluate(DROP_APP_FILE)
    ctx.route(APP_FILE, fail_once)
    try:
        page.reload()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        assert failed, "Open WebUI's file never failed"
        assert len(loads) == 2, loads   # the load that failed, the one reload
        page.wait_for_function(f"() => ({RELOADED_FLAG})() === null", timeout=WAIT_MS)
    finally:
        ctx.unroute(APP_FILE, fail_once)
        stop()


BOOT_READS = ("**/api/v1/users/user/settings", "**/api/v1/chats/?page=1", "**/api/v1/chats/pinned")
PINNED_PATH = "/api/v1/chats/pinned"
KEPT_APP = "oc-shell-v1"   # oc-sw.js SHELL
NO_API_CACHES = "() => caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith('oc-api-')).map((k) => caches.delete(k))))"


@needs_offline
def test_a_network_change_during_the_first_load_still_opens_the_chat(owui, browser):
    """A change of network fails every request on its way (ERR_NETWORK_CHANGED). On a load with
    nothing kept yet, Open WebUI's own reads failing leave it half drawn for good (no chat, no
    error page): the service worker asks a failed read once more, and the chat opens."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Changed Network")
    opened(page, chat)
    page.evaluate(NO_API_CACHES)   # nothing kept: as on the first load the worker handles
    failed = []
    def fail_once(route):
        if route.request.service_worker is None or route.request.url in failed:
            return route.fallback()
        failed.append(route.request.url)
        route.abort("connectionreset")   # a request cut on its way, as a network change does
    for r in BOOT_READS:
        ctx.route(r, fail_once)
    try:
        page.reload()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_text("Chat Changed Network").first.wait_for(timeout=WAIT_MS)   # the chat list too
        assert len(failed) == len(BOOT_READS), failed
    finally:
        for r in BOOT_READS:
            ctx.unroute(r, fail_once)


@needs_offline_off
def test_start_reads_cut_by_a_network_change_still_open_the_chat(owui, pw):
    """Offline open off (the worker answers nothing): Open WebUI's start reads cut once on their
    way (a change of network) are asked once more by the page script, and the chat and the chat
    list open. Its own browser: nothing kept from other tests."""
    chat, _ = new_chat(owui, "stub", "Chat Start Cut")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        cut = []
        def cut_once(route):
            if route.request.method != "GET" or route.request.url in cut:
                return route.fallback()
            cut.append(route.request.url)
            route.abort("connectionreset")   # a request cut on its way, as a network change does
        for r in BOOT_READS:
            ctx.route(r, cut_once)
        try:
            page.reload()
            page.locator("#chat-input").wait_for(timeout=WAIT_MS)
            sidebar_link(page, chat).wait_for(timeout=WAIT_MS)   # the chat list's entry
            assert len(cut) == len(BOOT_READS), cut
        finally:
            for r in BOOT_READS:
                ctx.unroute(r, cut_once)
    finally:
        ctx.close()


# Counts the calls of fetch for a path made from the outside (Open WebUI's) and their ends. The
# page script and Open WebUI each wrap window.fetch: whoever reads it gets the top wrapper at that
# time, and a call counts only when it goes to the top one, not from one wrapper to the next.
OUTER_FETCH = """(path) => {
    const seen = window.__ocOuter = {calls: 0, ends: 0, sent: 0};
    const layers = [window.fetch];
    const view = (k) => function (input, init) {
        const p = layers[k].call(this, input, init);
        if (new URL(input instanceof Request ? input.url : String(input), location.href).pathname !== path) return p;
        if (k === 0) seen.sent++;   // the browser's own fetch: a request the page sends
        if (k !== layers.length - 1) return p;
        seen.calls++;
        return p.finally(() => { seen.ends++; });
    };
    Object.defineProperty(window, 'fetch', {configurable: true, get: () => view(layers.length - 1),
                                            set: (f) => { layers.push(f); }});
}"""


HOLD_INSTALL = "oc-e2e-hold-install"   # e2e/sw_inflight.js: a worker installing waits while this cache exists
FIRST_VERSION_ID = "0"   # Chrome's id for a fresh profile's first version of the worker (CDP)
HELPER_PATH = "/manifest.json"   # any page of the site
INSTALL_GO = """() => navigator.serviceWorker.getRegistration('/').then((r) =>
    r && r.installing && r.installing.postMessage({type: 'oc-e2e-install-go'}))"""


def test_a_page_reloaded_before_its_worker_activated_comes_under_it(owui, pw):
    """A page reloaded while its worker installs (Open WebUI's app failing on a fresh device's first
    load, and the page script reloading it, ELI-352): the reload leaves before the worker is active,
    and its page comes after the worker claimed the pages open. No claim reaches it: only its next
    navigation is controlled, and controlled() must get there (the old helper waited for a claim
    that never comes). The order is made here: the worker's install waits (e2e/sw_inflight.js) until
    the reload is on its way, and the reload waits until the worker is active."""
    chat, _ = new_chat(owui, "stub", "Chat Reloaded While Installing")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    ctx.add_init_script(f"caches.open({json.dumps(HOLD_INSTALL)})")
    reload, status = [], {}
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    def reload_of_the_page(route):
        if not route.request.is_navigation_request() or route.request.service_worker is not None or reload:
            return route.fallback()
        reload.append(route)   # let go from the test, not in this handler
    tab = ctx.new_page()   # Chrome's account of the worker (CDP), beside the page
    helper = ctx.new_page()   # a page of the site that is not reloading
    cdp = ctx.new_cdp_session(tab)
    cdp.on("ServiceWorker.workerVersionUpdated", lambda ev: status.update(
        {v["versionId"]: v["status"] for v in ev["versions"]}))
    cdp.send("ServiceWorker.enable")
    def until(state, past=()):
        # Waits for state, and fails at once when the worker is already in a status past it (the
        # state can no longer come), with what was seen.
        end = time.time() + WAIT_MS / 1000
        while time.time() < end and not state() and status.get(FIRST_VERSION_ID) not in past:
            tab.wait_for_timeout(STATE_POLL_S * 1000)   # routes and CDP events arrive while Playwright waits
        assert state(), (reload, status)
    try:
        page.goto(f"{BASE}/c/{chat}")
        # Installed or later here: the install was not held, and nothing below is.
        until(lambda: status.get(FIRST_VERSION_ID) == "installing", past=("installed", "activating", "activated", "redundant"))
        ctx.route(f"**/c/{chat}", reload_of_the_page)
        page.evaluate("() => { setTimeout(() => location.reload()); }")   # after this evaluate returns
        until(lambda: reload, past=("installed", "activating", "activated", "redundant"))
        # The go comes from another page of the site: the page itself is between two documents now
        # (Playwright would wait for the held one), and only the go lets that one come.
        helper.goto(BASE + HELPER_PATH)
        helper.evaluate(INSTALL_GO)
        until(lambda: status.get(FIRST_VERSION_ID) == "activated", past=("redundant",))   # it claimed the page still open
        reload.pop().fallback()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        assert not page.evaluate("() => !!navigator.serviceWorker.controller")   # the case: not claimed
        controlled(page)
    finally:
        # A failure lets go of what it holds: the install, and the reload.
        if helper.url.startswith(BASE):
            helper.evaluate(INSTALL_GO)
        for r in reload:
            r.fallback()
        ctx.close()


@needs_offline
def test_a_start_read_the_worker_gave_up_on_is_not_asked_again_by_the_page(owui, pw):
    """Offline open on: the worker already asks a cut start read once more (oc-sw.js again()). A
    read that fails both times is final: the page script does not ask it again (two more
    attempts through the worker)."""
    chat, _ = new_chat(owui, "stub", "Chat Start Cut Twice")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    ctx.add_init_script(f"({OUTER_FETCH})({json.dumps(PINNED_PATH)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        controlled(page)
        page.reload()   # a load the worker answers: the app is kept
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        settled(page, f"() => caches.has({json.dumps(KEPT_APP)})")
        page.evaluate(NO_API_CACHES)   # no kept answer to fall back on: the failure reaches the page
        attempts = []
        def cut_always(route):
            if route.request.method != "GET":
                return route.fallback()
            attempts.append(bool(route.request.service_worker))
            route.abort("connectionreset")
        ctx.route("**" + PINNED_PATH, cut_always)
        try:
            page.reload()
            page.locator("#chat-input").wait_for(timeout=WAIT_MS)   # (the pinned list failing for good, no chat list)
            # Every call Open WebUI made has its answer (it may ask the list more than once).
            page.wait_for_function("() => window.__ocOuter.ends >= 1 && window.__ocOuter.ends === window.__ocOuter.calls",
                                   timeout=WAIT_MS)
            calls, sent = page.evaluate("() => [window.__ocOuter.calls, window.__ocOuter.sent]")
            # One request per call (the page asked none again), each one asked twice by the worker
            # alone. Calls on their way together share one request (oc-sw.js shared()).
            assert sent == calls, (calls, sent)
            assert attempts and all(attempts), attempts
            assert len(attempts) % CUT_ATTEMPTS == 0 and len(attempts) <= CUT_ATTEMPTS * calls, (calls, attempts)
        finally:
            ctx.unroute("**" + PINNED_PATH, cut_always)
    finally:
        ctx.close()


def test_the_error_page_reloads_only_once(owui, browser):
    """The reload fails too: the page stays on the error page, and neither 'online' nor a
    network change reloads it again."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Error Twice")
    opened(page, chat)
    fail = lambda route: route.abort("internetdisconnected")
    loads, stop = page_loads(page)
    page.evaluate(DROP_APP_FILE)
    ctx.route(APP_FILE, fail)
    try:
        page.reload()
        # The one reload, then its error page (the first error page ends in that reload).
        end = time.time() + WAIT_MS / 1000
        while len(loads) < 2:
            assert time.time() < end, "the page was never reloaded"
            page.wait_for_timeout(STATE_POLL_S * 1000)   # a poll; Playwright delivers the page's events meanwhile
        page.wait_for_function(f"() => ({ERROR_PAGE})() && ({RELOADED_FLAG})() === '1'", timeout=WAIT_MS)
        page.evaluate("() => { window.ocStayed = true; dispatchEvent(new Event('online')); }")
        assert page.evaluate("() => window.ocStayed === true")
        assert len(loads) == 2, loads
    finally:
        ctx.unroute(APP_FILE, fail)
        stop()
    opened(page, chat)   # a load that gets to the app clears the guard
    page.wait_for_function(f"() => ({RELOADED_FLAG})() === null", timeout=WAIT_MS)


# Longer than the navigation's 3 s network wait before this PR: with it, the kept app
# of the old version was used.
UPGRADED_PAGE_DELAY_S = 5
UPGRADED_VERSION = '{"version":"99.0.0-e2e","deployment_id":"e2e-upgrade"}'
UPGRADED_MARK = '<meta name="oc-e2e-build" content="upgraded">'


@needs_offline
def test_a_newer_open_webui_never_runs_the_kept_app(owui, browser):
    """The server is a newer Open WebUI (another /api/version) and its page is slow: the kept
    app of the old version is not used, the caches go, and the newer page opens and is kept
    with its version. Back on the old server, the same again the other way."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Upgrade")
    warm(page, chat)
    real = urllib.request.urlopen(OWUI + "/api/version", timeout=30).read().decode()
    html = urllib.request.urlopen(OWUI + "/", timeout=30).read().decode().replace("<head>", "<head>" + UPGRADED_MARK, 1)
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(UPGRADED_PAGE_DELAY_S)   # the upgraded server's slow page
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from_worker = []
    def upgraded_page(route):
        if route.request.service_worker is None:
            return route.fallback()
        from_worker.append(route.request.url)
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}/")
    def upgraded_version(route):
        if route.request.service_worker is None:
            return route.fallback()
        route.fulfill(status=200, content_type="application/json", body=UPGRADED_VERSION)
    ctx.route(f"**/c/{chat}", upgraded_page)
    ctx.route("**/api/version", upgraded_version)
    try:
        page.goto(f"{BASE}/c/{chat}", timeout=WAIT_MS)
        assert from_worker, "the service worker never asked the server for the page"
        # The kept app may open first; told the version differs, the page reloads onto the newer one.
        page.wait_for_function("() => !!document.querySelector('meta[name=oc-e2e-build]')", timeout=WAIT_MS)
        settled(page, f"() => ({SHELL_VERSION})().then((v) => v === {json.dumps(UPGRADED_VERSION)})")
    finally:
        ctx.unroute(f"**/c/{chat}", upgraded_page)
        ctx.unroute("**/api/version", upgraded_version)
        srv.shutdown()
    # The real server again: its version differs from the kept one, so its own page.
    page.goto(f"{BASE}/c/{chat}", timeout=WAIT_MS)
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.wait_for_function("() => !document.querySelector('meta[name=oc-e2e-build]')", timeout=WAIT_MS)
    settled(page, f"() => ({SHELL_VERSION})().then((v) => v === {json.dumps(real)})")


def sw_const(name):
    return int(re.search(rf"^const {name} = (\d+);$", open(os.path.join(ROOT, "web", "oc-sw.js")).read(), re.M).group(1))


# How long the worker before ELI-352 waited for the network before it gave the kept app (oc-sw.js LIE_FI_MS).
LIE_FI_MS = sw_const("SLOW_3G_SLOWEST_MS") * sw_const("SILENT_FACTOR")
NAV_ANSWER_MS = "() => { const n = performance.getEntriesByType('navigation')[0]; return n.responseStart - n.fetchStart; }"


@needs_offline
def test_the_kept_app_opens_without_waiting_for_the_version(owui, browser):
    """The kept app (kept with its version) opens at once: the server's /api/version is asked
    alongside it, not before it."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Instant")
    warm(page, chat)
    real = urllib.request.urlopen(OWUI + "/api/version", timeout=30).read()
    # The network answers only once the app is drawn: the app can never be waiting on it. Whether the
    # worker waited is its answer's time to the page, not the drawing's (that one grows with the host's load).
    drawn = threading.Event()
    class SlowVersion(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            drawn.wait(WAIT_MS / 1000)
            body, kind = (real, "application/json") if self.path == "/api/version" else (page_html, "text/html")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowVersion)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page_html = urllib.request.urlopen(OWUI + "/", timeout=30).read()
    def slow(route):
        if route.request.service_worker is None:
            return route.fallback()
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}{urllib.parse.urlsplit(route.request.url).path}")
    # A slow network: the page and the version both slow (a fast page alone would be used as it comes).
    ctx.route("**/api/version", slow)
    ctx.route(f"**/c/{chat}", slow)
    try:
        page.reload(wait_until="commit")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        waited = page.evaluate(NAV_ANSWER_MS)
        assert waited < LIE_FI_MS, f"the worker waited {waited:.0f} ms for the network"
    finally:
        drawn.set()
        ctx.unroute("**/api/version", slow)
        ctx.unroute(f"**/c/{chat}", slow)
        srv.shutdown()


def test_a_load_reads_the_users_valves_once(owui, pw):
    """Open WebUI navigates a few times while it starts; the page script reads the user's valves
    (mutes, devices) once for all of them. Notifications not asked yet (no subscription to save)."""
    chat, _ = new_chat(owui, "stub", "Chat One Read")
    body = json.dumps(api(VALVES_READ, token=owui)).encode()
    reads, answered = [], threading.Event()
    def answer(path):
        reads.append(path)
        answered.set()
        return body, "application/json"
    srv = slow_server(answer)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        # A slow answer (a slow network): Open WebUI's start navigations come while it is on its way.
        ctx.route(f"**{VALVES_READ}", lambda route: route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}/")
                  if route.request.method == "GET" else route.fallback())
        page.reload()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        assert answered.wait(WAIT_MS / 1000), "the page script never read the valves"
        # Open WebUI's start navigations all came while that first read was on its way.
        assert len(reads) == 1, reads
    finally:
        ctx.close()
        srv.shutdown()


SLOW_ANSWER_S = 2   # a Slow 3G round trip


def slow_server(answer, delay=SLOW_ANSWER_S, gate=None):
    """A local server whose answers come late (or, with `gate`, only once it is set)."""
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if gate is not None:
                gate.wait(WAIT_MS / 1000)
            else:
                time.sleep(delay)
            body, kind = answer(self.path)
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@needs_offline
def test_the_kept_app_runs_while_the_version_check_gets_no_answer(owui, browser):
    """A network that says nothing (lie-fi): the kept app opens and draws the chat; the version
    check stays pending beside it, and nothing in the app waits for it."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Silent")
    warm(page, chat)
    silent = threading.Event()
    # Let go at the end with the server's own version, as the kept app's: any other answer is a
    # new Open WebUI to the worker, which drops every cache and reloads the page into the next test.
    with urllib.request.urlopen(BASE + "/api/version", timeout=30) as r:
        same = r.read()
    srv = slow_server(lambda path: (same, "application/json"), gate=silent)
    def no_answer(route):
        if route.request.service_worker is None:
            return route.fallback()
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}/api/version")
    ctx.route("**/api/version", no_answer)
    try:
        page.reload(wait_until="commit")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_text("Chat Silent").first.wait_for(timeout=WAIT_MS)
        assert not silent.is_set()
    finally:
        ctx.unroute("**/api/version", no_answer)
        silent.set()
        srv.shutdown()


@needs_offline
def test_a_login_instead_of_a_version_drops_a_page_kept_without_one(owui, browser):
    """A page kept without its version (by an earlier worker) and an expired Access session: the
    login shows, and the kept page goes."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Old Kept")
    warm(page, chat)
    page.evaluate("() => caches.open('oc-shell-v1').then((c) => c.delete('/_oc/version'))")
    login = "<html><body>Cloudflare Access login</body></html>"
    to_login = lambda r: r.fulfill(status=302, headers={"location": f"{BASE}/access-login"})
    ctx.route("**/access-login", lambda r: r.fulfill(status=200, content_type="text/html", body=login))
    ctx.route(f"**/c/{chat}", to_login)
    ctx.route("**/api/version", to_login)
    try:
        page.goto(f"{BASE}/c/{chat}")
        page.wait_for_url("**/access-login", timeout=WAIT_MS)
        settled(page, "() => caches.open('oc-shell-v1').then((c) => c.match('/')).then((r) => !r)")
    finally:
        ctx.unroute(f"**/c/{chat}"); ctx.unroute("**/api/version"); ctx.unroute("**/access-login")


MODEL_AVATARS = "img[src*='/models/model/profile/image']"
AVATARS_DRAWN = f"""() => {{ const i = [...document.querySelectorAll("{MODEL_AVATARS}")];
    return i.length > 0 && i.every((x) => x.complete && x.naturalWidth > 0); }}"""
COMPOSER_LOCKED = "() => !!document.getElementById('chat-input')?.closest('form')?.inert"
# Everything on the message box or above it that keeps a tap from raising a phone's keyboard
# (inert, readonly, disabled, inputmode none, not editable): [] when the box takes typing.
BOX_LOCKS = """() => { const b = document.getElementById('chat-input'); if (!b) return ['no box'];
  const why = []; if (!b.isContentEditable) why.push('not editable'); if (b.editor && !b.editor.isEditable) why.push('editor off');
  for (let e = b; e; e = e.parentElement) for (const n of ['inert', 'readonly', 'disabled', 'aria-readonly', 'aria-disabled'])
    if (e.hasAttribute(n) || (n === 'inert' && e.inert)) why.push((e.id || e.tagName) + ' ' + n);
  for (let e = b; e; e = e.parentElement) if (e.getAttribute('inputmode') === 'none') why.push((e.id || e.tagName) + ' inputmode none');
  return why; }"""
# From now on, every lock BOX_LOCKS looks for that is set on the box or above it, even if taken off again.
WATCH_LOCKS = """() => { window.__ocLocks = []; const names = ['inert', 'readonly', 'disabled', 'inputmode', 'contenteditable', 'aria-readonly', 'aria-disabled'];
  new MutationObserver((ms) => { const b = document.getElementById('chat-input');
    for (const m of ms) if (b && (m.target === b || m.target.contains(b)) && m.target.hasAttribute(m.attributeName)
        && !(m.attributeName === 'contenteditable' && m.target.getAttribute('contenteditable') === 'true'))
      window.__ocLocks.push((m.target.id || m.target.tagName) + ' ' + m.attributeName + '=' + m.target.getAttribute(m.attributeName)); })
    .observe(document.documentElement, {attributes: true, attributeFilter: names, subtree: true}); return 1; }"""
OFFLINE_TEXT = "Offline: messages are sent when you're back online"   # oc-push.js


@needs_offline
def test_offline_shows_a_pill_keeps_the_box_open_and_the_avatars(owui, browser):
    """Opened offline, the saved chat shows a pill, the model's picture (kept, not a broken
    image), and a message box that still takes text (a message sent then waits: the outbox)."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Pill")
    warm(page, chat)
    page.wait_for_function(AVATARS_DRAWN, timeout=WAIT_MS)
    box = page.locator("#chat-input")
    pill = page.locator("#oc-offline")
    ctx.set_offline(True)
    try:
        page.reload(timeout=WAIT_MS)
        box.wait_for(timeout=WAIT_MS)
        pill.wait_for(timeout=WAIT_MS)
        assert pill.inner_text() == OFFLINE_TEXT
        page.wait_for_function(AVATARS_DRAWN, timeout=WAIT_MS)
        assert not page.evaluate(COMPOSER_LOCKED), "the message box is locked offline"
        assert page.evaluate(BOX_LOCKS) == [], "the message box can't take the keyboard offline"
        box.focus()
        page.keyboard.type("typed offline")
        assert "typed offline" in box.inner_text()
        shot(page, "web-push-offline-pill")
    finally:
        ctx.set_offline(False)
    pill.wait_for(state="detached", timeout=WAIT_MS)


@needs_offline
def test_the_network_going_with_the_chat_open_never_locks_the_box(owui, browser):
    """The network goes while the chat is on screen (a phone into airplane mode): the box is never
    made readonly, inert, disabled or keyboard-less on the way, not even for a moment, and a real
    click puts the focus in it and takes typing."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Box Open " + uuid.uuid4().hex[:6])
    warm(page, chat)
    box = page.locator("#chat-input")
    page.evaluate(WATCH_LOCKS)
    ctx.set_offline(True)
    try:
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        assert page.evaluate(BOX_LOCKS) == [], "the message box can't take the keyboard offline"
        box.click()
        assert page.evaluate("() => !!document.activeElement?.closest('#chat-input')"), "a click doesn't focus the box"
        page.keyboard.type("typed after the network went")
        assert "typed after the network went" in box.inner_text()
        assert page.evaluate("() => window.__ocLocks") == [], "the box was locked on the way offline"
        page.evaluate("() => document.getElementById('chat-input').editor.commands.clearContent(true)")
    finally:
        ctx.set_offline(False)


@needs_offline
def test_another_user_on_the_same_browser_never_sees_the_first_users_chats(owui, pw):
    """User A's kept reads go when user B signs in on the same browser; offline, B's app shows
    none of A's chats."""
    a_chat, _ = new_chat(owui, "stub", "Chat Of User A " + uuid.uuid4().hex[:6])
    a_title = api(f"/api/v1/chats/{a_chat}", token=owui)["title"]
    b_cred = {"name": "E2E B", "email": f"b-{uuid.uuid4().hex[:8]}@e2e.example.com", "password": "e2e-pass-b"}
    b_token = api("/api/v1/auths/signup", b_cred)["token"]
    api("/api/v1/users/user/settings/update", {"ui": {"showChangelog": False}}, b_token)
    ctx = chrome(pw)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/auth")
        page.evaluate("(t) => localStorage.setItem('token', t)", owui)
        warm(page, a_chat)
        page.get_by_text(a_title).first.wait_for(timeout=WAIT_MS)
        a_caches = page.evaluate("() => caches.keys().then((ks) => ks.filter((k) => k.startsWith('oc-api-')))")
        assert len(a_caches) == 1, a_caches
        # B signs in on this browser (Open WebUI keeps the token in localStorage).
        page.evaluate("(t) => localStorage.setItem('token', t)", b_token)
        page.goto(BASE + "/")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        settled(page, """(a) => caches.keys().then((ks) => !ks.includes(a)
            && ks.some((k) => k.startsWith('oc-api-')))""", a_caches[0])
        # B asks for A's chat by its address, as Open WebUI would: the server's answer, never A's kept copy.
        got = page.evaluate("""([c, t]) => fetch('/api/v1/chats/' + c, {headers: {authorization: 'Bearer ' + t}})
            .then(async (r) => ({status: r.status, kept: r.headers.get('x-oc-kept'), body: await r.text()}))""",
                            [a_chat, b_token])
        assert got["status"] != 200 and not got["kept"] and a_title not in got["body"], got
        ctx.set_offline(True)
        page.reload(timeout=WAIT_MS)
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        assert page.get_by_text(a_title).count() == 0
        found = page.evaluate("""(c) => caches.keys().then(async (ks) => {
            for (const k of ks) if (await (await caches.open(k)).match(location.origin + '/api/v1/chats/' + c)) return k;
            return null; })""", a_chat)
        assert found is None, found
    finally:
        ctx.close()


UPDATING_PILL = "#oc-updating"


def say(token, chat_id, message_id, text):
    """The chat's assistant message says `text` on the server (a reply that came in elsewhere)."""
    c = api(f"/api/v1/chats/{chat_id}", token=token)["chat"]
    c["history"]["messages"][message_id].update({"content": text, "done": True})
    c["messages"] = list(c["history"]["messages"].values())
    api(f"/api/v1/chats/{chat_id}", {"chat": {"history": c["history"], "messages": c["messages"]}}, token)


def slow_network(ctx, token, chat_id):
    """A slow network: every read of Open WebUI's API for the page (with the page's
    Authorization; the worker's or the page's own) waits at a gate, then gets the server's
    answer as it is. Not only the chat's: Open WebUI draws a chat once its other reads are in too (its tags, its running tasks,
    the tools), so a test that slows only the chat's read misses what a slow phone sees. The
    worker's own reads (its cookie, a notification's reply) go at once. Returns the gate and a
    function that undoes it."""
    gate = threading.Event()

    class Held(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            gate.wait(WAIT_MS / 1000)
            req = urllib.request.Request(OWUI + self.path, headers={"Authorization": "Bearer " + token})
            try:
                r = urllib.request.urlopen(req, timeout=30)
                status, kind, body = r.status, r.headers.get("Content-Type", "application/json"), r.read()
            except urllib.error.HTTPError as err:
                status, kind, body = err.code, err.headers.get("Content-Type", "application/json"), err.read()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Held)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def held(route):
        # The page's own reads too (those the worker lets through go from the page).
        if route.request.method != "GET" or "authorization" not in route.request.headers:
            return route.fallback()
        note("held", f"sw={route.request.service_worker is not None} {route.request.url}")
        u = urllib.parse.urlsplit(route.request.url)
        route.continue_(url=f"http://127.0.0.1:{srv.server_address[1]}{u.path}" + (f"?{u.query}" if u.query else ""))
    ctx.route("**/api/**", held)

    def undo():
        ctx.unroute("**/api/**", held)
        gate.set()
        srv.shutdown()
    return gate, undo


def warm_kept(page, chat):
    """warm(), and the tools list kept too: Open WebUI waits for it before it draws a chat after a
    load, and a person who opened the chat before has it (warm() waits for the chat only).
    For the worker with kept chats: main's never keeps the tools list."""
    warm(page, chat)
    settled(page, """() => caches.keys().then(async (ks) => {
        for (const k of ks.filter((k) => k.startsWith('oc-api-')))
            if (await (await caches.open(k)).match(location.origin + '/api/v1/tools/?')) return true;
        return false; })""")


def kept_then_fresh(page, gate, old, new):
    """The chat shows `old` (the kept copy) while the network has nothing, marked and read-only;
    once the network's copy comes, `new`, unmarked and writable."""
    page.get_by_text(old).first.wait_for(timeout=WAIT_MS)
    page.locator(UPDATING_PILL).wait_for(timeout=WAIT_MS)
    page.wait_for_function(COMPOSER_LOCKED, timeout=WAIT_MS)
    assert page.get_by_text(new).count() == 0
    note("gate", "released")
    gate.set()
    try:
        page.get_by_text(new).first.wait_for(timeout=WAIT_MS)
    except Exception:
        print(sw_diagnostics(page), "\nupdating pill:", page.locator(UPDATING_PILL).count(),
              "| old shown:", page.get_by_text(old).count())
        raise
    page.locator(UPDATING_PILL).wait_for(state="detached", timeout=WAIT_MS)
    page.wait_for_function(f"() => !({COMPOSER_LOCKED})()", timeout=WAIT_MS)


@needs_chats
def test_a_chat_opened_before_shows_at_once_and_the_fresh_copy_replaces_it(owui, pw):
    """Opened again with the network slow, a chat shows the copy kept of it at once, with
    "Updating…" and the message box locked; Open WebUI's own refresh then puts the network's copy
    in its place. The same from a notification's tap while another chat is open. Its own browser."""
    tag = uuid.uuid4().hex[:6]
    chat, answer = new_chat(owui, "stub", "Chat Kept " + tag)
    other, _ = new_chat(owui, "stub", "Chat Other " + tag)
    say(owui, chat, answer, "Kept reply " + tag)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        say(owui, chat, answer, "Fresh reply " + tag)   # came in while the chat was closed
        gate, undo = slow_network(ctx, owui, chat)
        try:
            page.reload()
            kept_then_fresh(page, gate, "Kept reply " + tag, "Fresh reply " + tag)
        finally:
            undo()
        # Another chat open; a notification for this one, a newer reply on the server.
        page.goto(f"{BASE}/c/{other}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        say(owui, chat, answer, "Newest reply " + tag)
        gate, undo = slow_network(ctx, owui, chat)
        try:
            tap(page, "", {"chat_id": chat, "url": f"/c/{chat}"})
            page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=chat, timeout=WAIT_MS)
            kept_then_fresh(page, gate, "Fresh reply " + tag, "Newest reply " + tag)
        finally:
            undo()
    finally:
        ctx.close()


@needs_chats
def test_the_kept_chat_is_replaced_even_with_no_socket(owui, pw):
    """Open WebUI's socket never comes up (it refreshes a chat only when it joins the chat's
    room): once the network's copy is in, the page reloads into it, unmarked and writable."""
    tag = uuid.uuid4().hex[:6]
    chat, answer = new_chat(owui, "stub", "Chat No Socket " + tag)
    say(owui, chat, answer, "Kept reply " + tag)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        say(owui, chat, answer, "Fresh reply " + tag)
        ctx.route_web_socket("**/ws/socket.io/**", lambda ws: None)   # connects, the server never speaks
        gate, undo = slow_network(ctx, owui, chat)
        try:
            page.reload()
            kept_then_fresh(page, gate, "Kept reply " + tag, "Fresh reply " + tag)
        finally:
            undo()
    finally:
        ctx.close()


@needs_chats
def test_an_unsent_reply_waiting_for_a_kept_chat_stays_if_the_page_goes_elsewhere(owui, pw):
    """A reply that could not be sent waits for the message box of its chat (locked while the
    chat is its kept copy). The page goes to another chat meanwhile: the reply is not put in
    that chat's box, and it stays kept on the device."""
    tag = uuid.uuid4().hex[:6]
    chat, _ = new_chat(owui, "stub", "Chat Unsent " + tag)
    api(f"/api/v1/chats/{chat}", {"chat": {"models": ["no-such-model"]}}, owui)
    other, _ = new_chat(owui, "stub", "Chat Elsewhere " + tag)
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    ctx.add_cookies([{"name": "token", "value": owui, "url": BASE}])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        page.goto(f"{BASE}/c/{other}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        gate, undo = slow_network(ctx, owui, chat)
        try:
            text = "unsent " + tag
            tap(page, "reply", {"chat_id": chat, "url": f"/c/{chat}", "fn": "web_push"}, text)
            page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=chat, timeout=WAIT_MS)
            page.locator(UPDATING_PILL).wait_for(timeout=WAIT_MS)   # the box locked: the reply waits
            tap(page, "", {"chat_id": other, "url": f"/c/{other}"})   # elsewhere, in the app
            page.wait_for_function("(c) => location.pathname === '/c/' + c", arg=other, timeout=WAIT_MS)
            page.wait_for_function("() => !!window.ocPushReply", timeout=WAIT_MS)
            assert page.evaluate("() => window.ocPushReply.put") is False
            # Not in the other chat's box (the network slow, it may not be drawn yet: then no box at all).
            assert text not in page.evaluate("() => document.querySelector('#chat-input')?.innerText ?? ''")
            assert page.evaluate("(c) => caches.open('oc-push-drafts').then((d) => d.match('/oc-push/draft/' + c)).then((r) => !!r)", chat)
        finally:
            undo()
    finally:
        ctx.close()


@needs_offline
def test_a_sign_out_drops_the_kept_chats(owui, pw):
    """Open WebUI's sign-out (POST /api/v1/auths/signout): nothing of the user's reads stays."""
    cred = {"name": "E2E Out", "email": f"out-{uuid.uuid4().hex[:8]}@e2e.example.com", "password": "e2e-pass-out"}
    token = api("/api/v1/auths/signup", cred)["token"]
    api("/api/v1/users/user/settings/update", {"ui": {"showChangelog": False}}, token)
    chat, _ = new_chat(token, "stub", "Chat Signed Out")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        type_draft(page, chat, "a draft before the sign-out")
        assert page.evaluate(DRAFT_COPIES), "the draft has no copy that outlives the tab"
        page.evaluate("() => fetch('/api/v1/auths/signout', {method: 'POST', credentials: 'include'})")
        settled(page, "() => caches.keys().then((ks) => !ks.some((k) => k.startsWith('oc-api-')))")
        assert page.evaluate(DRAFT_COPIES) == [], "a draft copy stayed after the sign-out"
    finally:
        ctx.close()


def worker_offline(value):
    """The stack Worker's OFFLINE var, the Worker restarted with it (stack.sh offline)."""
    note("worker", f"OFFLINE={value}: restarting")
    subprocess.run(["bash", os.path.join(HERE, "stack.sh"), "offline", os.environ["E2E_RUN"], value],
                   check=True, capture_output=True, timeout=WAIT_MS / 1000)
    note("worker", f"OFFLINE={value}: up")
    assert f'OFFLINE_SETTING = "{value}"' in urllib.request.urlopen(BASE + "/oc-sw.js", timeout=30).read().decode()



HOLD_FETCH = "/oc-e2e-hold-fetch"   # e2e/sw_inflight.js: a fetch the worker answers only on 'oc-e2e-fetch-go'
HOLD_WAIT = "/oc-e2e-hold-wait"     # e2e/sw_inflight.js: answered at once, the event kept open (waitUntil) until go


def old_worker_busy(owui, pw, hold):
    """A new worker installed while the old one is still busy with an event (skipWaiting already
    called in its install) must take over as soon as that event ends, with the page still open:
    nothing else comes to let it in. The event is a fetch the old worker answers late (HOLD_FETCH,
    respondWith held) or answers at once and keeps open (HOLD_WAIT, waitUntil held), both in
    e2e/sw_inflight.js; released once the new worker is installed. (A request the worker lets go to
    the network does not hold the new one at all: it activates while that request is on its way.)"""
    chat, _ = new_chat(owui, "stub", "Chat Old Worker Busy")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    status = {}
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        tab = ctx.new_page()   # Chrome's account of the workers (CDP), beside the page
        cdp = ctx.new_cdp_session(tab)
        cdp.on("ServiceWorker.workerVersionUpdated", lambda ev: status.update(
            {v["versionId"]: v["status"] for v in ev["versions"]}))
        cdp.send("ServiceWorker.enable")
        def until(state, past=()):
            # Waits for state; fails at once when the new worker is already past it, with what was seen.
            end = time.time() + WAIT_MS / 1000
            while time.time() < end and not state() and status.get(NEW_VERSION_ID) not in past:
                tab.wait_for_timeout(STATE_POLL_S * 1000)   # CDP events arrive while Playwright waits
            assert state(), status
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        controlled(page)
        page.evaluate(f"() => {{ window.ocE2eHeld = fetch({json.dumps(hold)}).then((r) => r.text()); }}")
        settled(page, f"""() => new Promise((done) => {{
            navigator.serviceWorker.addEventListener('message', (e) => {{
                if (e.data && e.data.type === 'oc-e2e-inflight') done(e.data.list.some((x) => x.includes({json.dumps(hold)}))); }});
            navigator.serviceWorker.controller.postMessage({{type: 'oc-e2e-inflight'}}); }})""")
        worker_offline("0")
        page.evaluate("() => navigator.serviceWorker.getRegistration('/').then((r) => r.update())")
        # Installed and its skipWaiting called: it can only wait for the old worker's event now.
        until(lambda: status.get(NEW_VERSION_ID) == "installed", past=("activating", "activated", "redundant"))
        page.evaluate("() => navigator.serviceWorker.controller.postMessage({type: 'oc-e2e-fetch-go'})")
        assert page.evaluate("() => window.ocE2eHeld") == "go"
        until(lambda: status.get(NEW_VERSION_ID) == "activated", past=("redundant",))
    finally:
        ctx.close()
        worker_offline("1")


@needs_offline
@pytest.mark.skipif(not os.environ.get("E2E_RUN"), reason="set E2E_RUN=<run_dir> (stack.sh up) to turn the Worker's switch")
def test_a_new_worker_takes_over_once_the_old_ones_late_answer_ends(owui, pw):
    old_worker_busy(owui, pw, HOLD_FETCH)


@needs_offline
@pytest.mark.skipif(not os.environ.get("E2E_RUN"), reason="set E2E_RUN=<run_dir> (stack.sh up) to turn the Worker's switch")
def test_a_new_worker_takes_over_once_the_old_ones_open_event_ends(owui, pw):
    old_worker_busy(owui, pw, HOLD_WAIT)

INFLIGHT_WAIT_MS = 3000   # a worker that answers at all does so at once
INFLIGHT_ASK = """(ms) => new Promise((done) => {
    navigator.serviceWorker.addEventListener('message', (e) => {
        if (e.data && e.data.type === 'oc-e2e-inflight') done(e.data.list); });
    navigator.serviceWorker.controller.postMessage({type: 'oc-e2e-inflight'});
    setTimeout(() => done('no answer (a worker without e2e/sw_inflight.js)'), ms); })"""
# The page's fetch answers, kept to tell which bodies it never read (the worker counts as busy until
# a body it gave is read). The last UNREAD_KEPT only.
UNREAD_KEPT = 200
PAGE_READS = """((max) => { const kept = []; const f = window.fetch;
    window.fetch = function (...a) { const p = f.apply(this, a);
        p.then((r) => { kept.push({at: Date.now(), r}); if (kept.length > max) kept.shift(); }, () => {});
        return p; };
    window.ocE2eUnread = () => kept.filter((k) => !k.r.bodyUsed).map((k) => k.at + ' ' + k.r.status + ' ' + k.r.url);
})(%d)""" % UNREAD_KEPT


NEW_VERSION_ID = "1"   # Chrome's id for a fresh profile's second version of the worker (CDP)


def waiting_after_close(ctx, page):
    """A new worker still waiting: what holds the old one? Its unfinished events, the page's unread
    bodies; then the new one asked to skip waiting again with the page open, and if it still waits,
    the page closed. Chrome's own account of each version's status (CDP) throughout."""
    tab = ctx.new_page()   # about:blank keeps the browser up and carries the CDP session
    cdp = ctx.new_cdp_session(tab)
    cdp.on("ServiceWorker.workerVersionUpdated", lambda ev: [
        note("version", f"{v['versionId']} {v['status']} {v['runningStatus']} clients={v.get('controlledClients')}")
        for v in ev["versions"]])
    cdp.send("ServiceWorker.enable")
    # The stack's worker (with e2e/sw_inflight.js) answers with its unfinished events.
    try:
        note("probe", "inflight " + json.dumps(page.evaluate(INFLIGHT_ASK, INFLIGHT_WAIT_MS)))
    except Exception as e:
        note("probe", f"inflight: {e}")
    try:
        note("probe", "unread bodies " + json.dumps(page.evaluate("() => window.ocE2eUnread ? window.ocE2eUnread() : 'no tracker'")))
    except Exception as e:
        note("probe", f"unread bodies: {e}")
    # The waiting worker skips waiting again, the page still open: taking over now means its first
    # skipWaiting was missed; staying means the old worker is held by something no script sees.
    page.evaluate("() => navigator.serviceWorker.getRegistration('/').then((r) => r.waiting?.postMessage({type: 'oc-e2e-skip'}))")
    note("probe", "asked the waiting worker to skip waiting again (page open)")
    if not took_over(tab):
        note("probe", "still waiting with the page open; page closing")
        page.close()
        took_over(tab)
    note("probe", "done")


def took_over(tab):
    """Whether the new version reports activated (CDP), within WAIT_MS."""
    end = time.time() + WAIT_MS / 1000
    while time.time() < end:
        if any(t.split()[2:4] == [NEW_VERSION_ID, "activated"] for t in TRACE if " version " in t):
            return True
        tab.wait_for_timeout(STATE_POLL_S * 1000)   # CDP events arrive while Playwright waits
    return False


@needs_offline
@pytest.mark.skipif(not os.environ.get("E2E_RUN"), reason="set E2E_RUN=<run_dir> (stack.sh up) to turn the Worker's switch")
def test_the_kill_switch_drops_the_caches_and_stops_serving(owui, pw):
    """The Worker's OFFLINE turned to "0": the browser's next update of the service worker
    deletes its caches, and it answers nothing itself (offline, Chrome's own offline page).
    Its own browser: a request another test intercepted can keep the old worker busy, and a
    new one waits for it."""
    chat, _ = new_chat(owui, "stub", "Chat Kill Switch")
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(owui)})")
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warm_kept(page, chat)
        worker_offline("0")
        page.evaluate("() => navigator.serviceWorker.getRegistration('/').then((r) => r.update())")
        # The new worker takes over once the old one is idle; the next open comes after that.
        try:
            settled(page, """() => navigator.serviceWorker.getRegistration('/').then((r) =>
                r.active && r.active.state === 'activated' && !r.waiting && !r.installing)""")
        except AssertionError:
            waiting_after_close(ctx, page)
            raise
        page.reload()   # the next open
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        settled(page, NO_OFFLINE_CACHES)
        ctx.set_offline(True)
        with pytest.raises(Exception, match="ERR_INTERNET_DISCONNECTED"):
            page.reload(timeout=WAIT_MS)
    finally:
        ctx.close()
        worker_offline("1")


# --- Offline open, every case gets the app's own screen: no page kept to open, the
# notifications offer offline, a chat with no copy on this device.

OFFLINE_PAGE = "#oc-offline-page"   # oc-sw.js offlinePage()
OFFER = "#oc-push-offer"
NOT_SAVED_PILL = "#oc-not-saved"
PAGE_KEPT = "() => caches.open('oc-shell-v1').then((c) => c.match('/')).then((r) => !!r)"
NOTHING_LOADED = "() => performance.getEntriesByType('resource').length === 0"


def own_chrome(pw, token):
    """chrome() signed in as `token`, with no notification permission asked yet."""
    ctx = chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


@needs_offline
def test_with_no_page_kept_and_no_network_the_app_shows_its_own_offline_page(owui, pw):
    """No page kept to open (dropped: a new Open WebUI, an expired sign-in) and no network: the
    worker's own page with Open WebUI's name and icon, kept with the page, and nothing loaded.
    Never the browser's offline page. The network back: it opens the app by itself."""
    chat, _ = new_chat(owui, "stub", "Chat Offline Page")
    with urllib.request.urlopen(BASE + "/manifest.json", timeout=30) as r:
        name = json.loads(r.read())["name"]
    ctx, page = own_chrome(pw, owui)
    try:
        warm(page, chat)
        settled(page, PAGE_KEPT)
        page.evaluate("() => caches.open('oc-shell-v1').then((c) => c.delete('/'))")
        ctx.set_offline(True)
        try:
            page.reload(timeout=WAIT_MS)
            page.locator(OFFLINE_PAGE).wait_for(timeout=WAIT_MS)
            shot(page, "offline-page")
            assert page.locator("h1").inner_text() == name
            assert page.locator("img").get_attribute("src").startswith("data:image/")
            assert page.evaluate(NOTHING_LOADED), page.evaluate("() => performance.getEntriesByType('resource').map((r) => r.name)")
        finally:
            ctx.set_offline(False)
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        assert urllib.parse.urlparse(page.url).path == f"/c/{chat}"
    finally:
        ctx.close()


@needs_offline
def test_offline_the_notifications_offer_steps_aside_for_the_pill(owui, pw):
    """The "Enable notifications" offer sits where the offline pill is: offline it goes (the
    network going, or opened offline), and it comes back with the network."""
    chat, _ = new_chat(owui, "stub", "Chat Offer Offline")
    ctx, page = own_chrome(pw, owui)
    offer, pill = page.locator(OFFER), page.locator("#oc-offline")
    try:
        warm(page, chat)
        assert ready(page) == "no-permission"
        offer.wait_for(timeout=WAIT_MS)
        ctx.set_offline(True)
        try:
            pill.wait_for(timeout=WAIT_MS)
            offer.wait_for(state="detached", timeout=WAIT_MS)
            page.reload(timeout=WAIT_MS)
            pill.wait_for(timeout=WAIT_MS)
            assert ready(page) == "no-permission"
            shot(page, "offline-offer")
            assert offer.count() == 0, "the offer covers the offline pill"
        finally:
            ctx.set_offline(False)
        offer.wait_for(timeout=WAIT_MS)
        pill.wait_for(state="detached", timeout=WAIT_MS)
    finally:
        ctx.close()


@needs_offline
def test_a_chat_not_saved_here_keeps_its_address_offline_and_opens_once_back(owui, browser):
    """Offline, the sidebar lists the chats; one never opened on this device has no copy here.
    Open WebUI takes its "chat not found" path (to "/"): the page stays on the chat's address with
    a pill saying why, and opens the chat once the network is back."""
    ctx, page = browser
    page.bring_to_front()
    unread, msg = new_chat(owui, "stub", "Chat Not Saved " + uuid.uuid4().hex[:6])
    reply(owui, "stub", unread, msg)
    wait_done(owui, unread, msg)
    text = [m for m in history(owui, unread)["messages"].values() if m["role"] == "assistant"][0]["content"]
    text = text.strip().splitlines()[0][:30]
    read, _ = new_chat(owui, "stub", "Chat Saved Here")
    warm(page, read)   # the sidebar's list, read after both chats were made, is kept
    ctx.set_offline(True)
    try:
        page.reload(timeout=WAIT_MS)
        link = sidebar_link(page, unread)
        link.wait_for(timeout=WAIT_MS)
        shot(page, "offline-sidebar")
        link.click()
        try:
            page.locator(NOT_SAVED_PILL).wait_for(timeout=WAIT_MS)
        finally:
            print("address after the open:", page.url)
        page.wait_for_timeout(MENU_DRAWN_WAIT_MS)   # SvelteKit's own history writes after the navigation
        shot(page, "offline-not-saved")
        assert urllib.parse.urlparse(page.url).path == f"/c/{unread}"
        assert page.locator("#oc-offline").count() == 0, "two pills in one place"
    finally:
        ctx.set_offline(False)
    page.get_by_text(text, exact=False).first.wait_for(timeout=WAIT_MS)
    assert urllib.parse.urlparse(page.url).path == f"/c/{unread}"
    assert page.locator(NOT_SAVED_PILL).count() == 0



# --- A draft in the message box is never lost: not by the app being closed and opened again
# (a phone app killed in the background), not by a message posted into the open chat.

BOX_TEXT = "() => document.getElementById('chat-input')?.innerText ?? ''"
OWUI_DRAFT = "(c) => sessionStorage.getItem('chat-input-' + c) || ''"   # Open WebUI's own draft (Chat.svelte saveDraft)
DRAFT_STATE = """() => ({box: document.getElementById('chat-input')?.innerText, url: location.pathname,
  session: Object.fromEntries(Object.keys(sessionStorage).filter((k) => k.startsWith('chat-input')).map((k) => [k, sessionStorage.getItem(k)])),
  copies: Object.keys(localStorage).filter((k) => k.startsWith('oc-draft:')),
  user: (() => { try { return JSON.parse(atob(localStorage.token.split('.')[1])).id; } catch (e) { return String(e); } })()})"""
DRAFT_COPIES = "() => Object.keys(localStorage).filter((k) => k.startsWith('oc-draft:'))"   # oc-push.js DRAFT_COPY
COPY_OF = "(c) => Object.keys(localStorage).some((k) => k.startsWith('oc-draft:') && k.endsWith(':' + c))"   # this chat's copy


def signed_in(ctx, token):
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
    ctx.add_cookies([{"name": "token", "value": token, "url": BASE}])
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def type_draft(page, chat, text):
    """Typed with the keyboard, then held until Open WebUI has saved it as the chat's draft."""
    page.locator("#chat-input").click()
    page.keyboard.type(text)
    page.wait_for_function(f"(c) => ({OWUI_DRAFT})(c).includes({json.dumps(text)})", arg=chat, timeout=WAIT_MS)


@needs_offline
def test_a_draft_outlives_the_app_being_closed_and_opened_again(owui, pw):
    """A draft typed in a chat is still in the box after the app is closed and opened again on
    the same device (a phone app killed in the background): Open WebUI keeps drafts only for the
    life of the tab (sessionStorage)."""
    chat, _ = new_chat(owui, "stub", "Chat Draft Cold " + uuid.uuid4().hex[:6])
    text = "a draft typed before the app closed"
    profile = tempfile.mkdtemp(prefix="webpush-e2e-")
    ctx = chrome(pw, profile)
    try:
        page = signed_in(ctx, owui)
        warm(page, chat)
        type_draft(page, chat, text)
        assert page.evaluate(DRAFT_COPIES), "the draft has no copy that outlives the tab"
    finally:
        ctx.close()
    ctx = chrome(pw, profile)
    try:
        page = signed_in(ctx, owui)
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        page.get_by_text("hi", exact=True).first.wait_for(timeout=WAIT_MS)
        try:
            page.wait_for_function(f"() => ({BOX_TEXT})().includes({json.dumps(text)})", timeout=WAIT_MS)
        except Exception:
            shot(page, "draft-after-reopen")   # an old build: the empty box
            raise AssertionError("draft not back: " + json.dumps(page.evaluate(DRAFT_STATE)))
        shot(page, "draft-after-reopen")
    finally:
        ctx.close()


def test_a_draft_outlives_a_reload(owui, browser):
    """A draft typed in a chat is still in the box after the page is loaded again in the same
    tab. Open WebUI 0.13.2 alone loses it: its load clears the box, and a delayed save of that
    empty box lands after its own restore (checked on Open WebUI's own port, no add-on)."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Draft Reload " + uuid.uuid4().hex[:6])
    warm(page, chat)
    text = "a draft before a reload"
    type_draft(page, chat, text)
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.get_by_text("hi", exact=True).first.wait_for(timeout=WAIT_MS)
    try:
        page.wait_for_function(f"() => ({BOX_TEXT})().includes({json.dumps(text)})", timeout=WAIT_MS)
    except Exception:
        raise AssertionError("draft not back: " + json.dumps(page.evaluate(DRAFT_STATE)))
    # Open WebUI's own delayed save comes after: the draft must still be there once it has
    # saved, and be what Open WebUI keeps as the chat's draft.
    page.wait_for_function(f"(c) => ({OWUI_DRAFT})(c).includes({json.dumps(text)})", arg=chat, timeout=WAIT_MS)
    assert text in page.evaluate(BOX_TEXT)
    page.evaluate("() => document.getElementById('chat-input').editor.commands.clearContent(true)")


# Every WebSocket the page opens (Open WebUI's socket.io among them), so a test can drop the connection.
KEEP_SOCKETS = """(() => { const W = window.WebSocket; window.ocTestSockets = [];
  window.WebSocket = function (...a) { const w = new W(...a); window.ocTestSockets.push(w); return w; };
  window.WebSocket.prototype = W.prototype; Object.assign(window.WebSocket, W); })();"""
DROP_SOCKETS = "() => window.ocTestSockets.forEach((w) => w.close())"


def test_a_draft_outlives_a_socket_reconnect(owui, pw):
    """A guard, not a fail-first (green on main before and after #8): the connection drops and comes back while a reply is unfinished (a phone's network change
    while the agent answers), with no page load: Open WebUI joins the chat's room again, asks for
    its tasks and reads the chat again (Chat.svelte handleSocketConnect). The draft stays in the box."""
    chat, _ = new_chat(owui, "stub", "Chat Draft Reconnect " + uuid.uuid4().hex[:6])   # its answer: not done
    text = "a draft before the connection dropped"
    ctx = chrome(pw)
    try:
        ctx.add_init_script(KEEP_SOCKETS)
        page = signed_in(ctx, owui)
        warm(page, chat)
        type_draft(page, chat, text)
        origin = page.evaluate("() => performance.timeOrigin")
        with page.expect_response(lambda r: r.url.endswith(f"/api/v1/chats/{chat}") and r.request.method == "GET",
                                  timeout=WAIT_MS):
            page.evaluate(DROP_SOCKETS)   # socket.io connects again by itself
        # Settled: a socket open again (the connect handler ran: its chat read above came after it),
        # and the page drawn twice since that read's answer.
        page.wait_for_function("() => window.ocTestSockets.some((w) => w.readyState === WebSocket.OPEN)", timeout=WAIT_MS)
        page.evaluate("() => new Promise((ok) => requestAnimationFrame(() => requestAnimationFrame(ok)))")
        assert page.evaluate("() => performance.timeOrigin") == origin, "the page was loaded again"
        shot(page, "draft-after-reconnect")
        assert text in page.evaluate(BOX_TEXT), "draft gone: " + json.dumps(page.evaluate(DRAFT_STATE))
    finally:
        ctx.close()


@needs_chats
def test_a_draft_outlives_the_reload_into_a_kept_chats_fresh_copy(owui, pw):
    """The page script's own reload (oc-push.js CHAT_FRESH): the chat was drawn from the copy kept on
    the device while Open WebUI's socket is down (a phone's network change), and the page reloads
    into the network's copy once it is in. No reload by the person: the draft must be in the box after it."""
    tag = uuid.uuid4().hex[:6]
    chat, answer = new_chat(owui, "stub", "Chat Draft Fresh " + tag)
    say(owui, chat, answer, "Kept reply " + tag)
    text = "a draft before the network changed"
    ctx = chrome(pw)
    try:
        page = signed_in(ctx, owui)
        warm_kept(page, chat)
        type_draft(page, chat, text)
        say(owui, chat, answer, "Fresh reply " + tag)
        ctx.route_web_socket("**/ws/socket.io/**", lambda ws: None)   # connects, the server never speaks
        gate, undo = slow_network(ctx, owui, chat)
        try:
            page.reload()
            page.get_by_text("Kept reply " + tag).first.wait_for(timeout=WAIT_MS)
            with page.expect_event("load", timeout=WAIT_MS):   # the page script's reload into the fresh copy
                gate.set()
            page.get_by_text("Fresh reply " + tag).first.wait_for(timeout=WAIT_MS)
        finally:
            undo()
        try:
            page.wait_for_function(f"() => ({BOX_TEXT})().includes({json.dumps(text)})", timeout=WAIT_MS)
        except Exception:
            shot(page, "draft-after-fresh-reload")   # an old build: the empty box
            raise AssertionError("draft not back: " + json.dumps(page.evaluate(DRAFT_STATE)))
        shot(page, "draft-after-fresh-reload")
    finally:
        ctx.close()


def test_a_sent_draft_is_not_put_back(owui, browser):
    """A draft that was sent is gone for good: after the send and a reload, the box is empty."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Draft Sent " + uuid.uuid4().hex[:6])
    warm(page, chat)
    type_draft(page, chat, "a draft that is sent")
    page.keyboard.press("Enter")
    page.get_by_text("a draft that is sent").first.wait_for(timeout=WAIT_MS)
    # Open WebUI draws the message, then sends it: the copy goes with the send.
    page.wait_for_function(f"(c) => !({COPY_OF})(c)", arg=chat, timeout=WAIT_MS)
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.get_by_text("a draft that is sent").first.wait_for(timeout=WAIT_MS)
    assert page.evaluate(BOX_TEXT).strip() == ""


def post_like_owui_send(token, chat, text, model):
    """The manager's owui_send (projects/session-manager/owui_send.py _send_locked), step by step:
    a user message and an assistant stub written into the chat, chat:reload to the open tab, then
    the completion request for the stub (read to its end)."""
    c = api(f"/api/v1/chats/{chat}", token=token)["chat"]
    h, leaf = c["history"], c["history"]["currentId"]
    uid, aid, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    umsg = {"id": uid, "parentId": leaf, "childrenIds": [aid], "role": "user", "content": text,
            "timestamp": now, "models": [model]}
    amsg = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "",
            "timestamp": now, "done": False, "model": model, "modelName": model, "modelIdx": 0}
    h["messages"][leaf].setdefault("childrenIds", []).append(uid)
    h["messages"][uid], h["messages"][aid], h["currentId"] = umsg, amsg, aid
    c.setdefault("messages", []).extend([umsg, amsg])
    api(f"/api/v1/chats/{chat}", {"chat": c}, token)
    api(f"/api/v1/chats/{chat}/messages/{aid}/event", {"type": "chat:reload", "data": {}}, token)
    req = urllib.request.Request(OWUI + "/api/chat/completions", method="POST", data=json.dumps(
        {"model": model, "messages": [{"role": "user", "content": text}], "stream": True, "chat_id": chat,
         "id": aid, "assistant_message_id": aid, "user_message": umsg, "session_id": "e2e-send"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()
    return aid


def test_a_draft_outlives_a_message_posted_into_the_open_chat(owui, browser):
    """Typing in a chat while another sender posts into it the way owui_send does (the message,
    chat:reload, the reply): the posted message and its reply show, and the draft stays in the box."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Draft Post " + uuid.uuid4().hex[:6])
    warm(page, chat)
    text = "a draft typed while a message came in"
    type_draft(page, chat, text)
    aid = post_like_owui_send(owui, chat, "posted from outside", "stub")
    page.get_by_text("posted from outside").first.wait_for(timeout=WAIT_MS)
    end = time.time() + WAIT_MS / 1000
    while not history(owui, chat)["messages"][aid].get("done"):
        assert time.time() < end, "the posted message's reply never finished"
        time.sleep(STATE_POLL_S)
    page.locator("#send-message-button").wait_for(timeout=WAIT_MS)   # the page has the reply's end: Send, not Stop
    assert text in page.evaluate(BOX_TEXT), f"draft gone: {page.evaluate(BOX_TEXT)!r}"
    page.evaluate("() => document.getElementById('chat-input').editor.commands.clearContent(true)")


def test_a_draft_copy_goes_once_its_chat_is_deleted_elsewhere(owui, browser):
    """Drafts in two chats; one chat is deleted elsewhere (another device: the API). The next
    load drops that chat's copy and keeps the other's."""
    ctx, page = browser
    page.bring_to_front()
    gone, _ = new_chat(owui, "stub", "Chat Draft Gone " + uuid.uuid4().hex[:6])
    kept, _ = new_chat(owui, "stub", "Chat Draft Kept " + uuid.uuid4().hex[:6])
    warm(page, gone)
    type_draft(page, gone, "a draft in a chat about to be deleted")
    warm(page, kept)
    type_draft(page, kept, "a draft in a chat that stays")
    copy_of = COPY_OF
    assert page.evaluate(copy_of, gone) and page.evaluate(copy_of, kept)
    api(f"/api/v1/chats/{gone}", token=owui, method="DELETE")
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.wait_for_function(f"(c) => !({copy_of})(c)", arg=gone, timeout=WAIT_MS)
    assert page.evaluate(copy_of, kept), "the copy of a chat that still exists was dropped"
    page.evaluate("() => document.getElementById('chat-input').editor.commands.clearContent(true)")


# --- The outbox: a message sent with no network waits on the device and goes once
# the network is back, as one message, once.

OUTBOX_ITEM = "#oc-outbox .oc-outbox-item"
OUTBOX_COUNT = "() => window.ocOutbox ? window.ocOutbox.entries().then((l) => l.length) : -1"
NOTICE = "#oc-push-notice"
SEND_FAILED_ON_NETWORK = "internetdisconnected"


def lines(text):
    """A message's lines, blank ones left out: Open WebUI writes a blank line of its box as "  "."""
    return "\n".join(x.strip() for x in str(text).splitlines() if x.strip())


def user_texts(token, chat):
    return [lines(m.get("content")) for m in history(token, chat)["messages"].values() if m["role"] == "user"]


def wait_user_texts(token, chat, want):
    """Until the server's user messages are `want` (a poll of the saved chat)."""
    end = time.time() + WAIT_MS / 1000
    while user_texts(token, chat) != want:
        assert time.time() < end, f"server user messages {user_texts(token, chat)} != {want}"
        time.sleep(STATE_POLL_S)


def settled_none_sent(page, token, chat, want):
    """Nothing more is sent: the outbox is empty and the server has `want` (one state each)."""
    settled(page, f"() => ({OUTBOX_COUNT})().then((n) => n === 0)")
    assert user_texts(token, chat) == want


def send(page, text):
    page.locator("#chat-input").focus()
    page.keyboard.type(text)
    page.keyboard.press("Enter")


@needs_offline
def test_messages_sent_offline_wait_and_go_once_as_one_message(owui, browser):
    """Two messages sent offline wait above the box (they outlive a reload), the box is empty, the
    server has nothing; back online they go as ONE message, joined with a blank line, and the
    waiting ones go. A load afterwards sends nothing again."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Outbox " + uuid.uuid4().hex[:6])
    warm(page, chat)
    ctx.set_offline(True)
    try:
        page.reload(timeout=WAIT_MS)
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        send(page, "first offline")
        send(page, "second offline")
        page.locator(OUTBOX_ITEM).nth(1).wait_for(timeout=WAIT_MS)
        assert page.locator("#chat-input").inner_text().strip() == ""
        shot(page, "outbox-waiting")
        page.reload(timeout=WAIT_MS)
        page.locator(OUTBOX_ITEM).nth(1).wait_for(timeout=WAIT_MS)
        assert page.locator(OUTBOX_ITEM).count() == 2
        # What waits is not also a draft: no copy of it, and the box stays empty.
        assert not page.evaluate(COPY_OF, chat), "a text in the outbox kept its draft copy"
        assert page.evaluate(BOX_TEXT).strip() == ""
        assert user_texts(owui, chat) == ["hi"]
    finally:
        ctx.set_offline(False)
    wait_user_texts(owui, chat, ["hi", "first offline\nsecond offline"])
    page.locator(OUTBOX_ITEM).first.wait_for(state="detached", timeout=WAIT_MS)
    settled_none_sent(page, owui, chat, ["hi", "first offline\nsecond offline"])
    page.reload()
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    settled_none_sent(page, owui, chat, ["hi", "first offline\nsecond offline"])


@needs_offline
def test_a_send_that_fails_on_the_network_waits_and_goes_once(owui, browser):
    """Lie-fi: the device says online, Open WebUI's send fails on the network. The message waits
    on the device and goes once on the next load with a network, never twice."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Lie-fi " + uuid.uuid4().hex[:6])
    warm(page, chat)
    page.route("**/api/chat/completions", lambda r: r.abort(SEND_FAILED_ON_NETWORK))
    try:
        send(page, "sent on lie-fi")
        page.locator(OUTBOX_ITEM).first.wait_for(timeout=WAIT_MS)
        page.locator(NOTICE).wait_for(timeout=WAIT_MS)
        assert user_texts(owui, chat) == ["hi"]
    finally:
        page.unroute("**/api/chat/completions")
    page.reload()
    wait_user_texts(owui, chat, ["hi", "sent on lie-fi"])
    settled_none_sent(page, owui, chat, ["hi", "sent on lie-fi"])


@needs_offline
def test_two_tabs_back_online_together_send_it_once(owui, browser):
    """The same waiting message in two tabs of the chat, both back online at once: one message."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Two Tabs " + uuid.uuid4().hex[:6])
    warm(page, chat)
    other = ctx.new_page()
    try:
        other.goto(f"{BASE}/c/{chat}")
        other.locator("#chat-input").wait_for(timeout=WAIT_MS)
        ctx.set_offline(True)
        try:
            page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
            send(page, "from two tabs")
            page.locator(OUTBOX_ITEM).first.wait_for(timeout=WAIT_MS)
            other.reload(timeout=WAIT_MS)
            other.locator(OUTBOX_ITEM).first.wait_for(timeout=WAIT_MS)
        finally:
            ctx.set_offline(False)
        wait_user_texts(owui, chat, ["hi", "from two tabs"])
        for p in (page, other):
            settled_none_sent(p, owui, chat, ["hi", "from two tabs"])
    finally:
        other.close()


@needs_offline
def test_messages_waiting_in_another_chat_say_where_and_go_only_there(owui, browser):
    """A message waits in chat A; the page is on chat B when the network is back: a pill says how
    many wait and where; nothing is sent until A is open (from the pill), then once, in A."""
    ctx, page = browser
    page.bring_to_front()
    tag = uuid.uuid4().hex[:6]
    a, _ = new_chat(owui, "stub", "Chat Waiting " + tag)
    b, _ = new_chat(owui, "stub", "Chat Elsewhere " + tag)
    warm(page, b)
    warm(page, a)
    ctx.set_offline(True)
    try:
        page.reload(timeout=WAIT_MS)
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        send(page, "waiting in A")
        page.locator(OUTBOX_ITEM).first.wait_for(timeout=WAIT_MS)
        page.goto(f"{BASE}/c/{b}")
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    finally:
        ctx.set_offline(False)
    others = page.locator("#oc-outbox-elsewhere")
    others.wait_for(timeout=WAIT_MS)
    assert others.inner_text() == f"1 message waiting in Chat Waiting {tag}"
    shot(page, "outbox-elsewhere")
    assert user_texts(owui, a) == ["hi"] and user_texts(owui, b) == ["hi"]
    others.click()
    page.wait_for_url(f"**/c/{a}", timeout=WAIT_MS)
    wait_user_texts(owui, a, ["hi", "waiting in A"])
    settled_none_sent(page, owui, a, ["hi", "waiting in A"])
    assert user_texts(owui, b) == ["hi"]


@needs_offline
def test_a_waiting_message_whose_chat_was_deleted_stays_with_the_reason(owui, browser):
    """The chat is deleted while its message waits: back online, nothing is sent anywhere and the
    waiting message says why."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Deleted " + uuid.uuid4().hex[:6])
    warm(page, chat)
    ctx.set_offline(True)
    try:
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        send(page, "to a chat that goes")
        page.locator(OUTBOX_ITEM).first.wait_for(timeout=WAIT_MS)
        req = urllib.request.Request(f"{OWUI}/api/v1/chats/{chat}", method="DELETE", headers={"Authorization": "Bearer " + owui})
        urllib.request.urlopen(req, timeout=30).read()
    finally:
        ctx.set_offline(False)
    # Two places can say why, both keeping the message: the bubble above the box while the page is
    # on the chat, or the pill on another page (Open WebUI may leave a deleted chat for its home page).
    page.wait_for_function("""() => [...document.querySelectorAll('#oc-outbox .oc-outbox-item')].some((b) =>
        b.innerText.includes('no longer exists')) || !!document.getElementById('oc-outbox-elsewhere')?.innerText.includes('was deleted')""",
                           timeout=WAIT_MS)
    shot(page, "outbox-chat-gone")
    assert page.evaluate(OUTBOX_COUNT) == 1
    page.goto(BASE + "/")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    others = page.locator("#oc-outbox-elsewhere")
    page.wait_for_function("() => !!document.getElementById('oc-outbox-elsewhere')?.innerText.includes('was deleted')", timeout=WAIT_MS)
    others.click()
    page.wait_for_function("() => document.getElementById('chat-input')?.innerText.includes('to a chat that goes')", timeout=WAIT_MS)
    settled(page, f"() => ({OUTBOX_COUNT})().then((n) => n === 0)")
    page.evaluate("() => document.getElementById('chat-input').editor.commands.clearContent(true)")


@needs_offline
def test_a_new_chat_offline_is_not_taken_and_the_text_stays(owui, browser):
    """Offline on a new chat (no chat yet): Send says why and the text stays in the box."""
    ctx, page = browser
    page.bring_to_front()
    chat, _ = new_chat(owui, "stub", "Chat Before New")
    warm(page, chat)
    page.goto(BASE + "/")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    before = page.evaluate(OUTBOX_COUNT)
    ctx.set_offline(True)
    try:
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        send(page, "a new chat offline")
        page.locator(NOTICE).wait_for(timeout=WAIT_MS)
        assert "new chat needs the network" in page.locator(NOTICE).inner_text()
        assert "a new chat offline" in page.locator("#chat-input").inner_text()
        assert page.evaluate(OUTBOX_COUNT) == before
    finally:
        ctx.set_offline(False)
    page.evaluate("() => document.getElementById('chat-input').editor.commands.clearContent(true)")


@needs_offline
def test_a_browser_that_keeps_nothing_sends_as_open_webui_does_and_says_so(owui, pw):
    """No IndexedDB (private mode, no storage): Send is left to Open WebUI, the offline pill says a
    send fails, and the text is not lost silently (Open WebUI shows the failed message)."""
    chat, _ = new_chat(owui, "stub", "Chat No Storage")
    ctx, page = own_chrome(pw, owui)
    ctx.add_init_script("indexedDB.open = () => { throw new DOMException('no storage', 'InvalidStateError'); };")
    try:
        warm(page, chat)
        ctx.set_offline(True)
        try:
            page.reload(timeout=WAIT_MS)
            pill = page.locator("#oc-offline")
            pill.wait_for(timeout=WAIT_MS)
            page.wait_for_function("() => document.getElementById('oc-offline')?.innerText.includes('keep messages')", timeout=WAIT_MS)
            send(page, "no storage here")
            page.locator(".user-message", has_text="no storage here").wait_for(timeout=WAIT_MS)
            assert page.locator(OUTBOX_ITEM).count() == 0
        finally:
            ctx.set_offline(False)
    finally:
        ctx.close()


def test_a_reconnecting_socket_with_http_up_sends_at_once(owui, pw):
    """The socket never comes up while HTTP works ("reconnecting"): that is not offline. Send goes
    to the server at once and nothing waits."""
    chat, _ = new_chat(owui, "stub", "Chat Socket Down " + uuid.uuid4().hex[:6])
    ctx, page = own_chrome(pw, owui)
    try:
        warm(page, chat)
        ctx.route_web_socket("**/ws/socket.io/**", lambda ws: None)   # connects, the server never speaks
        page.reload()
        page.locator("#chat-input").wait_for(timeout=WAIT_MS)
        send(page, "socket down, http up")
        wait_user_texts(owui, chat, ["hi", "socket down, http up"])
        assert page.locator(OUTBOX_ITEM).count() == 0
        assert page.evaluate(OUTBOX_COUNT) in (0, -1)
    finally:
        ctx.close()


# --- Channel messages (ELI-548): two users, each with a subscribed device (own Chrome profile) ---

CHANNEL_TEXT = "SECRET-CHANNEL-TEXT"
SENDER_NAME = "E2E"   # the admin of the stack, who sends


def signed_up(name):
    """A new plain user: (token, id)."""
    r = api("/api/v1/auths/signup", {"name": name, "email": f"{name.lower()}-{uuid.uuid4().hex[:8]}@e2e.example.com",
                                     "password": "e2e-pass-2"})
    return r["token"], r["id"]


def enabled_device(pw, token):
    """A Chrome profile signed in as `token` with notifications allowed and this device subscribed."""
    ctx, page = own_chrome(pw, token)
    ctx.add_init_script(DECISIONS)
    ctx.add_cookies([{"name": "token", "value": token, "url": BASE}])
    page.goto(BASE + "/")
    assert ready(page) == "no-permission"
    ctx.grant_permissions(["notifications"], origin=BASE)
    ctx.new_cdp_session(ctx.new_page()).send("Browser.setPermission", {
        "permission": {"name": "push", "userVisibleOnly": True}, "setting": "granted", "origin": BASE})
    page.bring_to_front()
    page.get_by_role("button", name="Enable notifications for replies").click()
    page.wait_for_function("() => window.ocPushState !== 'no-permission'", timeout=WAIT_MS)
    assert page.evaluate("() => window.ocPushState") == "saved"
    push_connection_live(ctx, BASE)
    return ctx, page


def post(token, channel, content):
    return api(f"/api/v1/channels/{channel}/messages/post", {"content": content}, token)


# Open WebUI publishes `channel.message` only for a member with no live socket session: a page connects
# its socket to every one of its user's channels (socket/main.py `connect`), open or not, in front or not.
# So a device is "away" here by parking its page on a file of the site: the service worker still runs, no
# Open WebUI socket is open, and the shown notifications are read from the registration.
PARKED = "/manifest.json"
ALL_DEVICES = []   # the pages of `pair`, for a failure's report


def park(ctx, page):
    """The page away from Open WebUI and off the screen (another tab in front: the worker shows a
    notification only when no Open WebUI window is in front)."""
    page.goto(BASE + PARKED)
    return behind(ctx, page)


def shown(page):
    return page.evaluate(NOTES)


def wait_shown(page, tag):
    """Waits until a notification with this tag is showing; returns it."""
    end = time.time() + PUSH_WAIT_MS / 1000
    while time.time() < end:
        found = [n for n in shown(page) if n["tag"] == tag]
        if found:
            return found[0]
        time.sleep(STATE_POLL_S)
    print(push_diagnostics(page))
    print("shown now:", json.dumps(shown(page)))
    for other in ALL_DEVICES:
        if other is not page:
            print("another device shows:", json.dumps(shown(other)), "| its subscription:", fingerprint(other.evaluate(ENDPOINT)))
    raise AssertionError("no notification tagged " + tag)


def channel_notes(page):
    return [n for n in shown(page) if n["tag"].startswith("channel-")]


def channel_decisions(page):
    return page.evaluate("() => window.ocPushSeen.filter(d => d.payload.channel_id).map(d => d.payload.message_id)")


@pytest.fixture(scope="module")
def pair(owui, pw):
    """Two users (the stack's admin `E2E` and `Bea`), each with a subscribed device; Open WebUI told to
    publish channel events (ui.enable_user_webhooks) and to have channels on."""
    cfg = api("/api/v1/auths/admin/config", token=owui)
    api("/api/v1/auths/admin/config", {**cfg, "ENABLE_CHANNELS": True, "ENABLE_USER_WEBHOOKS": True}, owui)
    bea, bea_id = signed_up("Bea")
    a = enabled_device(pw, owui)
    b = enabled_device(pw, bea)
    ALL_DEVICES[:] = [a[1], b[1]]
    yield a, b, bea, bea_id
    a[0].close()
    b[0].close()


def test_channel_messages_push_the_other_members_with_no_text_and_open_the_channel(owui, pair):
    (a_ctx, a_page), (b_ctx, b_page), bea, bea_id = pair
    name = api("/api/v1/auths/", token=owui)["name"]
    park(a_ctx, a_page)   # both away, as two phones with Open WebUI closed
    park(b_ctx, b_page)
    # A group channel: Bea gets one, titled by the channel, no text, opening the channel; the sender gets none.
    group = api("/api/v1/channels/create", {"name": "Japan", "type": "group", "user_ids": [bea_id]}, owui)["id"]
    post(owui, group, f"{CHANNEL_TEXT} first")
    n = wait_shown(b_page, f"channel-{group}")
    assert (n["title"], n["body"], n["url"]) == ("#Japan", "New message", f"/channels/{group}"), n
    assert CHANNEL_TEXT not in json.dumps(shown(b_page))
    # The sender's device: a chat reply that follows is the barrier; no channel notification came before it.
    chat, msg = new_chat(owui, "stub", "Barrier A")
    reply(owui, "stub", chat, msg)
    wait_shown(a_page, f"chat-{chat}")
    assert channel_notes(a_page) == []

    # A DM has no name: the sender's name is the title.
    dm = api("/api/v1/channels/create", {"type": "dm", "user_ids": [bea_id]}, owui)["id"]
    post(owui, dm, "psst")
    n = wait_shown(b_page, f"channel-{dm}")
    assert (n["title"], n["url"]) == (name, f"/channels/{dm}"), n

    # Muted in Open WebUI: a plain message sends nothing; the @mention of Bea does, once (a second
    # push in the channel would stack: its title would count "2 new messages").
    b_page.evaluate(CLEAR)
    api(f"/api/v1/channels/{group}/members/mute", {"is_channel_muted": True}, bea)
    post(owui, group, "plain, while muted")
    post(owui, group, f"hey <@U:{bea_id}|Bea> a mention")
    n = wait_shown(b_page, f"channel-{group}")
    assert n["title"] == "#Japan", n
    api(f"/api/v1/channels/{group}/members/mute", {"is_channel_muted": False}, bea)

    # With Open WebUI open and connected on the device, Open WebUI publishes nothing; opening the
    # channel closes its notification.
    b_page.goto(f"{BASE}/channels/{group}")
    b_page.bring_to_front()
    b_page.wait_for_function("async () => !(await (await navigator.serviceWorker.ready).getNotifications({tag: 'channel-%s'})).length" % group,
                             timeout=WAIT_MS)
    post(owui, group, "while it is open")
    b_chat, b_msg = new_chat(bea, "stub", "Barrier open")
    reply(bea, "stub", b_chat, b_msg)
    seen_for(b_page, b_chat)
    assert channel_decisions(b_page) == []
    assert channel_notes(b_page) == []

    # The kill switch (valve CHANNEL_PUSH): nothing for a channel; a reply to a chat still notifies.
    park(b_ctx, b_page)
    b_page.evaluate(CLEAR)
    api("/api/v1/functions/id/web_push/valves/update",
        {"VAPID_PRIVATE_KEY_FILE": "/mnt/keys/vapid_private.pem", "VAPID_SUBJECT": "mailto:e2e@example.com", "CHANNEL_PUSH": False}, owui)
    try:
        post(owui, dm, "while the switch is off")
        b_chat, b_msg = new_chat(bea, "stub", "Barrier switch")
        reply(bea, "stub", b_chat, b_msg)
        wait_shown(b_page, f"chat-{b_chat}")
        assert channel_notes(b_page) == []
    finally:
        api("/api/v1/functions/id/web_push/valves/update",
            {"VAPID_PRIVATE_KEY_FILE": "/mnt/keys/vapid_private.pem", "VAPID_SUBJECT": "mailto:e2e@example.com"}, owui)
