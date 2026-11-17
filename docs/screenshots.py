"""The README's pictures (docs/img/*.png), from a throwaway stack with made-up chats: never real data.

    e2e/stack.sh up <run_dir> <openclaw_pipe.py build>
    E2E_RUN=<run_dir> xvfb-run -a uv run --with playwright==1.64.0 --with pytest python docs/screenshots.py [name ...]
    e2e/stack.sh down <run_dir>

The stack, ports and Chrome are the e2e's (E2E_OWUI, E2E_BASE, E2E_CHROME); its helpers are reused. With names,
only those pictures are taken. A phone-sized window for what a phone shows, a desktop one for the admin and the
sidebar menus. The Android notification pictures come from an emulator, not from here (README, "Pictures").
"""
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "img")
os.environ.setdefault("WEB_PUSH_E2E", "1")
_spec = importlib.util.spec_from_file_location("web_push_e2e", os.path.join(ROOT, "e2e", "test_web_push_e2e.py"))
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)
api, BASE, WAIT_MS = e2e.api, e2e.BASE, e2e.WAIT_MS

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 800}
FN = "web_push"           # the function id the stack's Worker is given (FUNCTION_ID)
MODEL = "assistant"       # docs/fixtures/assistant_pipe.py
ADMIN = {"name": "Sam Rivera", "email": "sam@example.com", "password": "docs-pass-1"}
# A device of the user's, so the bell and the menu items show (they show once a device is enabled). Never sent to:
# no reply runs while the pictures are taken.
DEVICE = {"endpoint": "https://fcm.googleapis.com/fcm/send/docs-example", "keys": {"p256dh": "docs", "auth": "docs"},
          "device": "docs-example", "ua": "docs", "at": 0}
HOUR_S = 3600


def setup():
    """The admin (the stack's first user), the function, the stand-in model and the made-up chats."""
    try:
        token = api("/api/v1/auths/signin", {"email": ADMIN["email"], "password": ADMIN["password"]})["token"]
    except e2e.urllib.error.HTTPError:
        token = api("/api/v1/auths/signup", ADMIN)["token"]
    api("/api/v1/users/user/settings/update", {"ui": {"showChangelog": False}}, token)
    have = {f["id"] for f in api("/api/v1/functions/", token=token)}
    for fid, name, path in ((FN, "Web Push", os.path.join(ROOT, "openclaw_web_push.py")),
                            (MODEL, "Assistant", os.path.join(ROOT, "docs", "fixtures", "assistant_pipe.py"))):
        form = {"id": fid, "name": name, "content": open(path).read(), "meta": {"description": name}}
        api(f"/api/v1/functions/id/{fid}/update" if fid in have else "/api/v1/functions/create", form, token)
        if not api(f"/api/v1/functions/id/{fid}", token=token)["is_active"]:
            api(f"/api/v1/functions/id/{fid}/toggle", {}, token)
    api(f"/api/v1/functions/id/{FN}/valves/update",
        {"VAPID_PRIVATE_KEY_FILE": "/mnt/keys/vapid_private.pem", "VAPID_SUBJECT": "mailto:admin@example.com"}, token)
    for c in api("/api/v1/chats/", token=token):
        api(f"/api/v1/chats/{c['id']}", token=token, method="DELETE")
    for f in api("/api/v1/folders/", token=token) or []:
        api(f"/api/v1/folders/{f['id']}", token=token, method="DELETE")
    api(f"/api/v1/functions/id/{FN}/valves/user/update",
        {"subscriptions": json.dumps([DEVICE]), "mutes": "{}", "folder_mutes": "{}"}, token)
    return token


CHATS = [  # oldest first: the sidebar lists the newest on top
    ("Book club picks", None, [
        ("user", "Three short novels for a book club that meets in two weeks?"),
        ("assistant", "Try *Small Things Like These*, *Train Dreams* and *Convenience Store Woman*: each is under 200 pages and gives a group plenty to talk about.")]),
    ("Kyoto in autumn", "Travel", [
        ("user", "Where should we stay in Kyoto in November?"),
        ("assistant", "Near Gion for the evening walks, or by Kyoto Station if you'll take day trips to Nara and Osaka. Want me to compare a few hotels?")]),
    ("Garage door won't close", None, [
        ("user", "The garage door goes down, then back up halfway. What should I check?"),
        ("assistant", "Usually the safety sensors: wipe both lenses and check that their lights are steady. If one blinks, the two are out of line; nudge it until it stops.")]),
    ("Weekly groceries", None, [
        ("user", "Make a list for the week: pasta twice, a curry, and something with salmon."),
        ("assistant", "Here's the list:\n\n- Spaghetti, penne, canned tomatoes, parmesan\n- Chickpeas, coconut milk, curry paste, rice\n- Salmon fillets, lemons, dill, potatoes\n- Spinach, onions, garlic\n\nWant me to split it by store aisle?")]),
]


