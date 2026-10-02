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
from vitool.review import Reviewer, ReviewError
from vitool.store import Store
from vitool.telegram import Telegram, TelegramError, handle_command
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
    multi = {**DEFAULTS, "categories": ["pullovers", "shirts", "jackets"],
             "necklines": ["half_zip"]}
    assert match_item(item(category="shirts", title="Brown Oxford shirt"), multi)[0]
    assert match_item(item(category="jackets", title="Brown jacket"), multi)[0]
    assert not match_item(item(category="shirts"), {**DEFAULTS, "categories": ["pullovers"]})[0]


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
    assert women_query["catalog[]"] == ["13"]
    assert "size_ids[]" not in women_query
    for department, category, expected in [
        ("men", "shirts", "536"), ("women", "shirts", "1043"),
        ("men", "jackets", "1206"), ("women", "jackets", "1037"),
    ]:
        query = parse_qs(urlparse(search_url(DEFAULTS, "Gant", department, category)).query)
        assert query["catalog[]"] == [expected]
    material_query = parse_qs(urlparse(search_url({**DEFAULTS, "materials": ["cotton", "cashmere"]}, "Gant")).query)
    assert material_query["material_ids[]"] == ["44", "123"]
    assert parse_qs(urlparse(search_url({**DEFAULTS, "brands": ["Nike"]}, "Nike")).query)["brand_ids[]"] == ["53"]
    combined = parse_qs(urlparse(search_url(DEFAULTS, ["Ralph Lauren", "Gant"])).query)
    assert combined["brand_ids[]"] == ["88", "4273", "6075"]


def test_real_source_reuses_one_combined_catalog_request_for_all_brands():
    html = '''
      <a data-testid="product-item-id-1--overlay-link" href="/items/1-gant"
         title="Pullover, Marke: Gant, Zustand: Sehr gut, Größe: M, 18.00 €, 20.00 €"></a>
      <a data-testid="product-item-id-2--overlay-link" href="/items/2-ralph"
         title="Pullover, Marke: Polo Ralph Lauren, Zustand: Sehr gut, Größe: M, 19.00 €, 21.00 €"></a>
    '''
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=html)

    source = VintedSource()
    source.client = httpx.Client(transport=httpx.MockTransport(respond))
    source.start_scan()
    assert [row["id"] for row in source.catalog(DEFAULTS, "Gant")] == ["1"]
    assert [row["id"] for row in source.catalog(DEFAULTS, "Ralph Lauren")] == ["2"]
    assert len(requests) == 1
    assert requests[0].url.params.get_list("brand_ids[]") == ["88", "4273", "6075"]


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


def test_scheduled_scan_uses_start_to_start_interval(store, monkeypatch):
    import vitool.worker as worker_module

    clock = {"now": 1000}

    class SlowSource(Source):
        def catalog(self, settings, brand):
            clock["now"] = 1120
            return super().catalog(settings, brand)

    monkeypatch.setenv("VITOOL_SCHEDULED", "1")
    monkeypatch.setattr(worker_module.time, "time", lambda: clock["now"])
    worker = Worker(store, SlowSource([item()]), Bot())
    assert worker.scan(force=True)
    assert store.get("next_scan") == 1000 + DEFAULTS["interval_seconds"]
    assert store.get("retry_until") == 0


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


class FakeReviewer:
    ready = True

    def __init__(self, verdicts=None, error=None):
        self.verdicts = verdicts or {}
        self.error = error
        self.calls = []

    def review(self, item, settings):
        self.calls.append(item["id"])
        if self.error:
            raise self.error
        return {"verdict": self.verdicts.get(item["id"], "send"), "score": 8, "new_price": 150,
                "summary": "Brown merino half-zip", "warnings": ["Label not shown"], "photo": True}


