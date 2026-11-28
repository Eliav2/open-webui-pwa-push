"""The Web Push function (openclaw_web_push.py): title and link only, OpenClaw
events only from the Pipe, and a real encrypted send (tests/web_push_roundtrip.py)."""
import importlib.util
import hashlib
import asyncio
import contextlib
import time
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("openclaw_web_push", os.path.join(ROOT, "openclaw_web_push.py"))
web_push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web_push)

REPLY = "SECRET-REPLY-TEXT the whole answer the agent wrote"
STATE_KEY = "web_push:web_push"   # where the function's state is in a user's settings, for the id the tests send
PIPE = ["openclaw_gateway."]


def finished(source="openclaw-pipe", model="openclaw_gateway.main", **data):
    return {"event": "chat.finished", "source": source, "actor": {"id": "u1"},
            "data": {"user_id": "u1", "chat_id": "c1", "message_id": "m1", "model_id": model,
                     "title": "Trip plan", "url": "https://chat.example.com/c/c1", "message": REPLY, **data}}


def channel_message(content="hello there", **data):
    """What Open WebUI publishes per recipient (routers/channels.py send_notification): `user_id` is the
    recipient, `sender_id` who wrote it; `title` is the channel's name, empty for a DM."""
    return {"event": "channel.message", "source": "api", "actor": None,
            "data": {"user_id": "u1", "channel_id": "ch1", "message_id": "m1", "sender_id": "u2",
                     "content": content, "message": f"#Japan - https://chat.example.com/channels/ch1\n\n{content}",
                     "content_preview": content[:300], "title": "Japan",
                     "url": "https://chat.example.com/channels/ch1", **data}}


class PayloadTests(unittest.TestCase):
    def test_the_payload_never_contains_the_reply(self):
        """chat.finished carries the whole reply; the device gets only title and link."""
        body = web_push.encode_payload(web_push.build_payload(finished()))
        self.assertNotIn("SECRET-REPLY-TEXT", body)
        self.assertEqual(json.loads(body), {"v": 2, "kind": "chat.finished", "source": "openclaw-pipe", "chat_id": "c1", "message_id": "m1",
                                            "title": "Trip plan", "url": "/c/c1", "actions": ["reply"]})

    def test_the_link_is_relative_whatever_the_event_says(self):
        payload = web_push.build_payload(finished(url="https://elsewhere.example/phish"))
        self.assertEqual(payload["url"], "/c/c1")

    def test_the_link_can_open_another_chat_than_the_one_it_is_about(self):
        payload = web_push.build_payload(finished(), open_chat="p1")
        self.assertEqual((payload["url"], payload["chat_id"]), ("/c/p1?oc-side=c1", "c1"))

    def test_a_long_title_is_cut_and_the_payload_fits(self):
        body = web_push.encode_payload(web_push.build_payload(finished(title="x" * 10000)))
        self.assertLessEqual(len(body.encode()), web_push.MAX_PAYLOAD_BYTES)
        self.assertEqual(len(json.loads(body)["title"]), web_push.MAX_TITLE_CHARS)

    def test_a_chat_without_a_title(self):
        self.assertEqual(web_push.build_payload(finished(title=""))["title"], "Reply finished")

    def test_the_topic_is_a_short_base64url_per_chat(self):
        a, b = web_push.topic_for("c1"), web_push.topic_for("c2")
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a), 32)
        self.assertRegex(a, r"^[A-Za-z0-9_-]+$")


class WhichEventsTests(unittest.TestCase):
    def test_the_pipes_own_chat_finished_is_sent(self):
        self.assertTrue(web_push.should_send(finished(), PIPE))

    def test_open_webuis_chat_finished_for_an_openclaw_model_is_skipped(self):
        """For a handed-over reply the request ends first; the Pipe publishes that reply itself."""
        self.assertFalse(web_push.should_send(finished(source="api"), PIPE))

    def test_open_webuis_chat_finished_for_another_model_is_sent(self):
        self.assertTrue(web_push.should_send(finished(source="api", model="gpt-5"), PIPE))

    def test_other_events_are_not_sent(self):
        self.assertFalse(web_push.should_send({**finished(), "event": "chat.failed"}, PIPE))

    def test_a_channel_message_is_sent_unless_the_kill_switch_is_off(self):
        self.assertTrue(web_push.should_send(channel_message(), PIPE))
        self.assertTrue(web_push.should_send(channel_message(), PIPE, channel_push=True))
        self.assertFalse(web_push.should_send(channel_message(), PIPE, channel_push=False))

    def test_the_kill_switch_leaves_chat_finished_alone(self):
        self.assertTrue(web_push.should_send(finished(), PIPE, channel_push=False))

    def test_a_channel_message_is_never_held_back_by_the_pipe_prefixes(self):
        """A channel message has no model_id: the Pipe rule is for chat.finished."""
        self.assertTrue(web_push.should_send(channel_message(model_id="openclaw_gateway.main"), PIPE))

    def test_no_prefixes_sends_every_chat_finished(self):
        self.assertTrue(web_push.should_send(finished(source="api"), []))

    def test_malformed_subscriptions_are_ignored(self):
        raw = json.dumps([{"endpoint": "https://p/1", "keys": {"p256dh": "k", "auth": "a"}},
                          {"endpoint": "https://p/2"}, "junk", {"keys": {"p256dh": "k"}}])
        self.assertEqual([s["endpoint"] for s in web_push.parse_subscriptions(raw)], ["https://p/1"])
        self.assertEqual(web_push.parse_subscriptions("not json"), [])


