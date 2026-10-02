"""Kleinanzeigen saved searches: public search pages, newest first.

Free-text searches with an optional place and radius. No login, cookies,
proxies or challenge bypass; the same request safety as the Vinted source.
"""
import json
import re
import secrets
import time
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from .source import SourceError, retry_seconds

BASE = "https://www.kleinanzeigen.de"
RADII = (0, 5, 10, 20, 30, 50, 100, 150, 200)
MAX_SEARCHES = 5
PRICE_PATTERN = re.compile(r"\d[\d.]*(?:,\d{1,2})?\s*€(?:\s*VB)?|VB|Zu verschenken")


def slug(text):
    return quote("-".join(text.lower().split()), safe="-")


def search_url(search):
    """Build a newest-first search URL, e.g. /s-10115/preis::400/sortierung:neuste/iphone/k0l9668r20."""
    parts = []
    if search.get("location_id"):
        parts.append(slug(search["plz"]))
    if search.get("max_price"):
        parts.append(f"preis::{int(search['max_price'])}")
    parts += ["sortierung:neuste", slug(search["query"])]
    tail = "k0"
    if search.get("location_id"):
        tail += f"l{search['location_id']}" + (f"r{search['radius']}" if search.get("radius") else "")
    return f"{BASE}/s-" + "/".join(parts) + "/" + tail


def lookup_location(place):
    """Resolve a PLZ or city to Kleinanzeigen's location ID and label."""
    try:
        response = httpx.get(f"{BASE}/s-ort-empfehlungen.json", params={"query": place}, timeout=15,
                             headers={"User-Agent": "Vitool/0.1 personal listing watcher"})
        options = response.json()
    except (httpx.HTTPError, ValueError):
        raise ValueError("Kleinanzeigen place lookup failed; try again later") from None
    for key, label in options.items() if isinstance(options, dict) else []:
        if key != "_0" and key.lstrip("_").isdigit():
            return key.lstrip("_"), label
    raise ValueError(f"Place “{place}” was not found on Kleinanzeigen")


def validate_search(value):
    """Validate a new search from the dashboard. Returns a clean search without location data."""
    if not isinstance(value, dict):
        raise ValueError("Search must be an object")
    query = " ".join(str(value.get("query", "")).split())
    if not 2 <= len(query) <= 60:
        raise ValueError("Search text must be 2–60 characters")
    place = " ".join(str(value.get("plz", "")).split())
    if len(place) > 40:
        raise ValueError("Place is too long")
    try:
        radius = int(value.get("radius") or 0)
        max_price = value.get("max_price")
        max_price = None if max_price in (None, "") else float(max_price)
    except (TypeError, ValueError):
        raise ValueError("Radius and max price must be numbers") from None
    if radius not in RADII:
        raise ValueError("Choose a valid radius")
    if max_price is not None and not 1 <= max_price <= 100000:
        raise ValueError("Max price must be between €1 and €100000")
    return {"id": secrets.token_hex(4), "query": query, "plz": place, "location_id": "",
            "location_label": "", "radius": radius if place else 0, "max_price": max_price}


def parse_price(text):
    """'1.200 € VB' → 1200.0, 'Zu verschenken' → 0.0, 'VB' → None."""
    if "verschenken" in text.lower():
        return 0.0
    match = re.search(r"(\d[\d.]*)(?:,(\d{1,2}))?\s*€", text)
    if not match:
        return None
    return float(match[1].replace(".", "") + "." + (match[2] or "0"))


def parse_results(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    articles = soup.select("article[data-adid][data-href]")
    for article in articles:
        ad_id = article["data-adid"]
        if not ad_id.isdigit():
            continue
        data = {}
        script = article.find("script", attrs={"type": "application/ld+json"})
        if script and script.string:
            try:
                data = json.loads(script.string)
            except ValueError:
                data = {}
        heading = article.find("h3")
        title = data.get("title") or (heading.get_text(" ", strip=True) if heading else "")
        spans = [span.get_text(" ", strip=True) for span in article.find_all("span")]
        location = next((text for text in spans if re.match(r"^\d{5}\b", text)), "")
        distance = next((text.strip("()") for text in spans if re.fullmatch(r"\(\s*\d+\s*km\s*\)", text)), "")
        # The description snippet is also a <p> and may mention prices ("Neupreis 900 €"),
        # so only a paragraph that is nothing but a price counts.
        price_text = next((text for text in (p.get_text(" ", strip=True) for p in article.find_all("p"))
                           if PRICE_PATTERN.fullmatch(text)), "")
        items.append({"id": "ka:" + ad_id, "ad_id": int(ad_id), "title": title,
                      "description": data.get("description", ""), "price": parse_price(price_text),
                      "price_text": price_text, "location": location, "distance": distance,
                      "url": BASE + article["data-href"].split("?")[0], "image": data.get("contentUrl", ""),
                      "source": "kleinanzeigen"})
    if not articles:
        # Fail closed: an unexpected page must not silently look like an empty search.
        text = soup.get_text(" ", strip=True).lower()
        if "keine ergebnisse" in text:
            return []
        raise SourceError("Kleinanzeigen returned no readable listings. Page format or access may have changed.")
    return items


class KleinanzeigenSource:
    def __init__(self):
        self.client = httpx.Client(timeout=20, follow_redirects=False, headers={
            "User-Agent": "Vitool/0.1 personal listing watcher",
            "Accept-Language": "de-DE,de;q=0.9", "Accept": "text/html"})
        self.last_request = 0.0

    def get(self, url):
        # One request at a time, at least five seconds apart.
        time.sleep(max(0, 5 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            response = self.client.get(url)
        except httpx.HTTPError:
            raise SourceError("Kleinanzeigen connection failed; retrying after a cooldown.") from None
        if response.status_code in (401, 403):
            raise SourceError(f"Kleinanzeigen blocked access (HTTP {response.status_code}).", blocked=True)
        if response.status_code == 429:
            raise SourceError("Kleinanzeigen rate limit reached. Cooling down.",
                              delay=max(1800, retry_seconds(response.headers.get("Retry-After"))))
        if response.status_code != 200:
            raise SourceError(f"Kleinanzeigen returned HTTP {response.status_code}; scan stopped.")
        if len(response.content) > 8_000_000:
            raise SourceError("Unexpectedly large Kleinanzeigen page; scan stopped.")
        text = response.text
        if any(x in text.lower() for x in ["captcha-delivery.com", "please verify you are a human", "just a moment..."]):
            raise SourceError("Kleinanzeigen requested human verification.", blocked=True)
        return text

    def search(self, search):
        return parse_results(self.get(search_url(search)))
