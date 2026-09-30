"""Optional Gemini review of matched listings before they are sent to Telegram.

One call per matched listing: the catalog photo plus listing text and the user's
filters go in, a short verdict, score, rough new-price estimate and summary come
out. Any failure returns control to the caller, which still sends the alert.
"""
import base64
import json
import os
import time

import httpx

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_IMAGE_BYTES = 4_000_000

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["send", "skip"]},
        "score": {"type": "INTEGER"},
        "new_price": {"type": "INTEGER"},
        "summary": {"type": "STRING"},
        "warnings": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["verdict", "score", "new_price", "summary", "warnings"],
}

PROMPT = """You review a second-hand clothing listing from Vinted Germany for a buyer.
The listing already passed simple text filters. Check it against the buyer's wishes
using the photo and the text, which may be German.

Buyer wants: {wants}

Listing:
{listing}

Answer in English:
- verdict: "skip" only when the listing clearly does not fit: wrong item type, clearly
  wrong colour in the photo, kids size, serious visible damage, or an obvious fake.
  Otherwise "send". When unsure, use "send" and add a warning.
- score: 1-10, how good this find is for the buyer (fit with wishes, condition, value).
- new_price: rough retail price in EUR of this item bought new. 0 if you cannot tell.
- summary: one short line (max 90 characters) of what it is and what you confirmed.
- warnings: up to 3 short concerns, e.g. label not shown, possible pilling. Empty if none."""


class ReviewError(Exception):
    pass


class Reviewer:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"

    @property
    def ready(self):
        return bool(self.api_key)

    def image_part(self, url):
        if not url:
            return None
        try:
            response = httpx.get(url, timeout=10)
        except httpx.HTTPError:
            return None
        mime = response.headers.get("Content-Type", "").split(";")[0]
        if response.status_code != 200 or not mime.startswith("image/") or len(response.content) > MAX_IMAGE_BYTES:
            return None
        return {"inline_data": {"mime_type": mime, "data": base64.b64encode(response.content).decode()}}

    def review(self, item, settings):
        wants = {key: settings[key] for key in (
            "brands", "sizes", "colors", "max_price", "conditions", "materials", "necklines", "departments", "categories")}
        listing = {key: item.get(key, "") for key in (
            "title", "brand", "size", "condition", "price", "color_text", "description", "department", "category")}
        parts = [{"text": PROMPT.format(wants=json.dumps(wants, ensure_ascii=False),
                                        listing=json.dumps(listing, ensure_ascii=False))}]
        image = self.image_part(item.get("image", ""))
        if image:
            parts.append(image)
        response = None
        for attempt in range(3):
            try:
                response = httpx.post(API_URL.format(model=self.model), timeout=15,
                    headers={"x-goog-api-key": self.api_key},
                    json={"contents": [{"parts": parts}],
                          "generationConfig": {"responseMimeType": "application/json",
                                               "responseSchema": SCHEMA, "temperature": 0.2}})
            except httpx.HTTPError:
                if attempt == 2:
                    raise ReviewError("Gemini connection failed after retries") from None
            else:
                if response.status_code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    break
            time.sleep(2 ** attempt)
        if response.status_code != 200:
            raise ReviewError(f"Gemini returned HTTP {response.status_code} after retries"
                              if response.status_code in (408, 429, 500, 502, 503, 504)
                              else f"Gemini returned HTTP {response.status_code}")
        try:
            data = json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
            result = {
                "verdict": "skip" if data["verdict"] == "skip" else "send",
                "score": max(1, min(10, int(data["score"]))),
                "new_price": max(0, int(data["new_price"])),
                "summary": str(data["summary"])[:120],
                "warnings": [str(w)[:100] for w in data["warnings"]][:3],
                "photo": image is not None,
            }
        except (ValueError, KeyError, IndexError, TypeError):
            raise ReviewError("Gemini answer could not be read") from None
        return result