class ChannelPayloadTests(unittest.TestCase):
    def payload(self, event=None, **kw):
        return web_push.build_channel_payload(event or channel_message(), **kw)

    def test_a_group_message_shows_the_channel_name_and_opens_the_channel(self):
        p = self.payload()
        self.assertEqual((p["kind"], p["channel_id"], p["title"], p["url"]),
                         ("channel.message", "ch1", "#Japan", "/channels/ch1"))

    def test_a_dm_shows_the_senders_name(self):
        """A DM has no name of its own (`title` is empty): the sender's name is the title."""
        self.assertEqual(self.payload(channel_message(title=""), sender_name="Ortal")["title"], "Ortal")

    def test_a_dm_without_a_sender_name_still_has_a_title(self):
        self.assertEqual(self.payload(channel_message(title=""))["title"], web_push.CHANNEL_DEFAULT_TITLE)

    def test_it_carries_no_chat_id_and_no_buttons(self):
        """Reply, read and mute act on a chat: a channel notification has none of them."""
        p = self.payload(actions=["reply", "mute"], fn="web_push")
        for key in ("chat_id", "actions", "swipe_read"):
            self.assertNotIn(key, p)

    def test_the_content_is_not_sent_by_default(self):
        body = web_push.encode_payload(self.payload(channel_message("SECRET-CHANNEL-TEXT")))
        self.assertNotIn("SECRET-CHANNEL-TEXT", body)
        self.assertNotIn("body", json.loads(body))

    def test_the_preview_valve_puts_the_start_of_the_message_in_the_body(self):
        self.assertEqual(self.payload(channel_message("a long line of words"), preview_chars=10)["body"], "a long…")
        self.assertNotIn("body", self.payload(preview_chars=0))

    def test_a_mention_is_shown_as_its_label_in_the_preview(self):
        p = self.payload(channel_message("hi <@U:u1|Eliav> and <@M:gpt|GPT-4>, ok?"), preview_chars=100)
        self.assertEqual(p["body"], "hi @Eliav and @GPT-4, ok?")

    def test_a_channel_id_is_safe_in_the_url(self):
        self.assertEqual(self.payload(channel_message(channel_id="a/b c"))["url"], "/channels/a%2Fb%20c")

    def test_the_payload_stays_under_the_push_limit(self):
        long = "word " * 2000
        body = web_push.encode_payload(self.payload(channel_message(long, title="x" * 500), preview_chars=2000))
        self.assertLessEqual(len(body.encode()), web_push.MAX_PAYLOAD_BYTES)

    def test_the_topic_of_a_channel_differs_from_a_chat_with_the_same_id(self):
        self.assertNotEqual(web_push.channel_topic("ch1"), web_push.topic_for("ch1"))
        self.assertLessEqual(len(web_push.channel_topic("ch1")), 32)


class PreviewTests(unittest.TestCase):
    """PREVIEW_CHARS > 0: a plain-text start of the reply as the notification body."""

    def test_off_by_default_no_body(self):
        self.assertEqual(web_push.Event.Valves().PREVIEW_CHARS, 0)
        self.assertNotIn("body", web_push.build_payload(finished()))

    def test_on_the_start_of_the_reply_as_plain_text(self):
        md = ("# Plan\n\nHere is **the plan** for [Kyoto](https://x.example/k):\n\n- day one\n- day two\n\n"
              "```python\nprint('x')\n```\n\n![map](/img.png)\nMEDIA:/tmp/a.png\n"
              "<details type=\"tool_calls\"><summary>Tool</summary>\nsecret tool output\n</details>\nDone.")
        body = web_push.snippet(md, 600)
        self.assertEqual(body, "Plan Here is the plan for Kyoto: day one day two [code] Done.")

    def test_cut_on_a_word_with_an_ellipsis(self):
        self.assertEqual(web_push.snippet("one two three four", 9), "one two…")

    def test_hebrew_is_kept_as_is(self):
        self.assertEqual(web_push.snippet("**שלום** עולם, מה נשמע?", 600), "שלום עולם, מה נשמע?")

    def test_the_payload_with_a_preview_fits_a_push(self):
        p = web_push.build_payload(finished(message="א" * 5000), preview_chars=4000, fn="web_push")
        body = web_push.encode_payload(p)
        self.assertLessEqual(len(body.encode()), web_push.MAX_PAYLOAD_BYTES)
        self.assertTrue(json.loads(body)["body"].startswith("אאא"))
        self.assertEqual(json.loads(body)["fn"], "web_push")

    def test_an_empty_reply_has_no_body(self):
        self.assertNotIn("body", web_push.build_payload(finished(message="```\nx\n```"), preview_chars=600))


class ActionsTests(unittest.TestCase):
    """NOTIFICATION_ACTIONS: the buttons, in order (Android Chrome shows the first two)."""

    def test_the_default_is_reply_and_mute(self):
        self.assertEqual(web_push.Event.Valves().NOTIFICATION_ACTIONS, "reply,mute")
        self.assertEqual(web_push.build_payload(finished(), fn="web_push")["actions"], ["reply", "mute"])

    def test_the_valve_order_unknown_names_and_repeats(self):
        self.assertEqual(web_push.parse_actions(" read , reply,nope,read"), ["read", "reply"])
        self.assertEqual(web_push.parse_actions(""), [])

    def test_mute_needs_the_function_id(self):
        """Mute writes this function's state: without its id, no Mute button."""
        self.assertEqual(web_push.build_payload(finished(), actions=["reply", "mute", "read"])["actions"], ["reply", "read"])

    def test_no_chat_no_buttons(self):
        self.assertNotIn("actions", web_push.build_payload(finished(chat_id=""), fn="web_push"))


