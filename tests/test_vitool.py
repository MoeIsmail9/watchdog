import copy
import json
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
import pytest

from vitool.app import handler_for
from vitool.matching import basic_match, match_item
from vitool.settings import BRANDS, DEFAULTS, validate
from vitool.source import ItemSourceError, SourceError, VintedSource, parse_catalog, parse_detail, search_url
from vitool.store import Store
from vitool.telegram import TelegramError, handle_command
from vitool.worker import Worker


def item(id="1", **kwargs):
    return {"id": id, "brand": "Polo Ralph Lauren", "title": "Brown half zip pullover",
            "size": "M", "condition": "Sehr gut", "price": 20, "image": "",
            "url": f"https://www.vinted.de/items/{id}-pullover", "color_text": "Farbe Braun",
            "description": "Keine Flecken oder Löcher", **kwargs}


class Source:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error
        self.calls = 0
        self.detail_calls = 0

    def catalog(self, settings, brand):
        self.calls += 1
        if self.error:
            raise self.error
        return copy.deepcopy(self.items)

    def details(self, item):
        self.detail_calls += 1
        return {"color_text": "Braun", "description": "half zip"}


class Bot:
    ready = True
    chat_id = "123"

    def __init__(self):
        self.alerts = []
        self.messages = []
        self.fail = False

    def alert(self, item):
        if self.fail:
            raise TelegramError("Delivery failed")
        self.alerts.append(item)

    def send(self, text):
        self.messages.append(text)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def scan_again(worker):
    worker.store.set("next_scan", 0)
    worker.scan(force=True)


def test_matching_price_brand_size_color_and_style():
    assert match_item(item(), DEFAULTS)[0]
    assert match_item(item(condition="Neu"), DEFAULTS)[0]
    for changes in [{"price": 20.01}, {"brand": "Chaps Ralph Lauren"}, {"size": "XL"},
                    {"condition": "Gut"}, {"color_text": "Grau"}, {"color_text": ""},
                    {"description": "Pullover mit Flecken"}]:
        assert not match_item(item(**changes), DEFAULTS)[0]
    assert not basic_match(item(size="24–36 Monate / 92"), DEFAULTS)
    assert match_item(item(title="Pullover"), DEFAULTS)[0]
    assert not match_item(item(title="Pullover"), {**DEFAULTS, "necklines": ["half_zip"]})[0]
    assert match_item(item(title="Troyer"), {**DEFAULTS, "necklines": ["half_zip"]})[0]
    assert match_item(item(title="V-neck pullover"), {**DEFAULTS, "necklines": ["half_zip", "v_neck"]})[0]
    assert match_item(item(department="women"), {**DEFAULTS, "departments": ["men", "women"]})[0]
    assert not match_item(item(department="women"), DEFAULTS)[0]
    assert match_item(item(brand="Nike"), {**DEFAULTS, "brands": ["Nike"]})[0]


def test_catalogue_colour_filter_is_used_when_detail_colour_is_missing():
    candidate = item(color_text="", catalog_colors=["brown", "black"])
    matched, reason = match_item(candidate, DEFAULTS)
    assert matched
    assert "Vinted colour filter" in reason
    # A detail-page colour is more specific and takes precedence when available.
    assert not match_item(item(color_text="Farbe Grau", catalog_colors=["brown"]), DEFAULTS)[0]


def test_validation_rejects_unsafe_values():
    for change in [{"max_price": float("nan")}, {"max_price": True}, {"interval_seconds": 9},
                   {"brands": []}, {"colors": ["purple"]}, {"sizes": "M"}, {"departments": []},
                   {"necklines": ["polo"]}, {"paused": "no"}]:
        with pytest.raises(ValueError):
            validate({**DEFAULTS, **change})
    assert validate({**DEFAULTS, "brands": [" gant ", "GANT", "nike", "Nike"]})["brands"] == ["Gant", "Nike"]
    with pytest.raises(ValueError):
        validate({**DEFAULTS, "brands": list(BRANDS)[:11]})