def seed(token):
    """The chats and the folder; returns {title: chat id} and {folder name: id}."""
    folders, ids = {}, {}
    for title, folder, turns in CHATS:
        msgs, parent, now = {}, None, int(time.time())
        for i, (role, text) in enumerate(turns):
            mid = f"m{i}"
            msgs[mid] = {"id": mid, "parentId": parent, "childrenIds": [], "role": role, "content": text,
                         "timestamp": now, **({"model": MODEL, "done": True} if role == "assistant" else {"models": [MODEL]})}
            if parent:
                msgs[parent]["childrenIds"].append(mid)
            parent = mid
        chat = api("/api/v1/chats/new", {"chat": {"title": title, "models": [MODEL], "history": {"messages": msgs, "currentId": parent},
                                                  "messages": list(msgs.values())}}, token)
        ids[title] = chat["id"]
        if folder:
            if folder not in folders:
                folders[folder] = api("/api/v1/folders/", {"name": folder}, token)["id"]
            api(f"/api/v1/chats/{chat['id']}/folder", {"folder_id": folders[folder]}, token)
        time.sleep(1)   # distinct update times, so the sidebar keeps this order
    unread = ids["Garage door won't close"]
    api(f"/api/v1/chats/{unread}/unread", {}, token)
    return ids, folders


def browser(pw, token, size=PHONE, offer=False):
    """The e2e's Google Chrome (own profile), signed in. The notifications offer hidden unless `offer`: it sits over
    the top of every page until the browser has asked."""
    ctx = e2e.chrome(pw)
    ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)})")
    if not offer:
        ctx.add_init_script("localStorage.setItem('ocPushOffer', 'dismissed')")
    ctx.add_cookies([{"name": "token", "value": token, "url": BASE}])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_viewport_size(size)
    return ctx, page


def save(page, name):
    page.wait_for_timeout(e2e.MENU_DRAWN_WAIT_MS)   # the frame after a menu or pill appears
    page.screenshot(path=os.path.join(OUT, name + ".png"))
    print("docs/img/" + name + ".png")


def opened(page, chat):
    page.goto(f"{BASE}/c/{chat}")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    e2e.ready(page)


def sidebar(page, chat):
    """The sidebar open, with `chat` in it (Open WebUI 0.13 starts with it closed to a rail)."""
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    if page.locator("#sidebar-open-button").is_visible():
        page.locator("#sidebar-open-button").click()
    page.locator(f'a[href="/c/{chat}"]').wait_for(timeout=WAIT_MS)


def folder_row(page, folder):
    """A folder's row in the open sidebar (its Folders section opened if it is closed)."""
    row = page.locator(f"#folder-{folder}-button")
    if not row.count():
        page.get_by_role("button", name="Folders", exact=True).click()
    row.wait_for(timeout=WAIT_MS)


def bell_menu(page, chat, wait_for):
    opened(page, chat)
    page.locator("#oc-push-bell").click()
    page.get_by_role("menuitem", name=wait_for).first.wait_for(timeout=WAIT_MS)


def shot_enable(pw, token, ids, folders):
    """The offer, while the browser has not asked yet."""
    ctx, page = browser(pw, token, offer=True)
    page.goto(BASE + "/")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    page.locator("#oc-push-offer").wait_for(timeout=WAIT_MS)
    save(page, "enable")
    ctx.close()


def shot_bell(pw, token, ids, folders):
    """The bell's menu in a chat."""
    ctx, page = browser(pw, token)
    bell_menu(page, ids["Weekly groceries"], "Mute for 1 hour")
    save(page, "bell")
    ctx.close()


def shot_bell_folder(pw, token, ids, folders):
    """The bell's menu in a chat in a folder: the folder's part below the chat's."""
    ctx, page = browser(pw, token)
    bell_menu(page, ids["Kyoto in autumn"], "Mute folder for 1 hour")
    save(page, "bell-folder")
    ctx.close()


def shot_muted(pw, token, ids, folders):
    """A muted chat: the bell crossed out, its menu says until when."""
    chat = ids["Book club picks"]
    api(f"/api/v1/functions/id/{FN}/valves/user/update",
        {"subscriptions": json.dumps([DEVICE]), "mutes": json.dumps({chat: int(time.time()) + 8 * HOUR_S}),
         "folder_mutes": "{}"}, token)
    ctx, page = browser(pw, token)
    try:
        bell_menu(page, chat, "Unmute")
        save(page, "muted")
    finally:
        ctx.close()
        api(f"/api/v1/functions/id/{FN}/valves/user/update",
            {"subscriptions": json.dumps([DEVICE]), "mutes": "{}", "folder_mutes": "{}"}, token)


