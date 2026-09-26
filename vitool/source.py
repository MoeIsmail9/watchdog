"""Public HTML adapter. No private API, login cookies, proxies or challenge bypass."""
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from .settings import BRANDS, BRAND_ALIASES

# Read from Vinted Germany's native filter controls on 2026-09-26.
COLOR_IDS = {"black": 1, "brown": 2, "grey": 3, "beige": 4, "red": 7,
             "blue": 9, "green": 10, "white": 12}
MEN_SIZE_IDS = {"XS": 206, "S": 207, "M": 208, "L": 209, "XL": 210, "XXL": 211}
MATERIAL_IDS = {
    "cotton": 44, "wool": 46, "merino": 121, "cashmere": 123,
    "acrylic": 149, "polyester": 45, "viscose": 48, "fleece": 120,
    "nylon": 52, "elastane": 53, "velvet": 466, "tweed": 465,
    "corduroy": 299, "denim": 303, "leather": 43, "faux fur": 446,
    "suede": 298, "mesh": 456,
}


class SourceError(Exception):
    def __init__(self, message, *, blocked=False, delay=0):
        super().__init__(message)
        self.blocked, self.delay = blocked, delay


class ItemSourceError(SourceError):
    """A single listing cannot be enriched, but the rest of the scan can continue."""


def retry_seconds(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0


def search_url(settings, brand, department=None):
    department = department or settings.get("department") or settings["departments"][0]
    brands = [brand] if isinstance(brand, str) else brand
    category = 79 if department == "men" else 10
    params = [("catalog[]", category), ("order", "newest_first"),
              ("price_to", settings["max_price"]), ("currency", "EUR")]
    params.extend(("brand_ids[]", brand_id) for name in brands for brand_id in BRANDS[name])
    params.extend(("color_ids[]", COLOR_IDS[color]) for color in settings["colors"])
    if department == "men":
        params.extend(("size_ids[]", MEN_SIZE_IDS[size]) for size in settings["sizes"])
    params.extend(("material_ids[]", MATERIAL_IDS[material]) for material in settings["materials"])
    return "https://www.vinted.de/catalog?" + urlencode(params)


def parse_catalog(html):
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    cards = soup.select('a[data-testid$="--overlay-link"][href^="/items/"]')
    for card in cards:
        title = card.get("title", "")
        match = re.fullmatch(r"(.*), Marke: (.*), Zustand: (.*), Größe: (.*), (\d+[.,]\d{2}) €, .*", title)
        item_id = re.match(r"/items/(\d+)", card["href"])
        if not match or not item_id:
            continue
        name, brand, condition, size, price = match.groups()
        image = soup.find("img", attrs={"data-testid": f"product-item-id-{item_id[1]}--image--img"})
        items[item_id[1]] = {"id": item_id[1], "title": name, "brand": brand,
            "condition": condition, "size": size, "price": float(price.replace(",", ".")),
            "url": "https://www.vinted.de" + card["href"].split("?")[0],
            "image": image.get("src", "") if image else "", "color_text": "", "description": ""}
    if not items:
        # Fail closed: an unexpected page must not silently look like an empty search.
        text = soup.get_text(" ", strip=True).lower()
        if any(x in text for x in ["keine artikel gefunden", "keine ergebnisse gefunden"]):
            return []
        raise SourceError("Vinted returned no readable listing cards. Page format or access may have changed.")
    if cards and len(items) < len(cards) * 0.9:
        raise SourceError("Vinted listing format changed; scan stopped to avoid silently missing listings.")
    return list(items.values())


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    color = soup.select_one('[data-testid="item-attributes-color"]')
    description = soup.select_one('[itemprop="description"]')
    # Description markup varies; the public meta description is a conservative fallback.
    meta = soup.find("meta", attrs={"name": "description"})
    if not color:
        raise ItemSourceError("Item colour could not be read; detail page may have changed or item is unavailable.")
    return {"color_text": color.get_text(" ", strip=True),
            "description": description.get_text(" ", strip=True) if description else (meta.get("content", "") if meta else "")}


class VintedSource:
    def __init__(self):
        self.client = httpx.Client(timeout=20, follow_redirects=False, headers={
            "User-Agent": "Vitool/0.1 personal listing watcher",
            "Accept-Language": "de-DE,de;q=0.9", "Accept": "text/html"})
        self.last_request = 0.0
        self.catalog_cache = {}

    def start_scan(self):
        # One combined page can serve every selected brand in a department.
        # Reset between scans so even short local intervals always fetch fresh data.
        self.catalog_cache = {}

    def get(self, url):
        # One request at a time, at least five seconds apart across the entire scan.
        time.sleep(max(0, 5 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            response = self.client.get(url)
        except httpx.HTTPError:
            raise SourceError("Vinted connection failed; retrying after a cooldown.") from None
        if response.status_code in (401, 403):
            raise SourceError(f"Vinted blocked access (HTTP {response.status_code}). Monitoring paused.", blocked=True)
        if response.status_code == 429:
            raise SourceError("Vinted rate limit reached. Cooling down.", delay=max(1800, retry_seconds(response.headers.get("Retry-After"))))
        if response.status_code != 200:
            raise SourceError(f"Vinted returned HTTP {response.status_code}; scan stopped.")
        if len(response.content) > 8_000_000:
            raise SourceError("Unexpectedly large Vinted page; scan stopped.")
        text = response.text
        if any(x in text.lower() for x in ["geo.captcha-delivery.com/captcha", "please verify you are a human", "just a moment..."]):
            raise SourceError("Vinted requested human verification. Monitoring paused.", blocked=True)
        return text

    def catalog(self, settings, brand):
        url = search_url(settings, settings["brands"])
        if url not in self.catalog_cache:
            self.catalog_cache[url] = parse_catalog(self.get(url))
        aliases = BRAND_ALIASES[brand]
        return [item for item in self.catalog_cache[url]
                if " ".join(item["brand"].split()).casefold() in aliases]

    def details(self, item):
        return parse_detail(self.get(item["url"]))