def test_ai_review_annotates_skips_and_caches(store):
    source, bot, reviewer = Source([item()]), Bot(), FakeReviewer({"bad": "skip"})
    worker = Worker(store, source, bot, reviewer)
    scan_again(worker)
    source.items[:0] = [item("good"), item("bad")]
    scan_again(worker)
    scan_again(worker)
    assert [i["id"] for i in bot.alerts] == ["good"]
    assert bot.alerts[0]["review"]["score"] == 8
    assert sorted(reviewer.calls) == ["bad", "good"]
    assert not store.item("bad")["matched"] and "AI skipped" in store.item("bad")["reason"]
    assert [i["id"] for i in store.matches()] == ["good"]


def test_ai_review_failure_still_alerts_and_retries_nothing_else(store):
    source, bot = Source([item()]), Bot()
    reviewer = FakeReviewer(error=ReviewError("Gemini returned HTTP 429"))
    worker = Worker(store, source, bot, reviewer)
    scan_again(worker)
    source.items[:0] = [item("2"), item("3")]
    scan_again(worker)
    assert sorted(i["id"] for i in bot.alerts) == ["2", "3"]
    assert len(reviewer.calls) == 1
    assert "review" not in bot.alerts[0]
    assert store.get("status")["review_error"] == "Gemini returned HTTP 429"


def test_reviewer_is_off_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert not Reviewer().ready
    assert Reviewer().model == "gemini-3.5-flash-lite"


def test_reviewer_parses_gemini_answer(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    answer = {"verdict": "send", "score": 14, "new_price": 150, "summary": "ok", "warnings": ["a", "b", "c", "d"]}
    sent = {}

    def post(url, **kwargs):
        sent.update(url=url, **kwargs)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]})

    monkeypatch.setattr(httpx, "post", post)
    result = Reviewer().review(item(), DEFAULTS)
    assert result == {"verdict": "send", "score": 10, "new_price": 150, "summary": "ok",
                      "warnings": ["a", "b", "c"], "photo": False}
    assert sent["headers"]["x-goog-api-key"] == "test"
    assert "Ralph Lauren" in sent["json"]["contents"][0]["parts"][0]["text"]
    monkeypatch.setattr("vitool.review.time.sleep", lambda _: None)
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(429))
    with pytest.raises(ReviewError):
        Reviewer().review(item(), DEFAULTS)


def test_reviewer_retries_temporary_gemini_failure(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr("vitool.review.time.sleep", lambda _: None)
    answer = {"verdict": "send", "score": 8, "new_price": 0, "summary": "ok", "warnings": []}
    responses = iter([httpx.Response(503), httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]})])
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        return next(responses)
    monkeypatch.setattr(httpx, "post", post)
    assert Reviewer().review(item(), DEFAULTS)["verdict"] == "send"
    assert len(calls) == 2


def test_telegram_alert_shows_review(monkeypatch):
    sent = []
    telegram = Telegram()
    monkeypatch.setattr(telegram, "send", sent.append)
    telegram.alert({**item(), "price": 18, "reason": "brown",
                    "review": {"score": 8, "new_price": 150, "summary": "Brown merino half-zip",
                               "warnings": ["Label not shown"], "photo": True, "verdict": "send"}})
    assert "✅ 8/10" in sent[0] and "new ~€150 (−88%)" in sent[0] and "⚠ Label not shown" in sent[0]
    telegram.alert({**item(), "reason": "brown · Vinted colour filter"})
    assert "brown · Vinted colour filter" in sent[1] and "/10" not in sent[1]


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


def test_filter_change_alerts_only_uploads_newer_than_previous_scan(store):
    source, bot = Source([item("100")]), Bot()
    worker = Worker(store, source, bot)
    scan_again(worker)
    store.save_settings({**DEFAULTS, "brands": ["Nike"]})
    # 105 was uploaded after the last scan; 90 is an old listing in the new results.
    source.items = [item("105", brand="Nike"), item("90", brand="Nike")]
    scan_again(worker)
    assert [found["id"] for found in bot.alerts] == ["105"]
    source.items.insert(0, item("110", brand="Nike"))
    scan_again(worker)
    assert [found["id"] for found in bot.alerts] == ["105", "110"]
    assert store.get("watermark") == 110


