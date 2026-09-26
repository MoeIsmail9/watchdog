import json
import logging
import re
import threading
import time
from itertools import zip_longest

from .matching import STYLE_PATTERNS, basic_match, match_item
from .source import ItemSourceError, SourceError, VintedSource
from .telegram import Telegram, TelegramError, handle_command

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, source=None, telegram=None):
        self.store = store
        self.source = source or VintedSource()
        self.telegram = telegram or Telegram()
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
            if self.store.get("blocked", False) or now < self.store.get("next_scan", 0):
                return False
            if settings["paused"] and not force:
                return False
            # Persist before I/O so restarts and manual clicks cannot bypass the request budget.
            self.store.set("next_scan", now + settings["interval_seconds"])
            self.status(scanning=True, message="Reading newest listings…", last_attempt=now)
            watched_settings = {key: settings[key] for key in (
                "brands", "sizes", "colors", "max_price", "conditions",
                "materials", "necklines", "departments",
            )}
            search_signature = json.dumps(watched_settings, sort_keys=True)
            baseline = (not self.store.get("initialized", False)
                        or self.store.get("search_signature") != search_signature
                        or not self.store.get("brand_cursors", {}))
            pages = {}
            for department in settings["departments"]:
                for brand in settings["brands"]:
                    key = f"{department}:{brand}"
                    page_settings = {**settings, "department": department}
                    pages[key] = self.source.catalog(page_settings, brand)
                    for item in pages[key]:
                        item["department"] = department
            # One Vinted item can appear in several brand pages. Its item ID is the
            # canonical identity, so merge duplicates before doing any other work.
            items = {i["id"]: i for pair in zip_longest(*pages.values()) for i in pair if i is not None}
            previous_cursors = self.store.get("brand_cursors", {})
            next_cursors = {
                brand: page[0]["id"] if page else previous_cursors.get(brand)
                for brand, page in pages.items()
            }
            candidate_pages = []
            if not baseline:
                for brand, page in pages.items():
                    cursor = previous_cursors.get(brand)
                    if not cursor:
                        candidate_pages.append([])
                        continue
                    newer = []
                    for item in page:
                        if item["id"] == cursor:
                            break
                        newer.append(item)
                    candidate_pages.append(newer)
            candidates = {
                item["id"]: item
                for group in zip_longest(*candidate_pages)
                for item in group if item is not None
            } if candidate_pages else {}
            # A seen ID may move above the cursor because of catalog reordering.
            # Never process or alert it twice.
            new_items = [item for item in candidates.values() if self.store.item(item["id"]) is None]
            existing_skipped = len(items) - len(new_items)
            pattern = "|".join(STYLE_PATTERNS[neckline] for neckline in settings["necklines"])
            ordered_items = sorted(new_items, key=lambda item: not bool(
                pattern and re.search(pattern, item.get("title", "").lower())))
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
            self.store.set("failures", 0)
            # Wait the configured interval after a completed scan. This prevents a
            # long scan from immediately starting another cycle.
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
                for item in pending:
                    if item["id"] not in items or time.time() - item["first_seen"] > 3600:
                        continue
                    if not match_item(item, settings)[0]:
                        continue
                    try:
                        self.telegram.alert(item)
                    except TelegramError as exc:
                        self.status(telegram_error=str(exc))
                        break
                    self.store.delivered(item["id"])
                    self.status(telegram_error=None)
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

    def failure(self, exc):
        failures = self.store.get("failures", 0) + 1
        self.store.set("failures", failures)
        delay = max(exc.delay, min(21600, 600 * 2 ** min(failures, 6)))
        self.store.set("next_scan", time.time() + delay)
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
