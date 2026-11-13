"""
title: Web Push notifications
author: Eliav2
author_url: https://github.com/Eliav2/open-webui-pwa-push
version: 0.6.0
requirements: pywebpush==2.0.3
description: Event function. Sends a Web Push notification to each of the user's subscribed devices when a reply finishes: the chat's title and link, and the start of the reply only when PREVIEW_CHARS is on. Muted chats and chats in muted folders send nothing.
"""
# Docs: README.md. The page script (web/oc-push.js) keeps
# each device's subscription in this function's user valves; the service
# worker (web/oc-sw.js) shows the notification.
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("openclaw_web_push")

# Open WebUI's event names are a closed catalog (events.py `event_name`), so the
# OpenClaw Pipe marks its own `chat.finished` by `source`.
PIPE_SOURCE = "openclaw-pipe"
# Push services take at most 4096 bytes per message after encryption.
MAX_PAYLOAD_BYTES = 3000
MAX_TITLE_CHARS = 120
MAX_IMAGE_URL_CHARS = 500   # NOTIFICATION_ICON / _BADGE: room left in the push for the rest
# The VAPID subject push services take (RFC 8292 section 2.1); py_vapid drops any other,
# and every send then fails with "Missing 'sub' from claims".
SUBJECT_SCHEMES = ("mailto:", "https://")
# Apple's push service (web.push.apple.com) refuses the whole token, 403 BadJwtToken, when the
# subject names no public host: localhost, *.localhost, *.local or a bare name. FCM takes them.
LOCAL_HOST = "localhost"
LOCAL_HOST_SUFFIXES = (".localhost", ".local")
LABEL_DOT = "."   # a bare name (no dot) is no public host
# Open WebUI 0.13's chat_view_state contract (models/chat_views.py PanelTab.kind): a side chat
# (/btw) sits in its parent's saved view as a tab of this kind, naming the side chat in chatId.
SIDE_TAB_KIND = "side"
# The parent's URL names the side chat in this query parameter: oc-push.js opens the panel on its tab.
SIDE_PARAM = "oc-side"
# Written into the user's own valves (push_problem) when nothing can be sent; the page shows it.
NO_SUBJECT = ("Notifications can't be sent yet: the admin must set VAPID_SUBJECT in the Web Push "
              "function's valves (a mailto: address or an https: URL on a public host), since WEBUI_URL is not one.")
# A push service that never answers: the send is given up. A safety net for a
# hung connection, never expected to expire.
PUSH_REQUEST_TIMEOUT_S = 10
# The push service says the subscription is gone (RFC 8030 section 7.3): drop it.
GONE_STATUSES = (404, 410)
# A gone endpoint is also remembered, by fingerprint, in the user's `gone` valve: the browser may
# still hold that subscription (a push service can retire a token the browser never hears about),
# and the page then replaces it instead of saving the dead one again on every load.
# The most recent ones: a user has a few devices, each replacing its subscription now and then
# (a new profile, Open WebUI's update path), so 20 covers every one the page could still hold.
GONE_KEEP = 20
# Except a young subscription: FCM answers 404/410 for a token Chrome has just registered, then
# takes it. Measured in this repo's e2e (fresh profiles): 410 at +1, +2 and +4.5 s after the page
# saved it, the last after a push to it had gone through; procrastimate#255 saw up to ~25 s;
# reTerminal-eink-signage#11 keeps subscriptions younger than 10 min for the same reason. Dropping
# one would leave that device without notifications until its page saves it again: it is kept,
# and only a 404/410 once it is this old drops it.
YOUNG_SUBSCRIPTION_S = 600
# A notification refused that way is sent once more when the subscription is this old: past every
# offset above (FCM gives no signal that it has taken the token, only time), so the user's first
# notification after turning them on comes about this late instead of never. One resend per
# notification, no ladder: a second refusal is logged and the next notification tries again.
NEW_TOKEN_SETTLE_S = 30
MS = 1000                 # the page's `at`: Date.now(), milliseconds
FINGERPRINT_CHARS = 32    # hex digits of the endpoint's SHA-256 (web/oc-push.js computes the same)
# The notification's buttons the service worker knows (web/oc-sw.js):
# reply in the shade, mark the chat read, mute it for 8 hours.
KNOWN_ACTIONS = ("reply", "read", "mute")
# Mute writes this function's user valves, so it needs the function's id.
NEEDS_FUNCTION_ID = ("mute",)
DEFAULT_KEY_FILE = os.path.join(os.environ.get("DATA_DIR", "/app/backend/data"), "web-push", "vapid_private.pem")