def test_watermark_starts_from_existing_cursors(store):
    store.set("initialized", True)
    store.set("brand_cursors", {"men:Ralph Lauren": "100"})
    store.set("search_signature", "old filters")
    source, bot = Source([item("101"), item("99")]), Bot()
    scan_again(Worker(store, source, bot))
    assert [found["id"] for found in bot.alerts] == ["101"]


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
        f"{department}:{category}:{brand}"
        for department in ("men", "women")
        for category in DEFAULTS["categories"]
        for brand in DEFAULTS["brands"]
    }


def test_new_clothing_types_scan_separately_without_resending_old_results(store):
    class ByCategory(Source):
        new_shirt = False

        def catalog(self, settings, brand):
            if brand != "Gant":
                return []
            category = settings["category"]
            ids = {"pullovers": ["100"], "shirts": ["90"], "jackets": ["80"]}[category]
            if category == "shirts" and self.new_shirt:
                ids.insert(0, "101")
            return [item(id, brand="Gant", title=f"Brown {category}") for id in ids]

    source, bot = ByCategory(), Bot()
    worker = Worker(store, source, bot)
    store.save_settings({**DEFAULTS, "categories": ["pullovers"]})
    scan_again(worker)
    store.save_settings({**DEFAULTS, "categories": ["pullovers", "shirts", "jackets"]})
    scan_again(worker)
    assert not bot.alerts
    assert set(store.get("brand_cursors")) == {
        "men:pullovers:Ralph Lauren", "men:pullovers:Gant",
        "men:shirts:Ralph Lauren", "men:shirts:Gant",
        "men:jackets:Ralph Lauren", "men:jackets:Gant",
    }
    source.new_shirt = True
    scan_again(worker)
    assert [alert["id"] for alert in bot.alerts] == ["101"]
    assert bot.alerts[0]["category"] == "shirts"


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
    handle_command("/categories pullovers,shirts,jackets", store)
    store.set("next_scan", time.time() + 600)
    handle_command("/interval 30", store)
    settings = Store(store.directory).settings()
    assert settings["max_price"] == 18
    assert settings["colors"] == ["black"]
    assert settings["necklines"] == ["half_zip", "v_neck"]
    assert settings["materials"] == ["cotton", "cashmere"]
    assert settings["categories"] == ["pullovers", "shirts", "jackets"]
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
            assert client.get("/", auth=("watchdog", "strong-test-password")).status_code == 200
            response = client.post(
                "/api/settings", json={**DEFAULTS, "max_price": 16},
                headers={"X-Vitool": "local"}, auth=("watchdog", "strong-test-password"),
            )
            assert response.status_code == 200
            assert store.settings()["max_price"] == 16
    finally:
        server.shutdown()
        server.server_close()


# --- Kleinanzeigen -------------------------------------------------------

from vitool import kleinanzeigen
from vitool.telegram import ka_text

KA_HTML = """<ul>
<li><article data-adid="1457457547" data-href="/s-anzeige/top-gazelle/1457457547-217-1897">
<script type="application/ld+json">{"title":"TOP Gazelle Hollandrad","description":"Alt","contentUrl":"https://img.kleinanzeigen.de/a.jpg"}</script>
<div><span>40667 Meerbusch</span></div><h3><a>TOP Gazelle Hollandrad</a></h3><p>170 € VB</p></article></li>
<li><article data-adid="3529076916" data-href="/s-anzeige/iphone-14-128gb-schwarz/3529076916-173-3376?x=1">
<script type="application/ld+json">{"title":"iPhone 14 - 128GB - Schwarz","description":"Batterie 81%","contentUrl":"https://img.kleinanzeigen.de/b.jpg"}</script>
<div><span>10963 Kreuzberg</span><span>(2 km)</span></div><div><span>Heute, 16:48</span></div>
<h3><a>iPhone 14 - 128GB - Schwarz</a></h3><p>iPhone 14 - 128GB, Neupreis 900 € ...</p><div><p>1.200 € VB</p><p>1.390 €</p></div></article></li>
<li><article data-adid="3529075629" data-href="/s-anzeige/karton/3529075629-406-9642">
<h3><a>Umzugskartons</a></h3><p>Zu verschenken</p></article></li>
</ul>"""


