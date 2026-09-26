import argparse
import json
import logging
import os
import secrets
import threading
import time
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .settings import BRANDS, COLORS, CONDITIONS, MATERIALS
from .matching import match_item
from .source import search_url
from .store import Store
from .telegram import Telegram, TelegramError
from .worker import Worker


def load_env():
    path = Path(".env")
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def handler_for(store, worker):
    public = os.getenv("VITOOL_PUBLIC") == "1"
    dashboard_username = os.getenv("VITOOL_DASHBOARD_USERNAME", "watchdog")
    dashboard_password = os.getenv("VITOOL_DASHBOARD_PASSWORD", "")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def response(self, code, data, content_type="application/json"):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' https://*.vinted.net; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def trusted_host(self):
            return public or self.headers.get("Host", "") in {
                f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"
            }

        def authorized(self):
            if not dashboard_password:
                return not public
            try:
                scheme, encoded = self.headers.get("Authorization", "").split(" ", 1)
                username, password = b64decode(encoded).decode().split(":", 1)
            except (ValueError, UnicodeDecodeError):
                return False
            return scheme.lower() == "basic" and secrets.compare_digest(username, dashboard_username) \
                and secrets.compare_digest(password, dashboard_password)

        def require_authorization(self):
            body = json.dumps({"error": "Authentication required"}).encode()
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Vitool", charset="UTF-8"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self.response(200, {"ok": True})
            if not self.trusted_host():
                return self.response(403, {"error": "Use localhost or an SSH tunnel"})
            if not self.authorized():
                return self.require_authorization()
            if self.path == "/":
                return self.response(200, (Path(__file__).parent / "static/index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                settings = store.settings()
                return self.response(200, {"settings": settings, "status": store.get("status", {}),
                    "next_scan": store.get("next_scan", 0), "blocked": store.get("blocked", False),
                    "schedule": {"mode": "github", "minimum_seconds": 900}
                                if os.getenv("VITOOL_WEB_ONLY") == "1"
                                else {"mode": "continuous", "minimum_seconds": 10},
                    "telegram_ready": worker.telegram.ready,
                    "matches": [i for i in store.matches() if match_item(i, settings)[0]],
                    "options": {"brands": sorted(BRANDS, key=str.casefold), "colors": list(COLORS),
                                "conditions": sorted(CONDITIONS), "materials": sorted(MATERIALS),
                                "departments": ["men", "women"],
                                "necklines": ["half_zip", "v_neck", "crew_neck"]},
                    "searches": [{"brand": b, "department": d, "url": search_url(settings, b, d)}
                                 for d in settings["departments"] for b in settings["brands"]]})
            return self.response(404, {"error": "Not found"})

        def do_POST(self):
            if not self.trusted_host() or self.headers.get("X-Vitool") != "local":
                return self.response(403, {"error": "Request rejected"})
            if not self.authorized():
                return self.require_authorization()
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin not in {"http://" + host, "https://" + host}:
                return self.response(403, {"error": "Invalid origin"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 16384:
                    raise ValueError("Request too large")
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                if self.path == "/api/settings":
                    settings = store.save_settings(data)
                    store.set("next_scan", min(store.get("next_scan", float("inf")),
                                               time.time() + settings["interval_seconds"]))
                elif self.path == "/api/toggle":
                    settings = store.settings()
                    settings["paused"] = not settings["paused"]
                    store.save_settings(settings)
                    if not settings["paused"]:
                        store.set("blocked", False)
                elif self.path == "/api/scan":
                    if time.time() < store.get("next_scan", 0) or worker.lock.locked() or store.get("blocked", False):
                        return self.response(409, {"error": "Scan is running, paused by a block, or in cooldown. Wait until the next check."})
                    threading.Thread(target=worker.scan, kwargs={"force": True}, daemon=True).start()
                elif self.path == "/api/telegram-test":
                    worker.telegram.send("Vitool is connected. Use /settings to see your watchlist and /help to edit it.")
                else:
                    return self.response(404, {"error": "Not found"})
                return self.response(200, {"ok": True})
            except (ValueError, TypeError, TelegramError) as exc:
                return self.response(400, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Vitool personal watchlist")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    parser.add_argument("--once", action="store_true", help="One scan; still respects cooldown and block state")
    parser.add_argument("--telegram-chats", action="store_true", help="List chat IDs that have messaged your bot")
    args = parser.parse_args()
    load_env()
    if os.getenv("VITOOL_PUBLIC") == "1" and not os.getenv("VITOOL_DASHBOARD_PASSWORD"):
        parser.exit(1, "VITOOL_DASHBOARD_PASSWORD is required in public mode.\n")
    logging.basicConfig(level=logging.WARNING)
    # Prevent multiple workers sharing a data directory (also protects Telegram update offsets).
    store = Store(os.getenv("VITOOL_DATA_DIR", "data"))
    import fcntl
    lock_file = (store.directory / "worker.lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "Vitool is already running for this data directory. Stop it first.\n")
    if args.telegram_chats:
        try:
            chats = {str(u["message"]["chat"]["id"]) for u in Telegram().updates(0) if "message" in u}
            print("Chat IDs: " + (", ".join(sorted(chats)) or "none — send /start to your bot first"))
        except TelegramError as exc:
            parser.exit(1, str(exc) + "\n")
        return
    worker = Worker(store)
    if args.once:
        worker.process_commands()
        worker.scan(force=True)
        print(json.dumps(store.get("status", {}), indent=2))
        return
    worker.status(scanning=False)
    server = ThreadingHTTPServer(("0.0.0.0" if os.getenv("VITOOL_CONTAINER") == "1" else "127.0.0.1", args.port), handler_for(store, worker))
    if os.getenv("VITOOL_WEB_ONLY") != "1":
        for target in (worker.run, worker.commands):
            threading.Thread(target=target, daemon=True).start()
    print(f"Vitool → http://localhost:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
