"""
Shared pipeline for the government-tender scraper.

Everything source-agnostic lives here so the GeM / CPPP / GePNIC modules only
have to worry about fetching + parsing their own pages and emitting normalized
tender dicts:

    - HTTP with retries (exponential backoff) + polite rate limiting
    - 60-worker concurrency (asyncio.Semaphore), shared across all sources
    - Checkpointing every N rows: atomic CSV rewrite (temp file -> rename) plus a
      per-source JSON checkpoint of which listing pages are already done
    - Live progress with rolling-average ETA
    - Deduplication on (source, tender_id), keeping the latest corrigendum
    - A separate errors_log.csv for anything that failed to parse
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import re
import time
from datetime import datetime
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from keywords import is_empanelment

import aiohttp

# Normalized output schema. One row per tender, every source maps into this.
FIELDS = [
    "tender_id",
    "title",
    "description",
    "organization",
    "state",
    "published_date",
    "closing_date",
    "source",        # gem / cppp / <state name>
    "source_url",
    "corrigendum",   # marker used for keep-latest dedup; blank if none
    "matched_keywords",  # which target keywords this tender matched (if filtering)
    "is_empanelment",    # "1" if empanelment/EOI/rate-contract, else ""
]

ERROR_FIELDS = ["source", "tender_id", "reason"]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class ScraperConfig:
    """Tunables shared by every source module."""
    concurrency: int = 60           # asyncio.Semaphore workers
    max_retries: int = 3            # retries per request on network error
    backoff_base: float = 0.5       # exponential backoff base (seconds)
    rate_limit: float = 0.25        # polite delay between requests (~0.2-0.3s)
    checkpoint_every: int = 20      # flush CSV + checkpoints every N rows
    request_timeout: float = 45.0
    out_dir: str = "outputs"
    output_csv: str = "tenders_gem_cppp.csv"
    errors_csv: str = "errors_log.csv"
    resume: bool = True             # skip pages already in the checkpoint

    @property
    def output_path(self) -> str:
        return os.path.join(self.out_dir, self.output_csv)

    @property
    def errors_path(self) -> str:
        return os.path.join(self.out_dir, self.errors_csv)

    @property
    def seen_ids_path(self) -> str:
        return os.path.join(self.out_dir, "seen_ids.json")

    def checkpoint_path(self, source: str) -> str:
        # Sanitize: source may embed a keyword (e.g. "gem:mobile app") and ':'
        # / spaces are illegal or awkward in Windows filenames.
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source)
        return os.path.join(self.out_dir, f"{safe}_checkpoint.json")


class RetryableError(Exception):
    """Raised internally so callers can distinguish transient network failures."""


async def fetch(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    config: ScraperConfig,
    *,
    expect: str = "text",   # "text" or "json"
    **kwargs: Any,
) -> Any:
    """
    Fetch a URL with exponential-backoff retries and a polite delay.

    Raises RetryableError only after `max_retries` consecutive failures so the
    caller can log-and-skip that single unit of work without crashing the run.
    """
    last_exc: Exception | None = None
    for attempt in range(config.max_retries + 1):
        try:
            async with session.request(
                method, url,
                timeout=aiohttp.ClientTimeout(total=config.request_timeout),
                **kwargs,
            ) as resp:
                # 5xx / 429 are worth retrying; other 4xx are not.
                if resp.status in (429, 500, 502, 503, 504):
                    raise RetryableError(f"HTTP {resp.status} for {url}")
                resp.raise_for_status()
                if expect == "json":
                    # Some .gov.in endpoints send JSON as text/html.
                    text = await resp.text()
                    return json.loads(text)
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, RetryableError,
                json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < config.max_retries:
                # exponential backoff with jitter
                delay = config.backoff_base * (2 ** attempt) + random.uniform(0, 0.3)
                await asyncio.sleep(delay)
            else:
                raise RetryableError(f"{url} failed after "
                                     f"{config.max_retries} retries: {exc!r}") from last_exc
        finally:
            # Rate limit every attempt so retries also stay polite.
            await asyncio.sleep(config.rate_limit)


class Progress:
    """Live progress printer with rolling-average ETA (not single-sample)."""

    def __init__(self, window: int = 15):
        self._batch_durations: deque[tuple[int, float]] = deque(maxlen=window)
        self._total = 0
        self._done = 0
        self._last_tick = time.monotonic()

    def set_total(self, total: int) -> None:
        # Each source contributes its own estimate; totals accumulate so the
        # combined "X / Y" reflects all sources in one run.
        self._total += total

    def add_done(self, n: int) -> None:
        self._done += n

    def report(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_tick
        self._last_tick = now
        # record (items, seconds) for this batch
        self._batch_durations.append((max(self._done_delta(), 1), elapsed))
        # rolling average seconds-per-item over the window
        items = sum(i for i, _ in self._batch_durations)
        secs = sum(s for _, s in self._batch_durations)
        rate = (secs / items) if items else 0.0
        remaining = max(self._total - self._done, 0)
        eta = remaining * rate
        m, sdisp = divmod(int(eta), 60)
        total_disp = self._total if self._total else "?"
        print(f"  Processed {self._done} / {total_disp} tenders | "
              f"ETA: {m}m {sdisp}s", flush=True)

    _prev_done = 0

    def _done_delta(self) -> int:
        delta = self._done - self._prev_done
        self._prev_done = self._done
        return delta


class ResultSink:
    """
    Collects normalized tenders from every source, dedupes them, and handles
    checkpointing / progress / error logging. Source modules only call:
        sink.set_total(n)
        sink.emit(tender_dict)
        sink.emit_error(source, tender_id, reason)
        sink.save_page(source, page)          # mark a listing page complete
        sink.is_page_done(source, page)       # for resume
    """

    def __init__(self, config: ScraperConfig, matcher=None):
        self.config = config
        # Optional KeywordMatcher; when set, tenders from FILTERED_SOURCES that
        # match no keyword are dropped (silently, not counted as parse errors).
        # Other sources bypass the filter entirely.
        self.matcher = matcher
        self.filtered_out = 0
        self.scanned = 0
        os.makedirs(config.out_dir, exist_ok=True)
        self.progress = Progress()
        # dedup store keyed by (source, tender_id) -> row (only tenders that
        # PASSED the keyword filter, i.e. what ends up in the output CSV)
        self._rows: dict[tuple[str, str], dict[str, str]] = {}
        # every (source, tender_id) EVER scanned, whether kept or filtered out.
        # This is what is_known()/early-stop must use: a filtered-out tender is
        # still "already seen" and must not force a full rescan on every run.
        self._seen: set[tuple[str, str]] = set()
        self._errors: list[dict[str, str]] = []
        self._since_flush = 0
        # per-source checkpoints of completed pages
        self._checkpoints: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        # On resume, preload prior results so skipped pages aren't lost when we
        # atomically rewrite the CSV at the next flush.
        if config.resume:
            self._preload_existing()

    def _preload_existing(self) -> None:
        if os.path.exists(self.config.output_path):
            try:
                with open(self.config.output_path, encoding="utf-8-sig", newline="") as fh:
                    for row in csv.DictReader(fh):
                        key = (row.get("source", ""), row.get("tender_id", ""))
                        if key[1]:
                            self._rows[key] = {k: row.get(k, "") for k in FIELDS}
                            self._seen.add(key)
            except (OSError, csv.Error):
                pass
        if os.path.exists(self.config.errors_path):
            try:
                with open(self.config.errors_path, encoding="utf-8-sig", newline="") as fh:
                    self._errors = [dict(r) for r in csv.DictReader(fh)]
            except (OSError, csv.Error):
                pass
        if os.path.exists(self.config.seen_ids_path):
            try:
                with open(self.config.seen_ids_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for source, ids in data.items():
                    self._seen.update((source, tid) for tid in ids)
            except (OSError, json.JSONDecodeError):
                pass
        if self._rows:
            print(f"  [resume] loaded {len(self._rows)} existing tenders "
                  f"from {self.config.output_path}")
        if self._seen:
            print(f"  [resume] {len(self._seen)} tender IDs known from prior "
                  f"runs (kept + filtered-out) for incremental early-stop")

    def _write_seen_ids(self) -> None:
        by_source: dict[str, list[str]] = {}
        for source, tid in self._seen:
            by_source.setdefault(source, []).append(tid)
        _atomic_write_json(self.config.seen_ids_path, by_source)

    # ---- checkpoint / resume -------------------------------------------------
    def _load_checkpoint(self, source: str) -> dict[str, Any]:
        if source not in self._checkpoints:
            path = self.config.checkpoint_path(source)
            if self.config.resume and os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    data = {}
            else:
                data = {}
            data.setdefault("completed_pages", [])
            data.setdefault("last_page", 0)
            # store as set internally for O(1) lookups
            data["_completed_set"] = set(data["completed_pages"])
            self._checkpoints[source] = data
        return self._checkpoints[source]

    def is_page_done(self, source: str, page: int) -> bool:
        return page in self._load_checkpoint(source)["_completed_set"]

    async def save_page(self, source: str, page: int) -> None:
        async with self._lock:
            cp = self._load_checkpoint(source)
            cp["_completed_set"].add(page)
            cp["last_page"] = max(cp["last_page"], page)

    def _write_checkpoints(self) -> None:
        for source, cp in self._checkpoints.items():
            out = {
                "last_page": cp["last_page"],
                "completed_pages": sorted(cp["_completed_set"]),
            }
            _atomic_write_json(self.config.checkpoint_path(source), out)

    # ---- results -------------------------------------------------------------
    def set_total(self, total: int) -> None:
        self.progress.set_total(total)

    def is_known(self, source: str, tender_id: str) -> bool:
        """
        True if this tender was already SCANNED in a prior run — whether it was
        kept or dropped by the keyword filter. Sources whose listings are
        sorted newest-first can use this to stop paginating as soon as a page
        comes back all-known, turning a repeat run into a fetch of just the new
        tenders instead of a full rescan. (Checking self._rows alone would be
        wrong here: a filtered-out tender never lands in self._rows, so it
        would look "new" forever and defeat early-stop entirely.)

        `tender_id` is normalized the same way emit() cleans it before storing,
        so callers can pass the raw just-parsed value directly.
        """
        return (source, _clean(tender_id)) in self._seen

    @staticmethod
    def _is_newer(new: dict[str, str], existing: dict[str, str]) -> bool:
        """Keep-latest rule for corrigendums / re-listings."""
        # An explicit corrigendum marker wins.
        if new.get("corrigendum") and not existing.get("corrigendum"):
            return True
        # Otherwise prefer the later published date (lexicographic on ISO works;
        # fall back to closing date, then to "keep existing").
        for key in ("published_date", "closing_date"):
            n, e = new.get(key, ""), existing.get(key, "")
            if n and e and n != e:
                return n > e
        return False

    async def emit(self, tender: dict[str, Any]) -> None:
        row = {k: _clean(tender.get(k, "")) for k in FIELDS}
        key = (row["source"], row["tender_id"])
        if row["tender_id"]:
            # Record as "seen" regardless of what happens next, so a
            # filtered-out tender doesn't look "new" again on the next run.
            self._seen.add(key)
        # Empanelments are ALWAYS kept (the user wants every empanelment),
        # regardless of IT keywords. Flag them so the dashboard can show them
        # in the Empanelments tab without polluting the IT-only tender tabs.
        emp = is_empanelment(row["title"], row["description"])
        row["is_empanelment"] = "1" if emp else ""
        # Keyword filter (if enabled) — applied to EVERY source. Keep a tender
        # if it matches an IT/AI keyword OR is an empanelment.
        if self.matcher is not None:
            hits = self.matcher.match(row["title"], row["description"],
                                      row["organization"])
            self.scanned += 1
            if not hits and not emp:
                self.filtered_out += 1
                self.progress.add_done(1)  # count scanned-but-dropped as progress
                return
            row["matched_keywords"] = "; ".join(hits)
        async with self._lock:
            if not row["tender_id"]:
                self._errors.append({
                    "source": row["source"], "tender_id": "",
                    "reason": "empty tender_id after parse",
                })
                return
            existing = self._rows.get(key)
            if existing is None or self._is_newer(row, existing):
                self._rows[key] = row
            self._since_flush += 1
            self.progress.add_done(1)
            if self._since_flush >= self.config.checkpoint_every:
                self._flush_locked()

    async def emit_error(self, source: str, tender_id: str, reason: str) -> None:
        async with self._lock:
            self._errors.append({
                "source": source, "tender_id": str(tender_id or ""),
                "reason": reason,
            })

    # ---- flushing ------------------------------------------------------------
    def _flush_locked(self) -> None:
        """Assumes self._lock is held."""
        sorted_rows = sorted(self._rows.values(), key=_closing_date_sort_key,
                            reverse=True)
        _atomic_write_csv(self.config.output_path, FIELDS, sorted_rows)
        if self._errors:
            _atomic_write_csv(self.config.errors_path, ERROR_FIELDS, self._errors)
        self._write_checkpoints()
        self._write_seen_ids()
        self._since_flush = 0
        self.progress.report()

    async def flush(self) -> None:
        async with self._lock:
            self._flush_locked()

    async def finalize(self) -> None:
        async with self._lock:
            self._flush_locked()
        if self.matcher is not None:
            print(f"\nKeyword filter: {self.scanned} scanned, "
                  f"{self.filtered_out} dropped (no keyword match).")
        print(f"\nDone. {len(self._rows)} unique tenders -> "
              f"{self.config.output_path}")
        if self._errors:
            print(f"      {len(self._errors)} parse errors -> "
                  f"{self.config.errors_path}")


# ---- atomic file helpers ----------------------------------------------------
def _replace_with_retry(tmp: str, path: str, attempts: int = 6) -> None:
    """
    os.replace() with retries for transient Windows file locks.

    Cloud-synced folders (OneDrive, etc.) can briefly hold an exclusive handle
    on a just-written file while indexing/uploading it, which makes
    os.replace() raise PermissionError (WinError 5) even though nothing in
    this process is holding the file open. That's transient (milliseconds to
    a couple seconds), so retry with backoff instead of crashing the whole run.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (2 ** attempt) + random.uniform(0, 0.1))


