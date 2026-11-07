"""The Web Push function (openclaw_web_push.py): title and link only, OpenClaw
events only from the Pipe, and a real encrypted send (tests/web_push_roundtrip.py)."""
import importlib.util
import hashlib
import asyncio
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
PIPE = ["openclaw_gateway."]


def finished(source="openclaw-pipe", model="openclaw_gateway.main", **data):
    return {"event": "chat.finished", "source": source, "actor": {"id": "u1"},
            "data": {"user_id": "u1", "chat_id": "c1", "message_id": "m1", "model_id": model,
                     "title": "Trip plan", "url": "https://chat.example.com/c/c1", "message": REPLY, **data}}


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

    def test_no_prefixes_sends_every_chat_finished(self):
        self.assertTrue(web_push.should_send(finished(source="api"), []))

    def test_malformed_subscriptions_are_ignored(self):
        raw = json.dumps([{"endpoint": "https://p/1", "keys": {"p256dh": "k", "auth": "a"}},
                          {"endpoint": "https://p/2"}, "junk", {"keys": {"p256dh": "k"}}])
        self.assertEqual([s["endpoint"] for s in web_push.parse_subscriptions(raw)], ["https://p/1"])
        self.assertEqual(web_push.parse_subscriptions("not json"), [])


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
        """Mute writes this function's user valves: without its id, no Mute button."""
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

        class Functions:
            @staticmethod
            async def get_user_valves_by_id_and_user_id(fid, user_id):
                return dict(store)

            @staticmethod
            async def update_user_valves_by_id_and_user_id(fid, user_id, valves):
                store.update(valves)
        # c1 is in folder "qa-old", inside "qa".
        self.folder_reads = []
        reads = self.folder_reads

        class Chats:
            @staticmethod
            async def get_chat_by_id(chat_id):
                reads.append(chat_id)
                return types.SimpleNamespace(id=chat_id, folder_id="qa-old" if chat_id == "c1" else None)

        class Folders:
            @staticmethod
            async def get_folders_by_user_id(user_id):
                return [types.SimpleNamespace(id="qa", parent_id=None), types.SimpleNamespace(id="qa-old", parent_id="qa")]
        mods = {"open_webui": types.ModuleType("open_webui"), "open_webui.models": types.ModuleType("open_webui.models"),
                "open_webui.models.functions": types.ModuleType("open_webui.models.functions"),
                "open_webui.models.chats": types.ModuleType("open_webui.models.chats"),
                "open_webui.models.folders": types.ModuleType("open_webui.models.folders")}
        mods["open_webui.models.functions"].Functions = Functions
        mods["open_webui.models.chats"].Chats = Chats
        mods["open_webui.models.folders"].Folders = Folders
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
        self.assertTrue(web_push.Event.UserValves().swipe_read)

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

    async def test_no_folder_mutes_reads_no_chat(self):
        """Without a folder mute in effect, the send costs no extra read."""
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

    def functions(self, valves):
        store = {"v": valves}

        class Functions:
            @staticmethod
            async def get_user_valves_by_id_and_user_id(fid, user_id):
                return store["v"]

            @staticmethod
            async def update_user_valves_by_id_and_user_id(fid, user_id, v):
                store["v"] = v
        return Functions, store

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