def ka_item(ad_id, price=300.0, **kwargs):
    return {"id": f"ka:{ad_id}", "ad_id": ad_id, "title": f"iPhone {ad_id}", "description": "",
            "price": price, "price_text": f"{price:.0f} € VB" if price is not None else "VB", "location": "10963 Kreuzberg",
            "distance": "2 km", "url": f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}",
            "image": "", "source": "kleinanzeigen", **kwargs}


class KaSource:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls = []

    def search(self, search):
        self.calls.append(search["query"])
        if self.error:
            raise self.error
        return copy.deepcopy(self.results)


def ka_search(id="s1", **kwargs):
    return {"id": id, "query": "iphone", "plz": "10115", "location_id": "9668",
            "location_label": "10115 Mitte", "radius": 20, "max_price": 400, **kwargs}


def ka_worker(store, results, reviewer=None, bot=None):
    store.save_settings({**DEFAULTS, "paused": False})
    store.set("ka_searches", [ka_search()])
    source = KaSource(results)
    worker = Worker(store, Source(), bot or Bot(), reviewer, ka_source=source)
    return worker, source


def ka_scan_again(worker):
    worker.store.set("ka_next_scan", 0)
    return worker.scan_kleinanzeigen()


def test_ka_parse_results_from_public_markup():
    items = kleinanzeigen.parse_results(KA_HTML)
    assert [i["id"] for i in items] == ["ka:1457457547", "ka:3529076916", "ka:3529075629"]
    phone = items[1]
    assert phone["title"] == "iPhone 14 - 128GB - Schwarz" and phone["price"] == 1200.0
    assert phone["price_text"] == "1.200 € VB" and phone["location"] == "10963 Kreuzberg"
    assert phone["distance"] == "2 km" and phone["image"] == "https://img.kleinanzeigen.de/b.jpg"
    assert phone["url"] == "https://www.kleinanzeigen.de/s-anzeige/iphone-14-128gb-schwarz/3529076916-173-3376"
    assert items[2]["price"] == 0.0 and items[2]["title"] == "Umzugskartons"
    assert kleinanzeigen.parse_results("<p>Es wurden leider keine Ergebnisse gefunden.</p>") == []
    with pytest.raises(SourceError):
        kleinanzeigen.parse_results("<p>Something else</p>")
    assert kleinanzeigen.parse_price("VB") is None


def test_ka_search_url_and_validation():
    assert kleinanzeigen.search_url(ka_search(query="iPhone 15")) == \
        "https://www.kleinanzeigen.de/s-10115/preis::400/sortierung:neuste/iphone-15/k0l9668r20"
    assert kleinanzeigen.search_url(ka_search(query="Kühlschrank", plz="", location_id="", radius=0, max_price=None)) == \
        "https://www.kleinanzeigen.de/s-sortierung:neuste/k%C3%BChlschrank/k0"
    clean = kleinanzeigen.validate_search({"query": "  iPhone   15 ", "plz": "", "radius": 50, "max_price": ""})
    assert clean["query"] == "iPhone 15" and clean["radius"] == 0 and clean["max_price"] is None
    for bad in [{"query": "x"}, {"query": "ok", "radius": 7}, {"query": "ok", "max_price": 0}, []]:
        with pytest.raises(ValueError):
            kleinanzeigen.validate_search(bad)


def test_ka_lookup_location(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        200, json={"_0": "Deutschland", "_9668": "10115 Mitte", "_3504": "10115 Wedding"}))
    assert kleinanzeigen.lookup_location("10115") == ("9668", "10115 Mitte")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"_0": "Deutschland"}))
    with pytest.raises(ValueError):
        kleinanzeigen.lookup_location("nowhere")


def test_ka_baseline_then_only_newer_listings_within_price(store):
    worker, source = ka_worker(store, [ka_item(100), ka_item(50)])
    assert ka_scan_again(worker)
    assert not worker.telegram.alerts and store.get("ka_watermarks") == {"s1": 100}
    # 60 is an old paid TOP ad, 120 is too expensive, 110 is new and fits.
    source.results = [ka_item(60), ka_item(120, price=900), ka_item(110), ka_item(100)]
    ka_scan_again(worker)
    assert [i["id"] for i in worker.telegram.alerts] == ["ka:110"]
    assert worker.telegram.alerts[0]["query"] == "iphone"
    ka_scan_again(worker)
    assert len(worker.telegram.alerts) == 1
    assert store.get("ka_watermarks") == {"s1": 120}


