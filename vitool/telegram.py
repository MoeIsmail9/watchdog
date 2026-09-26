import os
import time

import httpx


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    @property
    def ready(self):
        return bool(self.token and self.chat_id)

    def call(self, method, payload):
        if not self.token:
            raise TelegramError("Telegram bot token is not configured")
        try:
            result = httpx.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=15)
            data = result.json()
        except (httpx.HTTPError, ValueError):
            # Never leak request URLs: Telegram tokens are part of the path.
            raise TelegramError("Telegram connection failed; check token and connectivity") from None
        if not data.get("ok"):
            raise TelegramError(f"Telegram rejected the request (code {data.get('error_code', 'unknown')})")
        return data["result"]

    def send(self, text):
        if not self.ready:
            raise TelegramError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return self.call("sendMessage", {"chat_id": self.chat_id, "text": text[:4000]})

    def alert(self, item):
        review = item.get("review")
        if review:
            price = f"€{item['price']:.2f}"
            if review["new_price"] > item["price"]:
                price += f" · new ~€{review['new_price']} (−{round(100 - 100 * item['price'] / review['new_price'])}%)"
            lines = [f"✅ {review['score']}/10 — {item['brand']} · {item['title']}",
                     f"Size {item['size']} · {item['condition']}", price,
                     f"✔ {review['summary']}", *(f"⚠ {w}" for w in review["warnings"])]
            if not review.get("photo"):
                lines.append("⚠ Photo not checked")
            text = "\n".join(lines) + f"\nShipping and buyer fees extra.\n{item['url']}"
        else:
            text = (f"{item['brand']} · €{item['price']:.2f}\n{item['title']}\n"
                    f"Size {item['size']} · {item['condition']}\n{item['reason']}\n"
                    f"Shipping and buyer fees extra.\n{item['url']}")
        # A link preview provides the product image when Telegram can fetch it.
        # One API call avoids duplicate alerts after an ambiguous photo timeout.
        self.send(text)

    def updates(self, offset):
        return self.call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})


HELP = """Vitool settings
/status — last scan and next check
/settings — current preferences
/price 20
/size M (or S,M)
/colors brown,black
/brands Ralph Lauren,Gant
/materials cotton,wool,cashmere (or /materials any)
/necklines half_zip,v_neck (or any)
Change condition and departments through the local settings page.
/interval 60 — seconds; minimum 10 seconds
/pause /resume
All price limits exclude shipping and buyer fees."""


def handle_command(text, store):
    command, _, arg = text.partition(" ")
    command = command.split("@")[0].lower()
    arg = arg.strip()
    settings = store.settings()
    if command in ("/start", "/help"):
        return HELP
    if command == "/status":
        return str(store.get("status", {"message": "No scan yet"}))
    if command == "/settings":
        return "\n".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in settings.items())
    if command in ("/pause", "/resume"):
        settings["paused"] = command == "/pause"
        if command == "/resume":
            store.set("blocked", False)
    elif command == "/price":
        settings["max_price"] = float(arg)
    elif command == "/interval":
        settings["interval_seconds"] = int(arg)
    elif command in ("/size", "/colors", "/brands", "/materials"):
        key = {"/size": "sizes", "/colors": "colors", "/brands": "brands", "/materials": "materials"}[command]
        settings[key] = [x.strip() for x in arg.split(",")]
        if command == "/materials" and settings[key] == ["any"]:
            settings[key] = []
    elif command in ("/style", "/necklines"):
        settings["necklines"] = [] if arg == "any" else [x.strip() for x in arg.split(",")]
    else:
        return HELP
    store.save_settings(settings)
    store.set("next_scan", min(store.get("next_scan", float("inf")), time.time() + settings["interval_seconds"]))
    return "Saved. Changes apply on the next scheduled scan. Use /settings to view them."
