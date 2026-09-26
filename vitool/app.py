import argparse
import json
import logging
import os
import threading
import time
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
            return self.headers.get("Host", "") in {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}

        def do_GET(self):
            if not self.trusted_host():
                return self.response(403, {"error": "Use localhost or an SSH tunnel"})
            if self.path == "/":
                return self.response(200, (Path(__file__).parent / "static/index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                settings = store.settings()
                return self.response(200, {"settings": settings, "status": store.get("status", {}),
                    "next_scan": store.get("next_scan", 0), "blocked": store.get("blocked", False),
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
                return self.response(403, {"error": "Local requests only"})
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
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
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--once", action="store_true", help="One scan; still respects cooldown and block state")
    parser.add_argument("--telegram-chats", action="store_true", help="List chat IDs that have messaged your bot")
    args = parser.parse_args()
    load_env()
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
        worker.scan(force=True)
        print(json.dumps(store.get("status", {}), indent=2))
        return
    worker.status(scanning=False)
    server = ThreadingHTTPServer(("0.0.0.0" if os.getenv("VITOOL_CONTAINER") == "1" else "127.0.0.1", args.port), handler_for(store, worker))
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