class MuteTests(unittest.TestCase):
    def test_always_and_until_and_expired(self):
        mutes = json.dumps({"c1": 0, "c2": 2000, "c3": 500})
        self.assertTrue(web_push.is_muted(mutes, "c1", now=1000))
        self.assertTrue(web_push.is_muted(mutes, "c2", now=1000))
        self.assertFalse(web_push.is_muted(mutes, "c3", now=1000))
        self.assertFalse(web_push.is_muted(mutes, "c4", now=1000))

    def test_malformed_mutes_mute_nothing(self):
        for raw in ("not json", "[1]", json.dumps({"c1": "soon"}), None, ""):
            self.assertFalse(web_push.is_muted(raw, "c1", now=1000), raw)


class FolderMuteTests(unittest.TestCase):
    """`folder_mutes`: {folder_id: until}; a folder's mute covers the folders inside it."""
    PARENTS = {"qa": None, "qa-old": "qa", "qa-old-2025": "qa-old", "work": None}

    def test_the_folder_itself_and_every_folder_inside_it(self):
        raw = json.dumps({"qa": 0})
        for folder in ("qa", "qa-old", "qa-old-2025"):
            self.assertEqual(web_push.muted_folder(raw, folder, self.PARENTS, now=1000), "qa", folder)
        self.assertIsNone(web_push.muted_folder(raw, "work", self.PARENTS, now=1000))

    def test_until_expired_and_no_folder(self):
        self.assertEqual(web_push.muted_folder(json.dumps({"qa-old": 2000}), "qa-old-2025", self.PARENTS, now=1000), "qa-old")
        self.assertIsNone(web_push.muted_folder(json.dumps({"qa": 500}), "qa", self.PARENTS, now=1000))
        self.assertIsNone(web_push.muted_folder(json.dumps({"qa": 0}), None, self.PARENTS, now=1000))

    def test_a_parent_cycle_ends(self):
        self.assertIsNone(web_push.muted_folder(json.dumps({"x": 0}), "a", {"a": "b", "b": "a"}, now=1000))

    def test_malformed_folder_mutes_mute_nothing(self):
        for raw in ("not json", "[1]", json.dumps({"qa": "soon"}), None, ""):
            self.assertIsNone(web_push.muted_folder(raw, "qa", self.PARENTS, now=1000), raw)


