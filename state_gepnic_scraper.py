"""
Module 3 - State GePNIC eProcurement portals (STUB + one working state).

Most Indian states run the same NIC "GePNIC" software, so their listing pages
share an almost-identical HTML structure. This module provides ONE generic
parser plus a config registry, so a new state can be switched on in a single
line once you've *manually confirmed its listing page is CAPTCHA-free*.

IMPORTANT SAFETY GATE
---------------------
A state is only scraped at full concurrency when its config has
    "captcha_confirmed_free": True
Until you set that (after eyeballing the portal yourself), the state can only be
run with --dry-run, which fetches a single page and prints the raw structure so
you can decide whether the generic parser needs a state-specific tweak.

We never solve or bypass CAPTCHAs. If a page shows a captcha field, the scraper
flags it and stops for that state.

Pagination-fix logic (ported from the find_company_names.py-style scraper):
GePNIC pagers often keep a "Next" link visible on the LAST page, only marking
its parent <li>/<a> as disabled, and sometimes just re-serve the final page.
So we stop when EITHER
    (a) the Next control's parent element is disabled, OR
    (b) the page's content fingerprint matches the previous page.
"""

from __future__ import annotations

import asyncio
import hashlib
import re

from bs4 import BeautifulSoup

from pipeline import ResultSink, ScraperConfig, fetch

# ---------------------------------------------------------------------------
# Registry. Add a state here (or via add_state_portal) once confirmed free.
#   base_url         : portal root
#   listing_endpoint : path template with {page}, relative to base_url
#   captcha_confirmed_free : True (scrape), None/False (dry-run only)
# ---------------------------------------------------------------------------
STATE_PORTALS: dict[str, dict] = {
    # The one fully-wired example. captcha_confirmed_free is left None because
    # it was NOT verifiable during recon (portal returned 404); confirm the
    # correct live listing URL + CAPTCHA-free status manually, then flip to True.
    "telangana": {
        "base_url": "https://tender.telangana.gov.in",
        "listing_endpoint": "/nicgep/app?page=FrontEndLatestActiveTenders"
                            "&service=direct&session=T&sp=S{page}",
        "captcha_confirmed_free": None,
    },
    # "karnataka": {"base_url": "...", "listing_endpoint": "...",
    #               "captcha_confirmed_free": None},
}


def add_state_portal(state_name: str, base_url: str, listing_endpoint: str,
                     captcha_confirmed_free: bool | None = None) -> None:
    """
    Register a new GePNIC state in one line, reusing the generic parser below.

        add_state_portal("kerala",
                         "https://etenders.kerala.gov.in",
                         "/nicgep/app?page=FrontEndLatestActiveTenders"
                         "&service=direct&session=T&sp=S{page}")

    Leave captcha_confirmed_free as None and use --dry-run first; only set True
    after you've manually confirmed the listing renders without a CAPTCHA.
    """
    STATE_PORTALS[state_name.lower()] = {
        "base_url": base_url.rstrip("/"),
        "listing_endpoint": listing_endpoint,
        "captcha_confirmed_free": captcha_confirmed_free,
    }


# ---------------------------------------------------------------------------
# Generic GePNIC parsing (shared by every state)
# ---------------------------------------------------------------------------
_TITLE_COL_RE = re.compile(r"Title.*(Ref|Tender)", re.I)


def _looks_captcha_gated(html: str) -> bool:
    return 'name="captchaText"' in html or "Provide Captcha" in html


def _find_listing_table(soup: BeautifulSoup):
    for t in soup.find_all("table"):
        if _TITLE_COL_RE.search(t.get_text()):
            return t
    return None


def _fingerprint(table) -> str:
    """Hash of the row texts, used to detect a re-served last page."""
    text = "|".join(r.get_text(" ", strip=True) for r in table.find_all("tr")[1:])
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()


def _next_is_disabled(soup: BeautifulSoup) -> bool:
    """
    GePNIC keeps a 'Next' link on the last page but disables its parent.
    Return True when there's no live Next control to follow.
    """
    for a in soup.find_all("a"):
        if re.fullmatch(r"\s*next\s*(?:»|>|&raquo;)?\s*", a.get_text(strip=True), re.I):
            parent = a.parent
            classes = " ".join(
                (a.get("class") or []) + (parent.get("class") or [] if parent else [])
            ).lower()
            disabled = ("disabled" in classes
                        or a.get("aria-disabled") == "true"
                        or not a.get("href"))
            return disabled
    return True  # no Next control at all -> treat as last page