def test_catalog_and_detail_parse_actual_public_markup():
    html = '''<a data-testid="product-item-id-42--overlay-link" href="/items/42-zip?referrer=catalog"
      title="Zip &amp; knit, Marke: GANT, Zustand: Neu, mit Etikett, Größe: M, 19.50 €, 20.98 €"></a>
      <img data-testid="product-item-id-42--image--img" src="https://images1.vinted.net/a.webp">'''
    parsed = parse_catalog(html)[0]
    assert parsed["title"] == "Zip & knit"
    assert parsed["condition"] == "Neu, mit Etikett"
    assert parsed["price"] == 19.5
    assert parsed["url"] == "https://www.vinted.de/items/42-zip"
    details = parse_detail('<div data-testid="item-attributes-color">Farbe <span>Schwarz</span></div><div itemprop="description">Half zip</div>')
    assert details == {"color_text": "Farbe Schwarz", "description": "Half zip"}
    with pytest.raises(SourceError):
        parse_catalog("<html>Something changed</html>")


def test_search_filters_match_native_vinted_controls():
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(search_url(DEFAULTS, "Ralph Lauren")).query)
    assert query["color_ids[]"] == ["2", "1"]
    assert query["size_ids[]"] == ["208"]
    assert query["brand_ids[]"] == ["88", "4273"]
    assert query["order"] == ["newest_first"]
    assert "material_ids[]" not in query
    women_query = parse_qs(urlparse(search_url(DEFAULTS, "Ralph Lauren", "women")).query)
    assert women_query["catalog[]"] == ["10"]
    assert "size_ids[]" not in women_query
    material_query = parse_qs(urlparse(search_url({**DEFAULTS, "materials": ["cotton", "cashmere"]}, "Gant")).query)
    assert material_query["material_ids[]"] == ["44", "123"]
    assert parse_qs(urlparse(search_url({**DEFAULTS, "brands": ["Nike"]}, "Nike")).query)["brand_ids[]"] == ["53"]


def test_old_minute_interval_migrates_to_seconds():
    old = {k: v for k, v in DEFAULTS.items() if k != "interval_seconds"}
    old["interval_minutes"] = 10
    assert validate(old)["interval_seconds"] == 600
    legacy = {k: v for k, v in old.items() if k not in {"departments", "necklines"}}
    migrated = validate({**legacy, "department": "women", "style": "v_neck"})
    assert migrated["departments"] == ["women"]
    assert migrated["necklines"] == ["v_neck"]


def test_success_clears_error_and_preserves_other_status_fields(store):
    worker = Worker(store, Source([item()]), Bot())
    worker.status(error=True, telegram_error="Previous delivery error")
    scan_again(worker)
    assert store.get("status")["error"] is False
    assert store.get("next_scan") >= time.time() + DEFAULTS["interval_seconds"] - 2


def test_detail_budget_alternates_brands(store):
    class ByBrand(Source):
        detail_brands = []
        include_new = False

        def catalog(self, settings, brand):
            old = [item(brand + "-old-" + str(n), brand=brand, color_text="") for n in range(8)]
            new = [item(brand + "-new-" + str(n), brand=brand, color_text="") for n in range(4)]
            return new + old if self.include_new else old

        def details(self, candidate):
            self.detail_brands.append(candidate["brand"])
            return super().details(candidate)

    source = ByBrand()
    worker = Worker(store, source, Bot())
    scan_again(worker)
    source.include_new = True
    scan_again(worker)
    assert source.detail_calls == 6
    assert source.detail_brands.count("Gant") == 3
    assert source.detail_brands.count("Ralph Lauren") == 3
    assert len([i for i in store.matches() if i["brand"] == "Gant"]) == 4


def test_baseline_duplicates_and_delivery_retry(store):
    source, bot = Source([item()]), Bot()
    worker = Worker(store, source, bot)
    scan_again(worker)
    assert len(store.matches()) == 0 and not bot.alerts
    source.items.insert(0, item("2"))
    bot.fail = True
    scan_again(worker)
    assert not store.item("2")["delivered"]
    bot.fail = False
    scan_again(worker)
    scan_again(worker)
    assert [i["id"] for i in bot.alerts] == ["2"]
    assert store.item("2")["delivered"]


