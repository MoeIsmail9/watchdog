import json
import logging
import os
import re
import threading
import time
from itertools import zip_longest

from .matching import STYLE_PATTERNS, basic_match, match_item
from .kleinanzeigen import KleinanzeigenSource
from .review import Reviewer, ReviewError
from .source import ItemSourceError, SourceError, VintedSource
from .telegram import Telegram, TelegramError, handle_command

log = logging.getLogger(__name__)


def item_number(item_id):
    return int(item_id) if str(item_id).isdigit() else 0


class Worker:
    def __init__(self, store, source=None, telegram=None, reviewer=None, ka_source=None):
        self.store = store
        self.source = source or VintedSource()
        self.ka_source = ka_source or KleinanzeigenSource()
        self.telegram = telegram or Telegram()
        self.reviewer = reviewer or Reviewer()
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def status(self, **changes):
        self.store.patch("status", changes)

    def scan(self, force=False):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            settings = self.store.settings()
            now = time.time()
            scheduled = os.getenv("VITOOL_SCHEDULED") == "1"
            # Cloud jobs start every 15 minutes but GitHub queues them for a varying
            # time. The grace period keeps a 15-minute setting from skipping a slot
            # when one job starts later than the next. Retry cooldowns remain exact
            # and are never bypassed by this grace period.
            schedule_grace = min(300, settings["interval_seconds"]) if scheduled else 0
            retry_until = self.store.get("retry_until", 0)
            if (self.store.get("blocked", False) or now < retry_until
                    or now < self.store.get("next_scan", 0) - schedule_grace):
                return False
            if settings["paused"] and not force:
                return False
            # Persist before I/O so restarts and manual clicks cannot bypass the request budget.
            self.store.set("next_scan", now + settings["interval_seconds"])
            self.status(scanning=True, message="Reading newest listings…", last_attempt=now)
            watched_settings = {key: settings[key] for key in (
                "brands", "sizes", "colors", "max_price", "conditions",
                "materials", "necklines", "departments", "categories",
            )}
            search_signature = json.dumps(watched_settings, sort_keys=True)
            previous_cursors = self.store.get("brand_cursors", {})
            # Vinted item IDs increase over time. The highest ID seen by the previous
            # scan separates new uploads from old listings, even after a filter change.
            watermark = self.store.get("watermark") or max(
                (item_number(c) for c in previous_cursors.values() if c), default=0)
            filters_changed = self.store.get("search_signature") != search_signature
            baseline = not self.store.get("initialized", False) or not (watermark or previous_cursors)
            if hasattr(self.source, "start_scan"):
                self.source.start_scan()
            pages = {}
            for department in settings["departments"]:
                for category in settings["categories"]:
                    for brand in settings["brands"]:
                        key = f"{department}:{category}:{brand}"
                        page_settings = {**settings, "department": department, "category": category}
                        pages[key] = self.source.catalog(page_settings, brand)
                        for item in pages[key]:
                            item["department"] = department
                            item["category"] = category
            # One Vinted item can appear in several brand pages. Its item ID is the
            # canonical identity, so merge duplicates before doing any other work.
            items = {}
            for pair in zip_longest(*pages.values()):
                for item in pair:
                    if item is not None:
                        items.setdefault(item["id"], item)
            next_cursors = {
                brand: page[0]["id"] if page else previous_cursors.get(brand)
                for brand, page in pages.items()
            }
            candidate_pages = []
            if not baseline:
                for brand, page in pages.items():
                    cursor = None if filters_changed else previous_cursors.get(brand)
                    if not cursor:
                        # A new search has no matching cursor: only uploads newer than
                        # the previous scan count as new, so old results never flood alerts.
                        candidate_pages.append([item for item in page if item_number(item["id"]) > watermark])
                        continue
                    newer = []
                    for item in page:
                        if item["id"] == cursor:
                            break
                        newer.append(item)
                    candidate_pages.append(newer)
            candidates = {}
            if candidate_pages:
                for group in zip_longest(*candidate_pages):
                    for item in group:
                        if item is not None:
                            candidates.setdefault(item["id"], item)
            # A seen ID may move above the cursor because of catalog reordering.
            # Never process or alert it twice.
            new_items = [item for item in candidates.values() if self.store.item(item["id"]) is None]
            existing_skipped = len(items) - len(new_items)
            pattern = "|".join(STYLE_PATTERNS[neckline] for neckline in settings["necklines"])
            ordered_items = sorted(new_items, key=lambda item: not bool(
                item["category"] == "pullovers" and pattern
                and re.search(pattern, item.get("title", "").lower())))
            details_used, remaining, detail_errors, matches = 0, 0, 0, 0
            rejection_reasons = {}
            for item in ordered_items:
                # The public catalog has already applied these native Vinted filters.
                item["material_filter"] = settings["materials"]
                item["catalog_colors"] = settings["colors"]
                if basic_match(item, settings) and not item.get("color_text"):
                    if details_used < 6:
                        details_used += 1
                        try:
                            item.update(self.source.details(item))
                        except ItemSourceError:
                            detail_errors += 1
                    else:
                        remaining += 1
                matched, reason = match_item(item, settings)
                self.store.save_item(item, matched, reason, baseline=baseline)
                matches += int(matched)
                if not matched:
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            self.store.set("initialized", True)
            self.store.set("search_signature", search_signature)
            self.store.set("brand_cursors", next_cursors)
            self.store.set("watermark", max([watermark, *(item_number(i) for i in items)]))
            self.store.set("failures", 0)
            self.store.set("retry_until", 0)
            # A continuously running local worker waits after completion. Scheduled
            # cloud jobs keep the start-to-start timestamp written before I/O so a
            # five-minute setting does not accidentally become ten minutes.
            if not scheduled:
                self.store.set("next_scan", time.time() + settings["interval_seconds"])
            if baseline:
                message = "Current results saved as a baseline. Future new matches can alert you."
            else:
                listing_word = "listing" if len(new_items) == 1 else "listings"
                message = (f"Scan complete. Checked {len(new_items)} new {listing_word}; "
                           f"{matches} matched your filters.")
                if rejection_reasons:
                    reasons = sorted(rejection_reasons.items(), key=lambda pair: (-pair[1], pair[0]))
                    shown = reasons[:3]
                    reason_text = "; ".join(f"{count}× {reason}" for reason, count in shown)
                    omitted = sum(count for _, count in reasons[3:])
                    if omitted:
                        reason_text += f"; {omitted}× other reasons"
                    message += " Not matched: " + reason_text + "."
                message += f" Skipped {existing_skipped} already seen."
            self.status(scanning=False, error=False, last_success=time.time(), listings=len(items),
                        new_listings=len(new_items), existing_skipped=existing_skipped, matches=matches,
                        deferred=remaining, detail_errors=detail_errors, rejection_reasons=rejection_reasons,
                        message=message
                        + (f" {remaining} candidates awaiting optional details." if remaining else "")
                        + (f" {detail_errors} unavailable detail pages skipped." if detail_errors else ""))
            # Retry only recent matches that still appear in the current catalog and satisfy current settings.
            if self.telegram.ready:
                pending = self.store.matches(pending=True)
                budget = {"reviews": 5 if self.reviewer.ready else 0}
                for item in pending:
                    if item["id"] not in items or time.time() - item["first_seen"] > 3600:
                        continue
                    if not match_item(item, settings)[0]:
                        continue
                    if not self.deliver(item, lambda i=item: self.reviewer.review(i, settings), budget):
                        break
            return True
        except SourceError as exc:
            self.failure(exc)
            return False
        except Exception:
            log.exception("Scan failed")
            self.failure(SourceError("Unexpected scan failure. See the local log."))
            return False
        finally:
            self.lock.release()

    def deliver(self, item, ask_review, budget):
        """Review (while budget lasts) and alert one pending match. False stops further alerts."""
        if "review" not in item and budget["reviews"]:
            budget["reviews"] -= 1
            try:
                item["review"] = self.save_review(item, ask_review())
            except ReviewError as exc:
                # Never lose a find: alert unreviewed and stop reviewing this scan.
                budget["reviews"] = 0
                self.status(review_error=str(exc))
            if item.get("review", {}).get("verdict") == "skip":
                return True
        try:
            self.telegram.alert(item)
        except TelegramError as exc:
            self.status(telegram_error=str(exc))
            return False
        self.store.delivered(item["id"])
        self.status(telegram_error=None)
        return True

    def save_review(self, item, review):
        data = {k: v for k, v in item.items() if k not in ("first_seen", "reason", "delivered", "baseline")}
        data["review"] = review
        if review["verdict"] == "skip":
            # Keep the listing so it is never reviewed or alerted again, but drop it from matches.
            self.store.save_item(data, False, "AI skipped: " + review["summary"])
        else:
            self.store.save_item(data, True, item["reason"])
        self.status(review_error=None)
        return review

    def scan_kleinanzeigen(self, force=False):
        """Check every saved Kleinanzeigen search once. Independent of Vinted's block state."""
        searches = self.store.get("ka_searches", [])
        if not searches or not self.lock.acquire(blocking=False):
            return False
        try:
            settings = self.store.settings()
            now = time.time()
            scheduled = os.getenv("VITOOL_SCHEDULED") == "1"
            grace = min(300, settings["interval_seconds"]) if scheduled else 0
            if (settings["paused"] and not force) or now < self.store.get("ka_retry_until", 0) \
                    or now < self.store.get("ka_next_scan", 0) - grace:
                return False
            self.store.set("ka_next_scan", now + settings["interval_seconds"])
            watermarks = self.store.get("ka_watermarks", {})
            current, new_count, baselines = set(), 0, 0
            for search in searches:
                results = self.ka_source.search(search)
                mark = watermarks.get(search["id"])
                top = max([mark or 0, *(item["ad_id"] for item in results)])
                if mark is None:
                    # First scan of a new search: remember where it stands, alert nothing old.
                    watermarks[search["id"]] = top
                    baselines += 1
                    continue
                for item in results:
                    current.add(item["id"])
                    # Paid TOP ads and bumped ads keep their old IDs, so they stay below the mark.
                    if item["ad_id"] <= mark or self.store.item(item["id"]):
                        continue
                    if search["max_price"] is not None and (item["price"] or 0) > search["max_price"]:
                        continue
                    item.update(search_id=search["id"], query=search["query"])
                    place = item["location"] + (f" ({item['distance']})" if item["distance"] else "")
                    self.store.save_item(item, True, f"🔎 {search['query']}" + (f" · {place}" if place else ""))
                    new_count += 1
                watermarks[search["id"]] = top
            self.store.set("ka_watermarks", watermarks)
            self.store.set("ka_failures", 0)
            message = f"Checked {len(searches)} Kleinanzeigen searches; {new_count} new listings."
            if baselines:
                message += f" {baselines} new searches saved their starting point."
            self.store.set("ka_status", {"message": message, "error": False, "last_success": time.time()})
            if self.telegram.ready:
                by_id = {search["id"]: search for search in searches}
                budget = {"reviews": 5 if self.reviewer.ready else 0}
                for item in self.store.matches(pending=True):
                    search = by_id.get(item.get("search_id"))
                    if (item.get("source") != "kleinanzeigen" or not search or item["id"] not in current
                            or time.time() - item["first_seen"] > 3600):
                        continue
                    if not self.deliver(item, lambda i=item, s=search: self.reviewer.review_search(i, s), budget):
                        break
            return True
        except SourceError as exc:
            failures = self.store.get("ka_failures", 0) + 1
            self.store.set("ka_failures", failures)
            delay = max(exc.delay, min(21600, 600 * 2 ** min(failures, 6)), 21600 if exc.blocked else 0)
            self.store.set("ka_retry_until", time.time() + delay)
            self.store.set("ka_status", {**self.store.get("ka_status", {}), "message": str(exc), "error": True})
            if self.telegram.ready and failures == 1:
                try:
                    self.telegram.send("Vitool: " + str(exc) + " Vinted keeps running.")
                except TelegramError:
                    pass
            return False
        except Exception:
            log.exception("Kleinanzeigen scan failed")
            self.store.set("ka_status", {**self.store.get("ka_status", {}),
                                         "message": "Unexpected Kleinanzeigen scan failure.", "error": True})
            return False
        finally:
            self.lock.release()

    def failure(self, exc):
        failures = self.store.get("failures", 0) + 1
        self.store.set("failures", failures)
        delay = max(exc.delay, min(21600, 600 * 2 ** min(failures, 6)))
        retry_until = time.time() + delay
        self.store.set("next_scan", retry_until)
        self.store.set("retry_until", retry_until)
        if exc.blocked:
            settings = self.store.settings()
            settings["paused"] = True
            self.store.save_settings(settings)
            self.store.set("blocked", True)
        self.status(scanning=False, message=str(exc), error=True)
        if self.telegram.ready and (exc.blocked or failures == 1):
            try:
                self.telegram.send("Vitool: " + str(exc))
            except TelegramError:
                self.status(telegram_error="Could not deliver the watcher error to Telegram")

    def run(self):
        while not self.stop.is_set():
            self.scan()
            self.scan_kleinanzeigen()
            self.stop.wait(5)

    def commands(self):
        while not self.stop.is_set():
            self.process_commands()
            self.stop.wait(10)

    def process_commands(self):
        if not self.telegram.ready:
            return
        try:
            updates = self.telegram.updates(self.store.get("telegram_offset", 0))
            for update in updates:
                message = update.get("message", {})
                # Persist consumption before executing commands to avoid repeats after a crash.
                self.store.set("telegram_offset", update["update_id"] + 1)
                if str(message.get("chat", {}).get("id")) != self.telegram.chat_id:
                    continue
                if not message.get("text", "").startswith("/"):
                    continue
                try:
                    reply = handle_command(message["text"], self.store)
                except (ValueError, TypeError):
                    reply = "Invalid value. Use /help for supported settings."
                self.telegram.send(reply)
            self.status(telegram_command_error=None)
        except TelegramError as exc:
            self.status(telegram_command_error=str(exc))