class EventTests(unittest.IsolatedAsyncioTestCase):
    """`event` with Open WebUI's Functions stubbed and the send recorded."""

    async def asyncSetUp(self):
        self.store = {"subscriptions": json.dumps([{"endpoint": "https://push.example/1",
                                                    "keys": {"p256dh": "k", "auth": "a"}}])}
        store = self.store

        # What 0.6.0 kept in the user valves (empty unless a test sets it), and the user's Open WebUI settings,
        # where the function's state lives under STATE_KEY. `self.store` is that state (updated in place).
        self.legacy = {}
        legacy = self.legacy
        self.settings = {"theme": "dark", STATE_KEY: store}
        settings = self.settings

        class Functions:
            @staticmethod
            async def get_user_valves_by_id_and_user_id(fid, user_id):
                return dict(legacy) if legacy else None

            @staticmethod
            async def update_user_valves_by_id_and_user_id(fid, user_id, valves):
                raise AssertionError("the user valves are not written any more (ELI-442)")

        class Users:
            @staticmethod
            async def get_user_by_id(user_id):
                return types.SimpleNamespace(name=f"name-{user_id}",
                                             settings=types.SimpleNamespace(model_dump=lambda: dict(settings)))

            @staticmethod
            async def update_user_settings_by_id(user_id, updated):
                # Open WebUI 0.13: only the top-level keys it is sent are replaced.
                for key, value in updated.items():
                    if key == STATE_KEY:
                        store.clear()
                        store.update(value)
                    else:
                        settings[key] = value
        # c1 is in folder "qa-old", inside "qa".
        self.folder_reads = []
        reads = self.folder_reads

        # The chat's own JSON (originalChatId: a side chat, a fork or a clone) and the user's panel views, per test.
        self.chat_json = {}
        self.views = {}
        chat_json, views = self.chat_json, self.views

        class Chats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                return types.SimpleNamespace(id=chat_id, folder_id="qa-old" if chat_id == "c1" else None,
                                             chat=dict(chat_json))

        class ChatViewStates:
            @staticmethod
            async def get(user_id, chat_id, db):
                return views.get((user_id, chat_id))

        @contextlib.asynccontextmanager
        async def get_async_db():
            yield None

        # (channel id, user id) of each channel membership; u1 is in ch1 and ch2 unless a test changes it.
        self.members = {("ch1", "u1"), ("ch2", "u1")}
        members = self.members

        class Channels:
            @staticmethod
            async def get_member_by_channel_and_user_id(channel_id, user_id, db=None):
                return types.SimpleNamespace(user_id=user_id) if (channel_id, user_id) in members else None

        class Folders:
            @staticmethod
            async def get_folders_by_user_id(user_id):
                reads.append(user_id)
                return [types.SimpleNamespace(id="qa", parent_id=None), types.SimpleNamespace(id="qa-old", parent_id="qa")]
        mods = {"open_webui": types.ModuleType("open_webui"), "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.functions": types.ModuleType("open_webui.models.functions"),
                "open_webui.models.users": types.ModuleType("open_webui.models.users"),
                "open_webui.models.chats": types.ModuleType("open_webui.models.chats"),
                "open_webui.models.folders": types.ModuleType("open_webui.models.folders"),
                "open_webui.models.chat_views": types.ModuleType("open_webui.models.chat_views"),
                "open_webui.models.channels": types.ModuleType("open_webui.models.channels"),
                "open_webui.internal": types.ModuleType("open_webui.internal"),
                "open_webui.internal.db": types.ModuleType("open_webui.internal.db")}
        mods["open_webui.models.functions"].Functions = Functions
        mods["open_webui.models.users"].Users = Users
        mods["open_webui.models.chats"].Chats = Chats
        mods["open_webui.models.folders"].Folders = Folders
        mods["open_webui.models.chat_views"].ChatViewStates = ChatViewStates
        mods["open_webui.models.channels"].Channels = Channels
        mods["open_webui.internal.db"].get_async_db = get_async_db
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)
        self.fn = web_push.Event()
        key = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        key.write("PEM")
        key.close()
        self.addCleanup(os.unlink, key.name)
        self.fn.valves = self.fn.Valves(VAPID_PRIVATE_KEY_FILE=key.name, VAPID_SUBJECT="mailto:a@example.com")
        self.sent = []
        self.fn.send_all = lambda subs, body, topic, key, subject, chat_id, early=None: self.sent.append((subs, body, key, subject)) or set()

    async def resend_case(self, refuse):
        """event() with a send that the push service refuses (or not) for a just-saved subscription."""
        calls = []

        def send_all(subs, body, topic, key, subject, chat_id, early=None):
            calls.append((body, early is not None))
            if refuse and early is not None:
                early.extend(subs)
            return set()
        self.fn.send_all = send_all
        with mock.patch.object(web_push, "NEW_TOKEN_SETTLE_S", 0):   # no real wait in the test
            await self.fn.event(finished(), __id__="web_push")
            await asyncio.gather(*list(self.fn._pending))
        return calls

    async def test_a_refused_new_subscription_gets_the_notification_once_more(self):
        """FCM refuses a token Chrome registered seconds ago, then takes it: the notification is sent
        again once, when the subscription is NEW_TOKEN_SETTLE_S old; that send collects nothing more."""
        (body1, collects1), (body2, collects2) = await self.resend_case(refuse=True)
        self.assertEqual((body1, collects1, collects2), (body2, True, False))

    async def test_no_second_send_when_the_first_went_through(self):
        self.assertEqual(len(await self.resend_case(refuse=False)), 1)

    async def test_a_reply_is_sent_with_the_key_from_the_file(self):
        await self.fn.event(finished(), __id__="web_push")
        (subs, body, key, subject), = self.sent
        self.assertEqual((key, subject), (b"PEM", "mailto:a@example.com"))
        self.assertNotIn("SECRET-REPLY-TEXT", body)

    async def test_swipe_read_is_on_unless_the_user_turned_it_off(self):
        """The user's swipe_read valve reaches the device: unset (default on) sends nothing
        extra, off sends swipe_read: false (the service worker then only hides a swiped one)."""
        await self.fn.event(finished(), __id__="web_push")
        self.assertNotIn("swipe_read", json.loads(self.sent[-1][1]))
        for off in (False, "false"):
            self.store["swipe_read"] = off
            await self.fn.event(finished(), __id__="web_push")
            self.assertIs(json.loads(self.sent[-1][1])["swipe_read"], False)
        self.store["swipe_read"] = True
        await self.fn.event(finished(), __id__="web_push")
        self.assertNotIn("swipe_read", json.loads(self.sent[-1][1]))
        self.assertTrue(web_push.Event.Valves().SWIPE_READ)

    async def test_the_function_has_no_user_valves(self):
        """ELI-442: a function with user valves gets a valves button and a Controls panel entry whose save
        can write stale values over the subscriptions and mutes. The state is in the user's settings."""
        self.assertFalse(hasattr(web_push.Event, "UserValves"))

    async def test_the_swipe_read_valve_off_wins_over_a_users_old_on(self):
        self.fn.valves.SWIPE_READ = False
        self.store["swipe_read"] = True
        await self.fn.event(finished(), __id__="web_push")
        self.assertIs(json.loads(self.sent[-1][1])["swipe_read"], False)

    async def test_the_0_6_0_user_valves_state_is_read_and_carried_over_by_the_next_write(self):
        """The shape 0.6.0 kept in the user valves (strings, a bool): read as the state until the first write
        under the new key, which carries every field over, the devices and mutes included."""
        subs = json.dumps([{"endpoint": "https://push.example/old", "keys": {"p256dh": "k", "auth": "a"}}])
        self.legacy.update({"subscriptions": subs, "mutes": json.dumps({"c9": 0}),
                            "folder_mutes": json.dumps({"work": 0}), "gone": json.dumps(["abc"]),
                            "push_problem": web_push.NO_SUBJECT, "swipe_read": False})
        self.store.clear()
        await self.fn.event(finished(), __id__="web_push")
        (sent_subs, body, _key, _subject), = self.sent
        self.assertEqual(sent_subs[0]["endpoint"], "https://push.example/old")
        self.assertIs(json.loads(body)["swipe_read"], False, "the old swipe_read off still holds")
        # push_problem was set and can now be sent: the function writes the state, once, with the rest.
        self.assertEqual(self.store["push_problem"], "")
        self.assertEqual({k: self.store[k] for k in ("subscriptions", "mutes", "folder_mutes", "gone", "swipe_read")},
                         {k: self.legacy[k] for k in ("subscriptions", "mutes", "folder_mutes", "gone", "swipe_read")})
        self.assertEqual(self.settings["theme"], "dark", "the user's other settings are not touched")

    async def test_a_mute_in_the_old_user_valves_still_holds(self):
        self.store.clear()
        self.legacy.update({"subscriptions": json.dumps([{"endpoint": "https://push.example/1",
                                                          "keys": {"p256dh": "k", "auth": "a"}}]),
                            "mutes": json.dumps({"c1": 0})})
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_the_new_state_wins_over_the_old_user_valves_field_by_field(self):
        """Unmuted since the update (the state's mutes are empty) beats the old valves' mute; a field the
        state does not have yet (here swipe_read) still comes from the old valves."""
        self.legacy.update({"mutes": json.dumps({"c1": 0}), "swipe_read": False})
        self.store["mutes"] = "{}"
        await self.fn.event(finished(), __id__="web_push")
        (_subs, body, _key, _subject), = self.sent
        self.assertIs(json.loads(body)["swipe_read"], False)

    def webui_url(self, url):
        """Open WebUI's saved WEBUI_URL as the function reads it."""
        class Config:
            @staticmethod
            async def get(key):
                return url if key == "webui.url" else None
        mods = {"open_webui.models.config": types.ModuleType("open_webui.models.config")}
        mods["open_webui.models.config"].Config = Config
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)

    async def test_no_subject_and_a_plain_http_webui_url_sends_nothing_and_says_why(self):
        """An http WEBUI_URL is no subject. Nothing is sent (it would fail at the push service),
        the user's push_problem says why (the page shows it) and the log says so once, as an error."""
        self.fn.valves.VAPID_SUBJECT = ""
        self.webui_url("http://localhost:3000")
        with self.assertLogs("openclaw_web_push", "ERROR") as logs:
            await self.fn.event(finished(), __id__="web_push")
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store["push_problem"], web_push.NO_SUBJECT)
        self.assertIn("VAPID_SUBJECT", web_push.NO_SUBJECT)
        errors = [r for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual(len(errors), 1, [r.getMessage() for r in errors])
        self.assertIn("http://localhost:3000", errors[0].getMessage())

    async def test_an_https_webui_url_on_localhost_is_no_subject(self):
        self.fn.valves.VAPID_SUBJECT = ""
        self.webui_url("https://localhost:8443")
        with self.assertLogs("openclaw_web_push", "ERROR"):
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store["push_problem"], web_push.NO_SUBJECT)

    async def test_an_https_webui_url_is_the_subject_and_clears_the_problem(self):
        self.fn.valves.VAPID_SUBJECT = ""
        self.webui_url("https://chat.example/")
        self.store["push_problem"] = web_push.NO_SUBJECT
        await self.fn.event(finished(), __id__="web_push")
        (_subs, _body, _key, subject), = self.sent
        self.assertEqual(subject, "https://chat.example")
        self.assertEqual(self.store["push_problem"], "")

    async def test_no_send_without_the_key_file(self):
        self.fn.valves.VAPID_PRIVATE_KEY_FILE = "/nonexistent/vapid.pem"
        with self.assertLogs("openclaw_web_push", "ERROR"):
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_no_send_without_subscriptions(self):
        self.store["subscriptions"] = "[]"
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_a_muted_chat_sends_nothing(self):
        self.store["mutes"] = json.dumps({"c1": 0})
        with self.assertLogs("openclaw_web_push", "INFO") as logs:
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent, [])
        self.assertIn("muted, nothing sent chat_id=c1", "\n".join(logs.output))

    async def test_a_chat_in_a_muted_folder_sends_nothing(self):
        for muted in ("qa-old", "qa"):   # its own folder, or the folder that holds it
            self.store["folder_mutes"] = json.dumps({muted: 0})
            with self.assertLogs("openclaw_web_push", "INFO") as logs:
                await self.fn.event(finished(), __id__="web_push")
            self.assertEqual(self.sent, [], muted)
            self.assertIn(f"folder {muted[:8]} is muted, nothing sent chat_id=c1", "\n".join(logs.output))

    async def test_another_folder_muted_still_sends(self):
        self.store["folder_mutes"] = json.dumps({"work": 0})
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(len(self.sent), 1)

    def sent_payload(self):
        self.assertEqual(len(self.sent), 1)
        return json.loads(self.sent[0][1])

    async def test_a_side_chat_opens_its_parent_with_the_side_chat_in_the_panel(self):
        """Open WebUI 0.13 side chat (/btw): its own chat row, shown in the parent's side panel. The tap opens the
        parent, whose saved panel brings the side chat back; mute, read and reply still act on the side chat."""
        self.chat_json["originalChatId"] = "p1"
        self.views[("u1", "p1")] = {"panels": {"tabs": [{"id": "t1", "kind": "side", "chatId": "c1"}]}}
        await self.fn.event(finished(), __id__="web_push")
        payload = self.sent_payload()
        # ?oc-side names the side chat: on a phone the page opens the panel sheet on its tab.
        self.assertEqual((payload["url"], payload["chat_id"]), ("/c/p1?oc-side=c1", "c1"))

    async def test_a_fork_or_clone_opens_itself(self):
        """A fork or a clone also names its originalChatId, but is a chat of its own: no side tab for it."""
        self.chat_json["originalChatId"] = "p1"
        self.views[("u1", "p1")] = {"panels": {"tabs": [{"id": "t1", "kind": "side", "chatId": "c9"}]}}
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent_payload()["url"], "/c/c1")

    async def test_a_side_chat_whose_parent_has_no_view_opens_itself(self):
        self.chat_json["originalChatId"] = "p1"
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(self.sent_payload()["url"], "/c/c1")

    async def test_no_folder_mutes_reads_no_folders(self):
        """Without a folder mute in effect, the folders are not read."""
        for raw in (None, "{}", json.dumps({"qa": 500})):
            if raw is None:
                self.store.pop("folder_mutes", None)
            else:
                self.store["folder_mutes"] = raw
            await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.folder_reads, [])

    async def test_another_chat_muted_still_sends(self):
        self.store["mutes"] = json.dumps({"c9": 0})
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(len(self.sent), 1)

    async def test_the_preview_valve_puts_the_reply_start_in_the_body(self):
        self.fn.valves.PREVIEW_CHARS = 600
        await self.fn.event(finished(), __id__="web_push")
        (subs, body, key, subject), = self.sent
        self.assertEqual(json.loads(body)["body"], REPLY)

    async def test_a_channel_message_is_sent_to_the_recipient(self):
        await self.fn.event(channel_message(), __id__="web_push")
        p = self.sent_payload()
        self.assertEqual((p["kind"], p["channel_id"], p["title"], p["url"]),
                         ("channel.message", "ch1", "#Japan", "/channels/ch1"))
        self.assertNotIn("hello there", self.sent[0][1])
        self.assertNotIn("chat_id", p)

    async def test_the_sender_gets_no_push_for_their_own_message(self):
        await self.fn.event(channel_message(user_id="u2", sender_id="u2"), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_a_dm_is_titled_with_the_senders_name(self):
        await self.fn.event(channel_message(title=""), __id__="web_push")
        self.assertEqual(self.sent_payload()["title"], "name-u2")

    async def test_a_group_message_does_not_look_up_the_sender(self):
        """Only a DM needs the sender's name: a group has the channel's."""
        users = sys.modules["open_webui.models.users"].Users
        calls = []
        real = users.get_user_by_id

        async def spy(user_id):
            calls.append(user_id)
            return await real(user_id)
        with mock.patch.object(users, "get_user_by_id", staticmethod(spy)):
            await self.fn.event(channel_message(), __id__="web_push")
        self.assertNotIn("u2", calls)

    async def test_the_kill_switch_sends_nothing_and_chat_replies_still_go(self):
        self.fn.valves.CHANNEL_PUSH = False
        await self.fn.event(channel_message(), __id__="web_push")
        self.assertEqual(self.sent, [])
        await self.fn.event(finished(), __id__="web_push")
        self.assertEqual(len(self.sent), 1)

    async def test_the_channel_preview_valve_is_its_own(self):
        """PREVIEW_CHARS (a reply) does not turn on a channel message's preview; CHANNEL_PREVIEW_CHARS does."""
        self.fn.valves.PREVIEW_CHARS = 600
        await self.fn.event(channel_message(), __id__="web_push")
        self.assertNotIn("body", self.sent_payload())
        self.sent.clear()
        self.fn.valves.CHANNEL_PREVIEW_CHARS = 600
        await self.fn.event(channel_message(), __id__="web_push")
        self.assertEqual(self.sent_payload()["body"], "hello there")

    async def test_the_defaults_are_push_on_and_no_preview(self):
        v = web_push.Event.Valves()
        self.assertEqual((v.CHANNEL_PUSH, v.CHANNEL_PREVIEW_CHARS), (True, 0))

    async def test_a_channel_message_needs_a_subscription(self):
        self.store["subscriptions"] = "[]"
        await self.fn.event(channel_message(), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_a_chat_mute_does_not_hold_a_channel_and_nothing_reads_the_chat_tables(self):
        """Per-channel mute is Open WebUI's (it does not publish for a muted member, a mention excepted);
        a chat mute or a folder mute that happens to share the id changes nothing here."""
        self.store["mutes"] = json.dumps({"ch1": 0})
        self.store["folder_mutes"] = json.dumps({"qa": 0})
        await self.fn.event(channel_message(), __id__="web_push")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.folder_reads, [])

    async def test_the_send_is_logged_by_channel(self):
        calls = []
        self.fn.send_all = lambda subs, body, topic, key, subject, chat_id, early=None: calls.append((topic, chat_id)) or set()
        await self.fn.event(channel_message(), __id__="web_push")
        self.assertEqual(calls, [(web_push.channel_topic("ch1"), "channel:ch1")])

    async def test_a_recipient_who_is_not_a_member_gets_nothing(self):
        """Defence in depth: Open WebUI publishes per member, the function checks it once more."""
        self.members.clear()
        with self.assertLogs("openclaw_web_push", "INFO") as logs:
            await self.fn.event(channel_message(), __id__="web_push")
        self.assertEqual(self.sent, [])
        self.assertIn("not a member, nothing sent chat_id=channel:ch1", "\n".join(logs.output))

    async def test_a_membership_that_cannot_be_read_sends_nothing(self):
        """Fail closed: a message to a channel the user may not be in is worse than a missing push."""
        channels = sys.modules["open_webui.models.channels"].Channels

        async def broken(*a, **k):
            raise RuntimeError("db down")
        with mock.patch.object(channels, "get_member_by_channel_and_user_id", staticmethod(broken)):
            with self.assertLogs("openclaw_web_push", "WARNING") as logs:
                await self.fn.event(channel_message(), __id__="web_push")
        self.assertEqual(self.sent, [])
        self.assertIn("membership", "\n".join(logs.output))

    async def test_a_message_without_a_recipient_or_channel_sends_nothing(self):
        await self.fn.event(channel_message(user_id=""), __id__="web_push")
        await self.fn.event(channel_message(channel_id=""), __id__="web_push")
        self.assertEqual(self.sent, [])

    async def test_the_middlewares_event_for_an_openclaw_model_sends_nothing(self):
        await self.fn.event(finished(source="api"), __id__="web_push")
        self.assertEqual(self.sent, [])


class RoundTripTests(unittest.TestCase):
    """Real pywebpush encryption and VAPID, decrypted by a local push service."""

    def run_scenario(self, scenario):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "web_push_roundtrip.py"), scenario],
                             capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_each_device_gets_the_title_and_link_encrypted(self):
        res = self.run_scenario("ok")
        self.assertEqual([r["device"] for r in res["received"]], ["dev1", "dev2"])
        for r in res["received"]:
            self.assertEqual(r["payload"], {"v": 2, "kind": "chat.finished", "source": "openclaw-pipe", "chat_id": "c1", "message_id": "m1",
                                            "title": "Trip plan", "url": "/c/c1", "fn": "web_push", "actions": ["reply", "mute"]})
            self.assertNotIn("SECRET-REPLY-TEXT", json.dumps(r["payload"]))
            self.assertEqual(r["headers"]["Content-Encoding"], "aes128gcm")
            self.assertEqual((r["headers"]["TTL"], r["headers"]["Urgency"]), ("3600", "normal"))
            self.assertEqual(r["headers"]["Topic"], web_push.topic_for("c1"))
            self.assertEqual(r["vapid"], "vapid")
        self.assertEqual(res["left"], ["dev1", "dev2"])

    def test_a_gone_subscription_is_dropped_and_a_failing_one_kept(self):
        res = self.run_scenario("gone")
        self.assertEqual(len(res["received"]), 2)
        self.assertEqual(res["left"], ["dev2"], "410 dropped, 500 kept")
        self.assertEqual(len(res["gone"]), 1, "and remembered, so the page replaces it")

    def test_a_new_token_refused_once_gets_the_notification_exactly_once(self):
        """FCM's settle window: 410 for the just-saved token, then 201. The function keeps it and sends
        that notification once more (NEW_TOKEN_SETTLE_S, 0 here): delivered once, nothing sent a third time."""
        res = self.run_scenario("settle")
        self.assertEqual([a["status"] for a in res["answers"] if a["device"] == "dev1"], [410, 201])
        self.assertEqual([a["status"] for a in res["answers"] if a["device"] == "dev2"], [201], "the older one: once")
        self.assertEqual(res["left"], ["dev1", "dev2"])
        self.assertEqual(res["gone"], [])

    def test_a_410_for_a_subscription_saved_moments_ago_keeps_it(self):
        """FCM answers 410 for a token Chrome registered seconds ago (measured: 410 one to two seconds
        after the save, then pushes go through), so a new subscription is not dropped for it."""
        res = self.run_scenario("gone-new")
        self.assertEqual(res["left"], ["dev1", "dev2"])
        self.assertEqual(res["gone"], [])