def test_scan_status_explains_new_listing_that_did_not_match(store):
    store.save_settings({**DEFAULTS, "necklines": ["half_zip"], "paused": False})
    source = Source([item("anchor")])
    worker = Worker(store, source, Bot())
    scan_again(worker)
    source.items.insert(0, item("plain", title="Polo Ralph Lauren Pulli", description=""))

    scan_again(worker)

    status = store.get("status")
    assert status["new_listings"] == 1
    assert status["matches"] == 0
    assert "0 matched your filters" in status["message"]
    assert "Requested neckline is not confirmed in the text" in status["message"]


def test_same_listing_from_multiple_brand_pages_is_saved_once(store):
    class DuplicateAcrossPages(Source):
        current_id = "shared"

        def catalog(self, settings, brand):
            rows = [item(self.current_id, brand=brand)]
            if self.current_id != "shared":
                rows.append(item("shared", brand=brand))
            return rows

    settings = {**DEFAULTS, "brands": ["Nike", "Gant"]}
    store.save_settings(settings)
    source = DuplicateAcrossPages()
    worker = Worker(store, source, Bot())
    scan_again(worker)
    source.current_id = "new-shared"
    scan_again(worker)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM items WHERE id='new-shared'").fetchone()[0] == 1
    assert len(store.matches()) == 1


def test_failed_delivery_is_not_retried_after_filters_change(store):
    source, bot = Source([item()]), Bot()
    worker = Worker(store, source, bot)
    scan_again(worker)
    source.items.insert(0, item("2"))
    bot.fail = True
    scan_again(worker)
    store.save_settings({**DEFAULTS, "max_price": 10})
    bot.fail = False
    scan_again(worker)
    assert not bot.alerts


def test_detail_budget_only_applies_to_new_items(store):
    anchor = item("anchor", color_text="")
    source = Source([anchor])
    worker = Worker(store, source, Bot())
    scan_again(worker)
    source.items = [item(str(n), color_text="") for n in range(9)] + [anchor]
    scan_again(worker)
    assert source.detail_calls == 6
    assert store.get("status")["deferred"] == 3
    scan_again(worker)
    assert source.detail_calls == 6
    assert store.get("status")["new_listings"] == 0
    assert store.get("status")["existing_skipped"] == 10


def test_filter_change_baselines_current_items_then_alerts_only_new_ids(store):
    source, bot = Source([item("old")]), Bot()
    worker = Worker(store, source, bot)
    scan_again(worker)
    store.save_settings({**DEFAULTS, "brands": ["Nike"]})
    source.items = [item("nike-current", brand="Nike")]
    scan_again(worker)
    assert not bot.alerts
    source.items.insert(0, item("nike-new", brand="Nike"))
    scan_again(worker)
    assert [found["id"] for found in bot.alerts] == ["nike-new"]


def test_worker_scans_every_selected_department_and_brand(store):
    class RecordingSource(Source):
        departments = []

        def catalog(self, settings, brand):
            self.departments.append((settings["department"], brand))
            return [item(f"{settings['department']}-{brand}", brand=brand)]

    store.save_settings({**DEFAULTS, "departments": ["men", "women"]})
    source = RecordingSource()
    scan_again(Worker(store, source, Bot()))
    assert set(source.departments) == {
        ("men", "Ralph Lauren"), ("men", "Gant"),
        ("women", "Ralph Lauren"), ("women", "Gant"),
    }
    assert set(store.get("brand_cursors")) == {
        "men:Ralph Lauren", "men:Gant", "women:Ralph Lauren", "women:Gant",
    }


def test_cursor_ignores_unseen_items_below_previous_newest(store):
    source, bot = Source([item("anchor"), item("older")]), Bot()
    worker = Worker(store, source, bot)
    scan_again(worker)
    source.items = [item("new"), item("anchor"), item("never-seen-old")]
    scan_again(worker)
    assert [found["id"] for found in bot.alerts] == ["new"]
    assert store.item("new") is not None
    assert store.item("never-seen-old") is None


