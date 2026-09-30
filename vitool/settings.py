import copy

# Popular brands shown by Vinted Germany's public men's pullover filter on
# 2026-09-26. Ralph Lauren's two Vinted labels are intentionally one option.
BRANDS = {
    "Ralph Lauren": [88, 4273], "Gant": [6075], "Nike": [53],
    "Tommy Hilfiger": [94], "adidas": [14], "Lacoste": [304],
    "Jack & Jones": [36955], "H&M": [7], "Vintage Dressing": [14803],
    "Zara": [12], "Champion": [7973], "Puma": [535], "Celio": [2615],
    "Carhartt": [362], "Pull & Bear": [4690593], "Jules": [3383],
    "Stone Island": [73306], "The North Face": [2319], "Levi's": [10],
    "Jordan": [2703], "Superdry": [191], "Kiabi": [60],
    "Calvin Klein": [255], "Bershka": [140], "Hollister": [11493],
    "Hugo Boss": [120], "FILA": [5291], "Shein": [172724],
    "Primark": [105], "Tommy Jeans": [352755], "Kappa": [8139],
    "Uniqlo": [1153], "Under Armour": [52035], "Ellesse": [787],
    "Devred": [4711], "Vans": [139], "Tom Tailor": [1845],
    "Fred Perry": [2929], "Napapijri": [214], "C&A": [11425],
    "C.P. Company": [73952], "Kenzo": [1075], "Teddy Smith": [132],
    "Diesel": [161], "GUESS": [20], "U.S. Polo Assn.": [7299],
    "Calvin Klein Jeans": [6393106], "Nautica": [52259],
    "Lyle & Scott": [66644],
}
BRAND_ALIASES = {name: {name.casefold()} for name in BRANDS}
BRAND_ALIASES["Ralph Lauren"].add("polo ralph lauren")
COLORS = {
    "brown": {"braun", "brown", "marron", "marrone", "bruin"},
    "black": {"schwarz", "black", "noir", "nero", "zwart"},
    "blue": {"blau", "blue", "bleu", "blu", "blauw"},
    "grey": {"grau", "grey", "gray", "gris", "grigio", "grijs"},
    "beige": {"beige"}, "white": {"weiß", "weiss", "white", "blanc", "bianco"},
    "green": {"grün", "green", "vert", "verde", "groen"},
    "red": {"rot", "red", "rouge", "rosso", "rood"},
}
CONDITIONS = {"Sehr gut", "Neu", "Neu, ohne Etikett", "Neu, mit Etikett", "Gut"}
MATERIALS = {
    "cotton", "wool", "merino", "cashmere", "acrylic", "polyester",
    "viscose", "fleece", "nylon", "elastane", "velvet", "tweed",
    "corduroy", "denim", "leather", "faux fur", "suede", "mesh",
}
DEFAULTS = {
    "brands": ["Ralph Lauren", "Gant"], "sizes": ["M"],
    "categories": ["pullovers", "shirts", "jackets"],
    "colors": ["brown", "black"], "max_price": 20,
    "conditions": ["Sehr gut", "Neu", "Neu, ohne Etikett", "Neu, mit Etikett"],
    "materials": [], "necklines": [], "departments": ["men"],
    "interval_seconds": 600, "paused": True,
}


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("Settings must be an object")
    value = copy.deepcopy(value)
    # Migrate settings saved by versions that used whole minutes.
    if "interval_minutes" in value:
        value.setdefault("interval_seconds", value["interval_minutes"] * 60)
        value.pop("interval_minutes")
    if "department" in value:
        value.setdefault("departments", [value.pop("department")])
    if "style" in value:
        style = value.pop("style")
        value.setdefault("necklines", [] if style in {"any", "prefer_half_zip"} else [style])
    if set(value) - set(DEFAULTS):
        raise ValueError("Unknown settings fields")
    result = copy.deepcopy(DEFAULTS)
    result.update(value)
    for field, allowed in [("brands", BRANDS), ("colors", COLORS),
                           ("conditions", CONDITIONS), ("sizes", {"XS", "S", "M", "L", "XL", "XXL"}),
                           ("departments", {"men", "women"}),
                           ("categories", {"pullovers", "shirts", "jackets"})]:
        items = result[field]
        if field == "brands" and isinstance(items, list):
            canonical = {" ".join(name.split()).casefold(): name for name in allowed}
            items = [canonical.get(" ".join(x.split()).casefold()) if isinstance(x, str) else None for x in items]
        if not isinstance(items, list) or not items or not all(isinstance(x, str) and x in allowed for x in items):
            raise ValueError(f"Choose valid {field}")
        result[field] = list(dict.fromkeys(items))
    if len(result["brands"]) > 10:
        raise ValueError("Choose no more than 10 brands")
    if not isinstance(result["materials"], list) or not all(
            isinstance(x, str) and x in MATERIALS for x in result["materials"]):
        raise ValueError("Choose valid materials")
    result["materials"] = list(dict.fromkeys(result["materials"]))
    if not isinstance(result["necklines"], list) or not all(
            isinstance(x, str) and x in {"half_zip", "v_neck", "crew_neck"} for x in result["necklines"]):
        raise ValueError("Choose valid necklines")
    result["necklines"] = list(dict.fromkeys(result["necklines"]))
    if type(result["max_price"]) not in (int, float) or not 1 <= result["max_price"] <= 1000:
        raise ValueError("Price must be between €1 and €1000")
    if type(result["interval_seconds"]) is not int or not 10 <= result["interval_seconds"] <= 86400:
        raise ValueError("Interval must be 10–86400 whole seconds")
    if type(result["paused"]) is not bool:
        raise ValueError("Invalid pause setting")
    return result