if __name__ == "__main__":
    unittest.main()


class GoneTests(unittest.IsolatedAsyncioTestCase):
    """A push service can retire a token the browser still holds (Chrome lists it, no
    pushsubscriptionchange): the page would save that dead one again on every load. So the function
    remembers each gone endpoint by fingerprint (`gone`), and the page replaces such a subscription."""

    def functions(self, valves, legacy=None):
        """`valves`: the function's state (the user's settings, under its key); `legacy`: what 0.6.0 kept in the
        user valves. Open WebUI's Users is stubbed as well: _drop writes the state there."""
        store = {"v": valves}

        class Functions:
            @staticmethod
            async def get_user_valves_by_id_and_user_id(fid, user_id):
                return dict(legacy) if legacy else None

        class Users:
            @staticmethod
            async def get_user_by_id(user_id):
                return types.SimpleNamespace(settings={"web_push:web_push": store["v"]})

            @staticmethod
            async def update_user_settings_by_id(user_id, updated):
                store["v"] = updated["web_push:web_push"]
        mods = {"open_webui": types.ModuleType("open_webui"), "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.users": types.ModuleType("open_webui.models.users")}
        mods["open_webui.models.users"].Users = Users
        p = mock.patch.dict(sys.modules, mods)
        p.start()
        self.addCleanup(p.stop)
        return Functions, store

    async def test_a_drop_carries_the_old_user_valves_into_the_state(self):
        keys = {"p256dh": "k", "auth": "a"}
        subs = [{"endpoint": "https://push.example/dead", "keys": keys, "device": "d1"},
                {"endpoint": "https://push.example/ok", "keys": keys, "device": "d2"}]
        Functions, store = self.functions({}, legacy={"subscriptions": json.dumps(subs), "mutes": json.dumps({"c9": 0})})
        await web_push.Event()._drop(Functions, "web_push", "u1", {"https://push.example/dead"})
        self.assertEqual([s["device"] for s in json.loads(store["v"]["subscriptions"])], ["d2"])
        self.assertEqual(store["v"]["mutes"], json.dumps({"c9": 0}), "the mute carried over from the old valves")

    async def test_a_gone_endpoint_is_dropped_and_its_fingerprint_kept(self):
        keys = {"p256dh": "k", "auth": "a"}
        subs = [{"endpoint": "https://push.example/dead", "keys": keys, "device": "d1"},
                {"endpoint": "https://push.example/ok", "keys": keys, "device": "d2"}]
        Functions, store = self.functions({"subscriptions": json.dumps(subs), "mutes": "{}"})
        await web_push.Event()._drop(Functions, "web_push", "u1", {"https://push.example/dead"})
        self.assertEqual([s["device"] for s in json.loads(store["v"]["subscriptions"])], ["d2"])
        self.assertEqual(json.loads(store["v"]["gone"]), [web_push.fingerprint("https://push.example/dead")])
        self.assertEqual(store["v"]["mutes"], "{}", "other valves kept")
        # The same endpoint again (the page saved the dead one before it knew): one entry.
        store["v"]["subscriptions"] = json.dumps(subs)
        await web_push.Event()._drop(Functions, "web_push", "u1", {"https://push.example/dead"})
        self.assertEqual(len(json.loads(store["v"]["gone"])), 1)

    async def test_the_list_keeps_the_most_recent(self):
        Functions, store = self.functions({"subscriptions": "[]"})
        for i in range(web_push.GONE_KEEP + 3):
            await web_push.Event()._drop(Functions, "web_push", "u1", {f"https://push.example/{i}"})
        gone = json.loads(store["v"]["gone"])
        self.assertEqual(len(gone), web_push.GONE_KEEP)
        self.assertEqual(gone[-1], web_push.fingerprint(f"https://push.example/{web_push.GONE_KEEP + 2}"))

    def test_the_fingerprint_is_the_pages(self):
        """web/oc-push.js computes the same: SHA-256 hex of the endpoint, its first FINGERPRINT_CHARS."""
        self.assertEqual(web_push.fingerprint("https://push.example/x"),
                         hashlib.sha256(b"https://push.example/x").hexdigest()[:web_push.FINGERPRINT_CHARS])