def test_unavailable_detail_page_does_not_abort_other_items(store):
    class OneBrokenDetail(Source):
        def details(self, candidate):
            self.detail_calls += 1
            if candidate["id"] == "broken":
                raise ItemSourceError("unavailable")
            return {"color_text": "Braun", "description": "half zip"}

    anchor = item("anchor")
    source = OneBrokenDetail([anchor])
    worker = Worker(store, source, Bot())
    scan_again(worker)
    source.items = [item("broken", color_text=""), item("working", color_text=""), anchor]
    scan_again(worker)
    assert store.get("status")["error"] is False
    assert store.get("status")["detail_errors"] == 1
    assert {match["id"] for match in store.matches()} == {"broken", "working"}


def test_blocks_and_cooldown_survive_restart(store):
    source = Source(error=SourceError("blocked", blocked=True))
    worker = Worker(store, source, Bot())
    scan_again(worker)
    assert store.get("blocked") and store.settings()["paused"]
    restarted = Worker(Store(store.directory), source, Bot())
    assert not restarted.scan(force=True)
    assert source.calls == 1
    handle_command("/resume", store)
    assert not store.get("blocked")
    assert not restarted.scan(force=True)  # Resume does not bypass cooldown.


def test_retry_after_is_honored_without_request_retry():
    source = VintedSource()
    source.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "7200"})))
    with pytest.raises(SourceError) as exc:
        source.get("https://www.vinted.de/catalog")
    assert exc.value.delay == 7200


def test_telegram_commands_persist_settings(store):
    handle_command("/price 18", store)
    handle_command("/colors black", store)
    handle_command("/necklines half_zip,v_neck", store)
    handle_command("/materials cotton,cashmere", store)
    store.set("next_scan", time.time() + 600)
    handle_command("/interval 30", store)
    settings = Store(store.directory).settings()
    assert settings["max_price"] == 18
    assert settings["colors"] == ["black"]
    assert settings["necklines"] == ["half_zip", "v_neck"]
    assert settings["materials"] == ["cotton", "cashmere"]
    assert settings["interval_seconds"] == 30
    assert store.get("next_scan") <= time.time() + 31
    with pytest.raises(ValueError):
        handle_command("/interval 9", store)


def test_local_http_settings_and_csrf(store):
    worker = Worker(store, Source(), Bot())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store, worker))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/state").json()["settings"]["max_price"] == 20
            brands = client.get("/api/state").json()["options"]["brands"]
            assert len(brands) == 49
            assert len({brand.casefold() for brand in brands}) == len(brands)
            assert "Ralph Lauren" in brands and "Polo Ralph Lauren" not in brands
            assert client.post("/api/settings", json=DEFAULTS).status_code == 403
            assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
            response = client.post("/api/settings", json={**DEFAULTS, "max_price": 17}, headers={"X-Vitool": "local"})
            assert response.status_code == 200
            assert store.settings()["max_price"] == 17
            assert store.get("next_scan") <= time.time() + DEFAULTS["interval_seconds"] + 1
    finally:
        server.shutdown()
        server.server_close()


def test_public_dashboard_requires_password_and_keeps_health_public(store, monkeypatch):
    monkeypatch.setenv("VITOOL_PUBLIC", "1")
    monkeypatch.setenv("VITOOL_DASHBOARD_PASSWORD", "strong-test-password")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store, Worker(store, Source(), Bot())))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/").status_code == 401
            assert client.get("/", auth=("wrong", "strong-test-password")).status_code == 401
            assert client.get("/", auth=("vitool", "strong-test-password")).status_code == 200
            response = client.post(
                "/api/settings", json={**DEFAULTS, "max_price": 16},
                headers={"X-Vitool": "local"}, auth=("vitool", "strong-test-password"),
            )
            assert response.status_code == 200
            assert store.settings()["max_price"] == 16
    finally:
        server.shutdown()
        server.server_close()