class KaReviewer(FakeReviewer):
    def review_search(self, item, search):
        return self.review(item, search)


def test_ka_review_skip_and_failure(store):
    worker, source = ka_worker(store, [ka_item(100)], KaReviewer({"ka:110": "skip"}))
    ka_scan_again(worker)
    source.results = [ka_item(111), ka_item(110), ka_item(100)]
    ka_scan_again(worker)
    assert [i["id"] for i in worker.telegram.alerts] == ["ka:111"]
    assert worker.telegram.alerts[0]["review"]["score"] == 8
    assert "AI skipped" in store.item("ka:110")["reason"]
    worker.reviewer = KaReviewer(error=ReviewError("Gemini returned HTTP 503"))
    source.results = [ka_item(112), *source.results]
    ka_scan_again(worker)
    assert worker.telegram.alerts[-1]["id"] == "ka:112" and "review" not in worker.telegram.alerts[-1]


def test_ka_block_backs_off_without_blocking_vinted(store):
    worker, source = ka_worker(store, [])
    source.error = SourceError("Kleinanzeigen blocked access (HTTP 403).", blocked=True)
    assert not ka_scan_again(worker)
    assert not store.get("blocked", False) and not store.settings()["paused"]
    assert store.get("ka_retry_until") > time.time() + 21000
    assert store.get("ka_status")["error"] and "Vinted keeps running" in worker.telegram.messages[0]
    assert not ka_scan_again(worker) and len(source.calls) == 1


def test_ka_respects_pause_and_dashboard_ignores_vinted_matching(store):
    worker, source = ka_worker(store, [ka_item(100)])
    store.save_settings({**DEFAULTS, "paused": True})
    assert not ka_scan_again(worker) and not source.calls
    assert worker.scan_kleinanzeigen(force=True) is True


def test_ka_telegram_text():
    text = ka_text({**ka_item(5, price=300.0), "query": "iphone"},
                   {"score": 8, "new_price": 800, "summary": "iPhone 14, 128 GB", "warnings": ["Battery 81%"],
                    "photo": True, "verdict": "send"})
    assert text.startswith("🔎 iphone — ✅ 8/10") and "300 € VB · new ~€800 (−62%)" in text
    assert "📍 10963 Kreuzberg (2 km)" in text and "⚠ Battery 81%" in text
    plain = ka_text({**ka_item(6, price=None, price_text="VB"), "query": "iphone"}, None)
    assert "/10" not in plain and "\nVB\n" in plain


def test_ka_dashboard_add_and_delete(store, monkeypatch):
    monkeypatch.setattr(kleinanzeigen, "lookup_location", lambda place: ("9668", "10115 Mitte"))
    worker = Worker(store, Source(), Bot(), ka_source=KaSource())
    store.save_item({**ka_item(7), "query": "iphone", "search_id": "x"}, True, "🔎 iphone")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store, worker))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    headers = {"X-Vitool": "local"}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert client.post("/api/ka-search", json={"query": "iphone"}).status_code == 403
            response = client.post("/api/ka-search", headers=headers,
                                   json={"query": "iPhone 15", "plz": "10115", "radius": 20, "max_price": 400})
            assert response.status_code == 200
            state = client.get("/api/state").json()
            [search] = state["ka_searches"]
            assert search["location_id"] == "9668" and search["url"].endswith("/iphone-15/k0l9668r20")
            assert [m["id"] for m in state["matches"]] == ["ka:7"]
            assert client.post("/api/ka-search", headers=headers, json={"query": "x"}).status_code == 400
            client.post("/api/ka-search-delete", headers=headers, json={"id": search["id"]})
            assert client.get("/api/state").json()["ka_searches"] == []
    finally:
        server.shutdown()
        server.server_close()
