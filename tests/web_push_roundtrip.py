"""The Web Push function against a local push service, with real encryption, in its
own interpreter (other suites stub `cryptography`). Prints one JSON result.

    python tests/web_push_roundtrip.py <scenario>

Scenarios: ok (two devices, both delivered), gone (one answers 410, one 500)."""
import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLY = "SECRET-REPLY-TEXT the whole answer the agent wrote"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def device():
    """A browser's side of a subscription: its ECDH key and auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, os.urandom(16), pub


def main(scenario: str) -> None:
    devices = {name: device() for name in ("dev1", "dev2")}
    status = {"dev1": 201, "dev2": 201} if scenario == "ok" else {"dev1": 410, "dev2": 500}
    # "settle": dev1 is a token the push service does not take yet: 410 once, then 201 (FCM's settle window)
    if scenario == "settle":
        status = {"dev1": [410, 201], "dev2": 201}
    received = []
    answers = []

    class PushService(BaseHTTPRequestHandler):
        def do_POST(self):
            name = self.path.rsplit("/", 1)[-1]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            key, auth, _ = devices[name]
            plain = http_ece.decrypt(body, private_key=key, auth_secret=auth, version="aes128gcm")
            received.append({"device": name, "payload": json.loads(plain), "bytes": len(body),
                             "headers": {k: self.headers.get(k) for k in
                                         ("TTL", "Urgency", "Topic", "Content-Encoding")},
                             "vapid": (self.headers.get("Authorization") or "").split(" ")[0]})
            code = status[name].pop(0) if isinstance(status[name], list) else status[name]
            answers.append({"device": name, "status": code})
            self.send_response(code)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), PushService)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/push"

    store = {"subscriptions": json.dumps([
        {"endpoint": f"{base}/{name}", "keys": {"p256dh": b64(pub), "auth": b64(auth)}, "device": name,
         # "gone-new": saved by the page a moment ago (its `at`, milliseconds), as a token FCM does not take yet
         **({"at": int(time.time() * 1000)} if scenario == "gone-new" or (scenario == "settle" and name == "dev1") else {})}
        for name, (_, auth, pub) in devices.items()])}

    class Functions:
        @staticmethod
        async def get_user_valves_by_id_and_user_id(fid, user_id):
            return dict(store) if (fid, user_id) == ("web_push", "u1") else None

        @staticmethod
        async def update_user_valves_by_id_and_user_id(fid, user_id, valves):
            store.clear()
            store.update(valves)
            return valves

    class Chats:
        @staticmethod
        async def get_chat_by_id(chat_id):
            return types.SimpleNamespace(id=chat_id, folder_id=None, chat={})

    for name, attrs in (("open_webui", {}), ("open_webui.models", {}),
                        ("open_webui.models.functions", {"Functions": Functions}),
                        ("open_webui.models.chats", {"Chats": Chats})):
        sys.modules[name] = types.ModuleType(name)
        sys.modules[name].__dict__.update(attrs)

    vapid = ec.generate_private_key(ec.SECP256R1())
    keyfile = os.path.join(tempfile.mkdtemp(), "vapid_private.pem")
    with open(keyfile, "wb") as f:
        f.write(vapid.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))

    spec = importlib.util.spec_from_file_location("openclaw_web_push", os.path.join(ROOT, "openclaw_web_push.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = mod.Event()
    fn.valves = fn.Valves(VAPID_PRIVATE_KEY_FILE=keyfile, VAPID_SUBJECT="mailto:test@example.com")
    event = {"event": "chat.finished", "source": "openclaw-pipe", "actor": {"id": "u1"},
             "data": {"user_id": "u1", "chat_id": "c1", "message_id": "m1", "model_id": "openclaw_gateway.main",
                      "title": "Trip plan", "url": "https://chat.example.com/c/c1", "message": REPLY}}
    mod.NEW_TOKEN_SETTLE_S = 0   # no real wait in the test

    async def run():
        await fn.event(event, __id__="web_push")
        await asyncio.gather(*list(fn._pending))   # the resend, if any
    asyncio.run(run())
    server.shutdown()
    print(json.dumps({"received": sorted(received, key=lambda r: r["device"]),
                      "left": [s["device"] for s in json.loads(store["subscriptions"])],
                      "gone": json.loads(store.get("gone") or "[]"), "answers": answers}))


if __name__ == "__main__":
    main(sys.argv[1])