def _atomic_write_csv(path: str, fieldnames: list[str],
                      rows: Iterable[dict[str, Any]]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    _replace_with_retry(tmp, path)  # atomic on the same filesystem


def _atomic_write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    _replace_with_retry(tmp, path)


# Closing-date formats seen across sources: GeM's ISO timestamp and CPPP's
# "DD-Mon-YYYY hh:mm AM/PM". Add new formats here as new sources are wired in.
_CLOSING_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%SZ",   # GeM, e.g. 2025-06-28T19:00:00Z
    "%d-%b-%Y %I:%M %p",    # CPPP, e.g. 08-Jul-2026 11:00 AM
]


def _parse_closing_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _CLOSING_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _closing_date_sort_key(row: dict[str, str]) -> datetime:
    # Rows with an unparseable/missing closing date sort last even in
    # descending order, since datetime.min is the smallest possible key.
    return _parse_closing_date(row.get("closing_date", "")) or datetime.min


def _clean(value: Any) -> str:
    """Normalize a scalar into a single clean CSV cell."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return " ".join(str(value).split()).strip()


def make_session(config: ScraperConfig) -> aiohttp.ClientSession:
    """A shared session: capped connector + default headers."""
    connector = aiohttp.TCPConnector(limit=config.concurrency, ssl=False)
    return aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": DEFAULT_UA,
                 "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8"},
    )