def _parse_row(row, base_url: str, state: str) -> dict | None:
    cells = row.find_all("td")
    if len(cells) < 5:
        return None
    # Standard GePNIC latest-active-tenders column order:
    #   Sl.No | e-Published Date | Closing Date | Opening Date | Title/Ref/Id | Org
    link = cells[4].find("a", href=True) if len(cells) > 4 else None
    href = link["href"] if link else ""
    if href and href.startswith("/"):
        href = base_url + href
    raw_title = cells[4].get_text(" ", strip=True) if len(cells) > 4 else ""
    title = re.split(r"\s+/", raw_title, maxsplit=1)[0].strip() or raw_title
    tender_id = raw_title.rsplit("/", 1)[-1].strip() if "/" in raw_title else raw_title
    org = cells[5].get_text(" ", strip=True) if len(cells) > 5 else ""
    return {
        "tender_id": tender_id,
        "title": title,
        "description": raw_title,
        "organization": org,
        "state": state,
        "published_date": cells[1].get_text(" ", strip=True),
        "closing_date": cells[2].get_text(" ", strip=True),
        "source": state,
        "source_url": href or base_url,
        "corrigendum": "",
    }


# ---------------------------------------------------------------------------
# Dry-run: fetch one page, print structure, parse nothing into the output.
# ---------------------------------------------------------------------------
async def dry_run_state(session, config: ScraperConfig, state: str) -> None:
    state = state.lower()
    cfg = STATE_PORTALS.get(state)
    if not cfg:
        print(f"[{state}] not registered. Use add_state_portal(...) first.")
        return
    url = cfg["base_url"] + cfg["listing_endpoint"].format(page=1)
    print(f"[{state}] DRY RUN -> GET {url}")
    try:
        html = await fetch(session, "GET", url, config, expect="text")
    except Exception as exc:  # noqa: BLE001
        print(f"[{state}] fetch failed: {exc!r}")
        return

    print(f"[{state}] status OK, {len(html):,} bytes")
    if _looks_captcha_gated(html):
        print(f"[{state}] ⚠️  CAPTCHA detected on the listing page. "
              f"Do NOT enable this state. Leaving captcha_confirmed_free unset.")
        return

    soup = BeautifulSoup(html, "html.parser")
    table = _find_listing_table(soup)
    if table is None:
        print(f"[{state}] No Title/Ref/Tender table found. The generic parser "
              f"likely needs a state-specific tweak. Tables on page: "
              f"{len(soup.find_all('table'))}")
        return
    rows = table.find_all("tr")
    print(f"[{state}] Found listing table with {len(rows)} rows.")
    print(f"[{state}] Header: "
          f"{rows[0].get_text(' | ', strip=True)[:200] if rows else '(none)'}")
    for r in rows[1:4]:
        parsed = _parse_row(r, cfg["base_url"], state)
        print(f"[{state}] sample -> {parsed}")
    print(f"[{state}] Next-control disabled? {_next_is_disabled(soup)}")
    print(f"[{state}] If the samples look right, set "
          f"captcha_confirmed_free=True to enable full scraping.")


# ---------------------------------------------------------------------------
# Full scrape (only for confirmed states). Sequential paging because GePNIC
# doesn't expose a total count; we page until the fix-logic says "last page".
# ---------------------------------------------------------------------------
async def scrape_state(session, config: ScraperConfig, sink: ResultSink,
                       state: str, max_pages: int | None = None) -> None:
    state = state.lower()
    cfg = STATE_PORTALS.get(state)
    if not cfg:
        print(f"[{state}] not registered; skipping.")
        return
    if cfg.get("captcha_confirmed_free") is not True:
        print(f"[{state}] SKIPPED: captcha_confirmed_free is not True. "
              f"Run with --dry-run and confirm the listing is CAPTCHA-free first.")
        return

    base = cfg["base_url"]
    prev_fp: str | None = None
    page = 1
    while True:
        if max_pages and page > max_pages:
            break
        url = base + cfg["listing_endpoint"].format(page=page)
        try:
            html = await fetch(session, "GET", url, config, expect="text")
        except Exception as exc:  # noqa: BLE001
            await sink.emit_error(state, f"page:{page}", f"fetch failed: {exc!r}")
            break

        if _looks_captcha_gated(html):
            await sink.emit_error(state, f"page:{page}", "captcha appeared; stopping")
            print(f"[{state}] CAPTCHA appeared at page {page}; stopping this state.")
            break

        soup = BeautifulSoup(html, "html.parser")
        table = _find_listing_table(soup)
        if table is None:
            await sink.emit_error(state, f"page:{page}", "listing table not found")
            break

        # Pagination fix: stop on a re-served page (fingerprint match).
        fp = _fingerprint(table)
        if fp == prev_fp:
            break
        prev_fp = fp

        for row in table.find_all("tr")[1:]:
            try:
                tender = _parse_row(row, base, state)
                if tender:
                    await sink.emit(tender)
            except Exception as exc:  # noqa: BLE001
                await sink.emit_error(state, "unknown", f"row parse failed: {exc!r}")

        # Or stop when the Next control is disabled.
        if _next_is_disabled(soup):
            break
        page += 1

    await sink.flush()
    print(f"[{state}] done.")