def shot_chat_menu(pw, token, ids, folders):
    """A chat's ⋯ menu in the sidebar: Mark as read and Mute notifications after Open WebUI's own items."""
    ctx, page = browser(pw, token, DESKTOP)
    page.goto(BASE + "/")
    e2e.ready(page)
    sidebar(page, ids["Garage door won't close"])
    menu = e2e.chat_menu(page, ids["Garage door won't close"])
    menu.get_by_role("menuitem", name="Mute notifications").wait_for(timeout=WAIT_MS)
    save(page, "chat-menu")
    ctx.close()


def shot_folder_menu(pw, token, ids, folders):
    """A folder's ⋯ menu in the sidebar: Mute folder."""
    ctx, page = browser(pw, token, DESKTOP)
    page.goto(BASE + "/")
    e2e.ready(page)
    sidebar(page, ids["Weekly groceries"])
    folder_row(page, folders["Travel"])
    menu = e2e.native_menu(page, e2e.FOLDER_ROW_TRIGGER, folders["Travel"])
    menu.get_by_role("menuitem", name="Mute folder").wait_for(timeout=WAIT_MS)
    save(page, "folder-menu")
    ctx.close()


def shot_reply_unsent(pw, token, ids, folders):
    """A reply typed in a notification that could not be sent: the chat opens with it in the message box.
    Set up as the service worker does when the send fails: the text in its drafts cache, the chat opened
    with ?oc-reply=unsent."""
    chat = ids["Weekly groceries"]
    ctx, page = browser(pw, token)
    page.goto(BASE + "/")
    page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=WAIT_MS)
    page.evaluate("""([c, text]) => caches.open('oc-push-drafts').then((d) =>
        d.put('/oc-push/draft/' + c, new Response(JSON.stringify({text, at: Date.now()}))))""",
                  [chat, "Yes please, by aisle"])
    page.goto(f"{BASE}/c/{chat}?oc-reply=unsent")
    page.locator("#oc-push-notice").wait_for(timeout=WAIT_MS)
    save(page, "reply-unsent")
    ctx.close()


def shot_offline(pw, token, ids, folders):
    """No network: the chat read last opens from the device, read-only."""
    chat = ids["Weekly groceries"]
    ctx, page = browser(pw, token)
    e2e.warm(page, chat)
    ctx.set_offline(True)
    try:
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#oc-offline").wait_for(timeout=WAIT_MS)
        page.get_by_text("Want me to split it by store aisle?").wait_for(timeout=WAIT_MS)
        save(page, "offline")
    finally:
        ctx.set_offline(False)
        ctx.close()


def shot_updating(pw, token, ids, folders):
    """A chat opened before, on a slow network: the kept copy at once, "Updating…" until the network's comes."""
    chat = ids["Garage door won't close"]
    ctx, page = browser(pw, token)
    e2e.warm_kept(page, chat)
    page.goto(BASE + "/")
    page.locator("#chat-input").wait_for(timeout=WAIT_MS)
    gate, undo = e2e.slow_network(ctx, token, chat)
    try:
        page.goto(f"{BASE}/c/{chat}")
        page.locator("#oc-updating").wait_for(timeout=WAIT_MS)
        page.get_by_text("wipe both lenses", exact=False).first.wait_for(timeout=WAIT_MS)
        save(page, "updating")
    finally:
        undo()
        ctx.close()


VALVES_OF = """(title) => { const t = [...document.querySelectorAll('div, span, a')].find((e) => e.textContent.trim() === title
    && !e.children.length); let box = t; while (box && !box.querySelector('button[aria-label=Valves]')) box = box.parentElement;
    box.querySelector('button[aria-label=Valves]').click(); }"""


def shot_admin(pw, token, ids, folders):
    """Admin Panel > Functions: the function, on; and its valves."""
    ctx, page = browser(pw, token, DESKTOP)
    page.goto(BASE + "/admin/functions")
    page.get_by_text("Web Push", exact=True).first.wait_for(timeout=WAIT_MS)
    save(page, "admin-functions")
    page.evaluate(VALVES_OF, "Web Push")
    page.get_by_text("Vapid Private Key File").wait_for(timeout=WAIT_MS)
    save(page, "admin-valves")
    ctx.close()


SHOTS = {f.__name__[len("shot_"):].replace("_", "-"): f for f in (
    shot_enable, shot_bell, shot_bell_folder, shot_muted, shot_chat_menu, shot_folder_menu, shot_reply_unsent,
    shot_offline, shot_updating, shot_admin)}


def main():
    names = sys.argv[1:] or list(SHOTS)
    unknown = [n for n in names if n not in SHOTS]
    if unknown:
        sys.exit(f"unknown: {unknown}; known: {list(SHOTS)}")
    os.makedirs(OUT, exist_ok=True)
    token = setup()
    ids, folders = seed(token)
    with e2e.sync_playwright() as pw:
        for name in names:
            SHOTS[name](pw, token, ids, folders)


if __name__ == "__main__":
    main()