class NotificationImageTests(unittest.TestCase):
    """The picture and the badge: Open WebUI's own unless the admin valves name others (the service
    worker reads Open WebUI's icon itself), never a picture of this add-on's own."""

    def test_none_by_default(self):
        p = web_push.build_payload(finished())
        self.assertNotIn("icon", p)
        self.assertNotIn("badge", p)

    def test_a_path_on_the_site_or_an_https_url_is_carried(self):
        p = web_push.build_payload(finished(), icon=" /static/team.png ", badge="https://cdn.example/b.png")
        self.assertEqual((p["icon"], p["badge"]), ("/static/team.png", "https://cdn.example/b.png"))

    def test_anything_else_is_dropped(self):
        for bad in ("data:image/png;base64,AAAA", "javascript:alert(1)", "//elsewhere.example/i.png",
                    "http://plain.example/i.png", "static/i.png", "/" + "x" * web_push.MAX_IMAGE_URL_CHARS):
            self.assertNotIn("icon", web_push.build_payload(finished(), icon=bad), bad)

    def test_the_valves_reach_the_payload(self):
        v = web_push.Event.Valves(NOTIFICATION_ICON="/static/team.png")
        self.assertEqual((v.NOTIFICATION_ICON, v.NOTIFICATION_BADGE), ("/static/team.png", ""))

    def test_the_service_worker_carries_no_picture_of_its_own(self):
        src = open(os.path.join(ROOT, "web", "oc-sw.js")).read()
        self.assertNotIn("data:image/", src)
        self.assertIn("icon: p.icon || await ownIcon(),", src)