# What a preview leaves out of the reply, and what it unwraps.
_BLOCKS = re.compile(r"<details\b.*?</details>", re.S | re.I)
_FENCES = re.compile(r"```.*?(```|$)", re.S)
_IMAGES = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MEDIA = re.compile(r"^\s*MEDIA:\S*\s*$", re.M)
_LINKS = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAGS = re.compile(r"<[^>]+>")
_LINE_MARKS = re.compile(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?)", re.M)
_INLINE_MARKS = re.compile(r"(\*\*|__|~~|`)")


def snippet(text: str, limit: int) -> str:
    """The start of a reply as one line of plain text, at most `limit` characters
    (cut on a word, with an ellipsis): tool and status blocks, images and media
    lines left out, a code block as "[code]", markdown and HTML unwrapped."""
    t = _BLOCKS.sub(" ", str(text or ""))
    t = _FENCES.sub(" [code] ", t)
    t = _MEDIA.sub(" ", _IMAGES.sub(" ", t))
    t = _LINKS.sub(r"\1", t)
    t = _TAGS.sub(" ", t)
    t = _INLINE_MARKS.sub("", _LINE_MARKS.sub("", t)).replace("|", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([:.,;!?])", r"\1", t)
    if t.replace("[code]", "").strip() == "":
        return ""
    if len(t) <= limit:
        return t
    cut = t[:limit - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:") + "…"


def is_on(raw, default: bool) -> bool:
    """A stored boolean valve: Open WebUI keeps it as JSON (true/false), an older or hand
    edit may be a string; unset or unreadable is the default."""
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    return {"true": True, "1": True, "on": True, "false": False, "0": False, "off": False}.get(text, default)


def parse_actions(raw: str) -> list:
    """NOTIFICATION_ACTIONS as a list: known names, in order, each once."""
    out = []
    for name in (x.strip() for x in str(raw or "").split(",")):
        if name in KNOWN_ACTIONS and name not in out:
            out.append(name)
    return out


def image_url(value: str) -> str:
    """A NOTIFICATION_ICON / NOTIFICATION_BADGE valve as the push carries it: a path on this
    site or an https: URL; anything else (a data: URL is too big for a push) is dropped."""
    value = (value or "").strip()
    ok = (value.startswith("/") and not value.startswith("//")) or value.startswith("https://")
    return value[:MAX_IMAGE_URL_CHARS] if ok and len(value) <= MAX_IMAGE_URL_CHARS else ""


def build_payload(event: dict, preview_chars: int = 0, fn: str = "", actions: list | None = None,
                  swipe_read: bool = True, icon: str = "", badge: str = "", open_chat: str = "") -> dict:
    """What the device gets: the chat's title and where to open it (`open_chat`, a side
    chat's parent, else the chat itself), the id of this function (the service worker's
    Mute writes its user valves), and, only with `preview_chars`, the start of the reply.
    `data.message` holds the whole reply (Open WebUI 0.13.0 and the Pipe); it is never sent whole."""
    data = event.get("data") or {}
    chat_id = str(data.get("chat_id") or "")
    title = str(data.get("title") or event.get("message") or "Reply finished")[:MAX_TITLE_CHARS]
    payload = {"v": 2, "kind": event.get("event"), "source": event.get("source"), "chat_id": chat_id,
               "message_id": data.get("message_id"), "title": title,
               "url": (f"/c/{open_chat}?{SIDE_PARAM}={chat_id}" if open_chat else f"/c/{chat_id}") if chat_id else "/"}
    if fn:
        payload["fn"] = fn
    if not swipe_read:
        payload["swipe_read"] = False
    for key, value in (("icon", image_url(icon)), ("badge", image_url(badge))):
        if value:
            payload[key] = value
    names = parse_actions(Event.Valves.model_fields["NOTIFICATION_ACTIONS"].default) if actions is None else actions
    names = [a for a in names if fn or a not in NEEDS_FUNCTION_ID]
    if chat_id and names:
        payload["actions"] = names
    if preview_chars > 0:
        body = snippet(data.get("message") or "", preview_chars)
        if body:
            payload["body"] = body
    return payload


def encode_payload(payload: dict) -> str:
    """JSON within MAX_PAYLOAD_BYTES: the preview is shortened first, then the title."""
    def dump(p):
        return json.dumps(p, separators=(",", ":"), ensure_ascii=False)
    body = dump(payload)
    while len(body.encode()) > MAX_PAYLOAD_BYTES and payload.get("body"):
        over = len(body.encode()) - MAX_PAYLOAD_BYTES
        # Each character is 1 to 4 bytes: drop at least what is over, in characters.
        keep = max(0, len(payload["body"]) - 1 - over // 2 - 1)
        payload = {**payload, "body": snippet(payload["body"].rstrip("…"), keep) if keep > 20 else ""}
        if not payload["body"]:
            payload.pop("body")
        body = dump(payload)
    if len(body.encode()) > MAX_PAYLOAD_BYTES:
        body = dump({**payload, "title": payload["title"][:40]})
    return body


def active_mutes(raw, now: float | None = None) -> dict:
    """A mutes user valve, {id: until} in epoch seconds with 0 for always, as
    the entries still in effect. Anything unreadable mutes nothing."""
    try:
        mutes = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    if not isinstance(mutes, dict):
        return {}
    now = time.time() if now is None else now
    return {k: v for k, v in mutes.items()
            if not isinstance(v, bool) and isinstance(v, (int, float)) and (v == 0 or v > now)}


def is_muted(raw, chat_id: str, now: float | None = None) -> bool:
    """`mutes` user valve: {chat_id: until}."""
    return chat_id in active_mutes(raw, now)


def muted_folder(raw, folder_id, parent_by_id: dict, now: float | None = None):
    """`folder_mutes` user valve: {folder_id: until}. The muted folder that
    holds `folder_id` (itself or a folder above it), or None."""
    mutes = active_mutes(raw, now)
    seen = set()
    while mutes and folder_id and folder_id not in seen:
        if folder_id in mutes:
            return folder_id
        seen.add(folder_id)
        folder_id = parent_by_id.get(folder_id)
    return None


def topic_for(chat_id: str) -> str:
    """RFC 8030 Topic (at most 32 base64url characters): a newer push for the
    same chat replaces one the push service still holds."""
    return base64.urlsafe_b64encode(hashlib.sha256(chat_id.encode()).digest()[:24]).decode().rstrip("=")


def subject_problem(v: str) -> str:
    """Why `v` can't be the VAPID subject; "" when it can."""
    if not v.startswith(SUBJECT_SCHEMES):
        return "must be a mailto: address or an https: URL"
    host = v.split("?")[0].split("@")[-1] if v.startswith("mailto:") else (urlsplit(v).hostname or "")
    host = host.lower().rstrip(LABEL_DOT)
    if host == LOCAL_HOST or host.endswith(LOCAL_HOST_SUFFIXES) or LABEL_DOT not in host:
        return "must name a public host (Apple refuses localhost, .local and bare names)"
    return ""


def parse_subscriptions(raw) -> list:
    try:
        subs = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
    except ValueError:
        return []
    return [s for s in subs if isinstance(s, dict) and s.get("endpoint") and (s.get("keys") or {}).get("p256dh")]


def should_send(event: dict, pipe_prefixes: list) -> bool:
    """`chat.finished` only. For an OpenClaw model, only the Pipe's own: Open
    WebUI's fires when the chat request ends, which for a reply the Pipe
    handed over is before the reply does, and the Pipe publishes every reply
    once it is saved (valve PUBLISH_CHAT_FINISHED)."""
    if event.get("event") != "chat.finished":
        return False
    model_id = str((event.get("data") or {}).get("model_id") or "")
    if any(model_id.startswith(p) for p in pipe_prefixes):
        return event.get("source") == PIPE_SOURCE
    return True


class Event:
    class Valves(BaseModel):
        VAPID_PRIVATE_KEY_FILE: str = Field(
            default=DEFAULT_KEY_FILE,
            description="Path, inside the Open WebUI container, of the VAPID private key (PEM). "
                        "Kept in a file because valves are stored unencrypted by default.")
        VAPID_SUBJECT: str = Field(
            default="",
            description="Contact the push services may use: a mailto: address or an https: URL. Empty: "
                        "Open WebUI's WEBUI_URL, which must then be https (otherwise nothing is sent).")
        TTL_SECONDS: int = Field(
            default=3600, ge=0,
            description="How long a push service keeps a notification for a device that is offline.")
        URGENCY: str = Field(
            default="normal",
            description="RFC 8030 Urgency: very-low, low, normal or high.")
        PIPE_MODEL_PREFIXES: str = Field(
            default="openclaw_gateway.",
            description="Comma list of model id prefixes whose replies come from the OpenClaw Pipe "
                        "(its function id plus a dot). For these, only the Pipe's own "
                        "'chat.finished' is sent. Turn on the Pipe's PUBLISH_CHAT_FINISHED.")
        NOTIFICATION_ACTIONS: str = Field(
            default="reply,mute",
            description="The notification's buttons, in order: reply (a text field in the "
                        "notification, sent as your message), read (marks the chat read), mute "
                        "(this chat, 8 hours). Android Chrome shows the first two; iOS shows none.")
        NOTIFICATION_ICON: str = Field(
            default="",
            description="The picture beside the notification's text: a path on this site "
                        "(/static/...) or an https: URL. Empty: Open WebUI's own app icon.")
        NOTIFICATION_BADGE: str = Field(
            default="",
            description="Android's small icon (status bar): drawn from the picture's transparency "
                        "only, so a white shape on a transparent background. A path on this site "
                        "or an https: URL. Empty: the browser's own.")
        PREVIEW_CHARS: int = Field(
            default=0, ge=0, le=2000,
            description="Show the start of the reply (plain text, at most this many characters) "
                        "in the notification. 0: off, the body says 'New reply'. The text shows on "
                        "the lock screen and in the notification shade of every subscribed device.")

        @field_validator("VAPID_SUBJECT")
        @classmethod
        def _usable_subject(cls, v: str) -> str:
            """Refused when the admin saves it (Open WebUI shows the error): push services take only these."""
            v = (v or "").strip()
            if v and (problem := subject_problem(v)):
                raise ValueError(f"VAPID_SUBJECT {problem} (for example mailto:admin@example.com), not {v!r}")
            return v

    class UserValves(BaseModel):
        subscriptions: str = Field(
            default="[]",
            description="Written by the page script: one Web Push subscription per device.")
        mutes: str = Field(
            default="{}",
            description="Written by the page script and the service worker: chats that send no "
                        "notification, {chat_id: until} in epoch seconds, 0 for always.")
        folder_mutes: str = Field(
            default="{}",
            description="Written by the page script: folders whose chats send no notification, "
                        "folders inside them included, {folder_id: until} as in mutes.")
        swipe_read: bool = Field(
            default=True,
            description="A notification swiped away marks its chat read (Android Chrome; "
                        "'Clear all' marks every chat in it read). Off: a swipe only hides it.")
        gone: str = Field(
            default="[]",
            description="Written by the function: fingerprints of this user's subscriptions the push "
                        "service says are gone. The page replaces such a subscription. Not set by hand.")
        push_problem: str = Field(
            default="",
            description="Written by the function: why it cannot send your notifications (empty: it "
                        "can). The page shows it. Not set by hand.")

    def __init__(self):
        self.valves = self.Valves()
        self._subject_logged = False   # the missing subject's error, once per load of the function
        self._pending = set()          # notifications to send again (_resend)

    async def event(self, event: dict, __id__: str = "", **kwargs):
        prefixes = [p.strip() for p in self.valves.PIPE_MODEL_PREFIXES.split(",") if p.strip()]
        if not should_send(event, prefixes):
            return
        data = event.get("data") or {}
        user_id = data.get("user_id") or (event.get("actor") or {}).get("id")
        if not user_id:
            return
        from open_webui.models.functions import Functions

        fid = __id__ or "openclaw_web_push"
        valves = await Functions.get_user_valves_by_id_and_user_id(fid, user_id) or {}
        subs = parse_subscriptions(valves.get("subscriptions"))
        if not subs:
            return
        chat_id = str(data.get("chat_id") or "")
        # Before anything is encrypted or sent: a muted chat sends nothing, to any device.
        if is_muted(valves.get("mutes"), chat_id):
            log.info("web push: chat is muted, nothing sent chat_id=%s", chat_id)
            return
        chat = await self._chat(chat_id)
        folder = await self._muted_folder(valves.get("folder_mutes"), chat, user_id)
        if folder:
            log.info("web push: folder %s is muted, nothing sent chat_id=%s", folder[:8], chat_id)
            return
        key = self._private_key()
        if key is None:
            return
        subject = await self._subject()
        # Said where the user sees it (the page shows push_problem), and cleared once it can send.
        problem = "" if subject else NO_SUBJECT
        if (valves.get("push_problem") or "") != problem:
            await Functions.update_user_valves_by_id_and_user_id(fid, user_id, {**valves, "push_problem": problem})
        if not subject:
            log.info("web push: no VAPID subject, nothing sent chat_id=%s", chat_id)
            return
        body = encode_payload(build_payload(event, self.valves.PREVIEW_CHARS, fid,
                                            parse_actions(self.valves.NOTIFICATION_ACTIONS),
                                            swipe_read=is_on(valves.get("swipe_read"), True),
                                            icon=self.valves.NOTIFICATION_ICON, badge=self.valves.NOTIFICATION_BADGE,
                                            open_chat=await self._side_chat_parent(chat, user_id)))
        early = []
        gone = await asyncio.to_thread(self.send_all, subs, body, topic_for(chat_id),
                                       key, subject, chat_id, early)
        if gone:
            await self._drop(Functions, fid, user_id, gone)
        if early:
            task = asyncio.create_task(self._resend(early, body, topic_for(chat_id), key, subject, chat_id))
            self._pending.add(task)   # held until done: the loop keeps only weak references
            task.add_done_callback(self._pending.discard)

    async def _resend(self, subs: list, body: str, topic: str, key: bytes, subject: str, chat_id: str) -> None:
        """Once, when the youngest of these just-saved subscriptions is NEW_TOKEN_SETTLE_S old. A second
        refusal is only logged (send_all without `early`): the next notification tries again."""
        youngest = max((s.get("at") or 0) / MS for s in subs)
        await asyncio.sleep(max(0.0, youngest + NEW_TOKEN_SETTLE_S - time.time()))
        log.info("web push: sending again to %d just-saved subscription(s) chat_id=%s", len(subs), chat_id)
        await asyncio.to_thread(self.send_all, subs, body, topic, key, subject, chat_id)

    @staticmethod
    async def _chat(chat_id: str):
        if not chat_id:
            return None
        from open_webui.models.chats import Chats

        return await Chats.get_chat_by_id(chat_id)

    @staticmethod
    async def _side_chat_parent(chat, user_id: str) -> str:
        """The parent of an Open WebUI 0.13 side chat (/btw), or "". A side chat is a chat of its own
        whose JSON names the parent (`originalChatId`) and which sits as a side tab in the user's saved
        view of the parent; opening the parent shows it in the panel. A fork or a clone also names
        an `originalChatId`, but no side tab of its parent holds it, so it opens itself."""
        parent = str(((chat and chat.chat) or {}).get("originalChatId") or "")
        if not parent:
            return ""
        from open_webui.internal.db import get_async_db
        from open_webui.models.chat_views import ChatViewStates

        async with get_async_db() as db:
            view = await ChatViewStates.get(user_id, parent, db) or {}
        tabs = (view.get("panels") or {}).get("tabs") or []
        return parent if any(t.get("kind") == SIDE_TAB_KIND and t.get("chatId") == chat.id for t in tabs) else ""

    @staticmethod
    async def _muted_folder(raw, chat, user_id: str):
        """The muted folder that holds the chat, or None. Reads the user's
        folders only while a folder mute is in effect."""
        if not chat or not active_mutes(raw):
            return None
        from open_webui.models.folders import Folders

        if not chat.folder_id:
            return None
        parents = {f.id: f.parent_id for f in await Folders.get_folders_by_user_id(user_id)}
        return muted_folder(raw, chat.folder_id, parents)

    def _private_key(self):
        path = self.valves.VAPID_PRIVATE_KEY_FILE
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as ex:
            log.error("web push: cannot read the VAPID key %s: %s", path, ex)
            return None

    async def _subject(self) -> str:
        """VAPID_SUBJECT (the valve refuses any other), else Open WebUI's WEBUI_URL when it is an https URL
        on a public host. Neither: "", and nothing can be sent."""
        if self.valves.VAPID_SUBJECT:
            return self.valves.VAPID_SUBJECT
        try:
            from open_webui.models.config import Config
            url = (await Config.get("webui.url") or "").rstrip("/")
        except Exception:
            url = ""
        problem = subject_problem(url)
        if not problem:
            return url
        if not self._subject_logged:
            self._subject_logged = True
            log.error("web push: nothing can be sent: VAPID_SUBJECT is empty and WEBUI_URL %r %s. "
                      "Set VAPID_SUBJECT (a mailto: address or an https: URL on a public host) in the "
                      "function's valves.", url, problem)
        return ""

    def send_all(self, subs: list, body: str, topic: str, key: bytes, subject: str, chat_id: str,
                 early: list | None = None) -> set:
        """Send `body` to each subscription; returns the endpoints the push
        service says are gone. `key`: the VAPID private key, PEM. Each line
        ends with `chat_id=<id>`, which central logs index (ELI-368). `early`:
        collects the just-saved subscriptions that were refused (sent again later)."""
        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush

        try:
            vapid = Vapid.from_pem(key)
        except Exception as ex:
            log.error("web push: the VAPID key file is not a PEM private key: %r", ex)
            return set()
        gone = set()
        for sub in subs:
            info = {"endpoint": sub["endpoint"], "keys": sub["keys"]}
            try:
                webpush(subscription_info=info, data=body, vapid_private_key=vapid,
                        vapid_claims={"sub": subject}, ttl=self.valves.TTL_SECONDS,
                        headers={"Urgency": self.valves.URGENCY, "Topic": topic},
                        content_encoding="aes128gcm", timeout=PUSH_REQUEST_TIMEOUT_S)
                log.info("web push: sent to %s chat_id=%s", _host(sub), chat_id)
            except WebPushException as ex:
                status = getattr(ex.response, "status_code", None)
                age = time.time() - (sub.get("at") or 0) / MS
                if status in GONE_STATUSES and age < YOUNG_SUBSCRIPTION_S:
                    log.warning("web push: %s answered %s for a subscription saved %d s ago: kept (a new token "
                                "is not taken everywhere at once)%s chat_id=%s", _host(sub), status, age,
                                ", sent again later" if early is not None else "", chat_id)
                    if early is not None:
                        early.append(sub)
                elif status in GONE_STATUSES:
                    gone.add(sub["endpoint"])
                    log.info("web push: subscription gone (%s), dropping %s chat_id=%s", status, _host(sub), chat_id)
                else:
                    log.warning("web push: %s answered %s: %s chat_id=%s", _host(sub), status, str(ex)[:200], chat_id)
            except Exception as ex:
                log.warning("web push: sending to %s failed: %r chat_id=%s", _host(sub), ex, chat_id)
        return gone

    async def _drop(self, Functions, fid: str, user_id: str, gone: set) -> None:
        # Read again: a device may have saved its subscription meanwhile.
        valves = await Functions.get_user_valves_by_id_and_user_id(fid, user_id) or {}
        subs = parse_subscriptions(valves.get("subscriptions"))
        keep = [s for s in subs if s["endpoint"] not in gone]
        prints = parse_list(valves.get("gone"))
        new = [fingerprint(e) for e in sorted(gone) if fingerprint(e) not in prints]
        if len(keep) != len(subs) or new:
            await Functions.update_user_valves_by_id_and_user_id(fid, user_id, {
                **valves, "subscriptions": json.dumps(keep), "gone": json.dumps((prints + new)[-GONE_KEEP:])})


def fingerprint(endpoint: str) -> str:
    """The endpoint as the page compares it, without storing the token itself once more."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:FINGERPRINT_CHARS]


def parse_list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _host(sub: dict) -> str:
    return str(sub.get("endpoint", "")).split("/")[2:3][0] if "//" in str(sub.get("endpoint", "")) else "?"
