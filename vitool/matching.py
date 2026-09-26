import re
from .settings import BRAND_ALIASES, COLORS

STYLE_PATTERNS = {
    "half_zip": r"half[ -]?zip|quarter[ -]?zip|1/4[ -]?zip|halbzip|troyer|demi[ -]?zip|col zipp[ée]",
    "v_neck": r"v[ -]?(?:neck|ausschnitt)|col en v|scollo a v",
    "crew_neck": r"crew[ -]?neck|rundhals|col rond|girocollo",
}


def basic_match(item, settings):
    if item.get("department", settings["departments"][0]) not in settings["departments"]:
        return False
    accepted_brands = set().union(*(BRAND_ALIASES[brand] for brand in settings["brands"]))
    if " ".join(item["brand"].split()).casefold() not in accepted_brands:
        return False
    if item["price"] > settings["max_price"] or item["condition"] not in settings["conditions"]:
        return False
    if settings["materials"] and not set(settings["materials"]).intersection(item.get("material_filter", [])):
        return False
    return item["size"].split("/")[0].strip().upper() in settings["sizes"]


def match_item(item, settings):
    if not basic_match(item, settings):
        return False, "Outside brand, size, price or condition filters"
    color_text = item.get("color_text", "").lower()
    if color_text:
        colors = [color for color in settings["colors"]
                  if any(re.search(r"\b" + re.escape(word) + r"\b", color_text) for word in COLORS[color])]
        color_reason = ", ".join(colors)
    else:
        # Catalog requests already use Vinted's native colour IDs. Preserve that
        # evidence when a listing detail page is unavailable or still queued.
        catalog_colors = set(item.get("catalog_colors", []))
        colors = [color for color in settings["colors"] if color in catalog_colors]
        color_reason = ", ".join(colors) + " · Vinted colour filter"
    if not colors:
        return False, "Colour does not match or is not known"
    text = (item["title"] + " " + item.get("description", "")).lower()
    # Negation-aware examples: 'keine Flecken' is not rejected. Unhandled wording stays visible.
    defects = re.search(r"\b(?:mit flecken|hat flecken|mit löchern|hat löcher|beschädigt|starkes pilling|stained|damaged|avec des taches)\b", text)
    if defects:
        return False, "Description mentions damage: " + defects[0]
    neckline_hits = [neckline for neckline in settings["necklines"]
                     if re.search(STYLE_PATTERNS[neckline], text)]
    if settings["necklines"] and not neckline_hits:
        return False, "Requested neckline is not confirmed in the text"
    detected = neckline_hits or [neckline for neckline, pattern in STYLE_PATTERNS.items()
                                 if re.search(pattern, text)]
    neckline_reason = ", ".join(name.replace("_", " ") for name in detected) + " mentioned" if detected else "neckline not confirmed"
    reason = color_reason + " · " + neckline_reason
    return True, reason + " · seller-stated condition; no AI verification"