class SubjectValveTests(unittest.TestCase):
    """VAPID_SUBJECT: push services take only a mailto: or https: subject (py_vapid drops any other and every
    send fails). Anything else is refused when the admin saves the valves; Open WebUI shows the error."""

    def test_a_mailto_or_https_subject_or_none(self):
        for ok in ("mailto:admin@example.com", "https://chat.example", "", "  mailto:a@b.example  "):
            self.assertEqual(web_push.Event.Valves(VAPID_SUBJECT=ok).VAPID_SUBJECT, ok.strip())

    def test_anything_else_is_refused_with_why(self):
        for bad in ("admin@example.com", "http://localhost:3000", "chat.example.com",
                    # Apple: 403 BadJwtToken for a subject on no public host
                    "mailto:webmaster@localhost", "mailto:a@box.local", "https://localhost:3000",
                    "https://app.localhost", "https://chat"):
            with self.assertRaises(Exception) as ctx:
                web_push.Event.Valves(VAPID_SUBJECT=bad)
            self.assertIn("mailto:", str(ctx.exception))
            self.assertIn("VAPID_SUBJECT", str(ctx.exception))


class LogIdsTests(unittest.TestCase):
    """Every log line names the full chat id as `chat_id=<id>` (ELI-368): central logs index it, so
    `logtrace <chat id>` shows the push next to the Pipe's lines of the same reply."""

    CHAT = "12345678-1234-4123-8123-123456789abc"

    def send(self, outcome):
        class WebPushException(Exception):
            def __init__(self, status):
                super().__init__(f"HTTP {status}")
                self.response = types.SimpleNamespace(status_code=status)

        def webpush(**kw):
            if outcome != "ok":
                raise WebPushException(outcome)

        mods = {"py_vapid": types.SimpleNamespace(Vapid=types.SimpleNamespace(from_pem=lambda key: object())),
                "pywebpush": types.SimpleNamespace(WebPushException=WebPushException, webpush=webpush)}
        fn = web_push.Event()
        sub = {"endpoint": "https://push.example/1", "keys": {"p256dh": "k", "auth": "a"}}
        with mock.patch.dict(sys.modules, mods), self.assertLogs("openclaw_web_push", "INFO") as logs:
            fn.send_all([sub], "{}", "t", b"PEM", "mailto:a@example.com", self.CHAT)
        return "\n".join(logs.output)

    def test_sent_gone_and_failed_lines_name_the_chat(self):
        for outcome in ("ok", 410, 500):
            self.assertIn(f"chat_id={self.CHAT}", self.send(outcome), outcome)
