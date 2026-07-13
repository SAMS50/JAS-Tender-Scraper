"""
Module 1 - GeM (Government e-Marketplace) bid scraper.

Recon findings (bidplus.gem.gov.in/all-bids):
  * The listing is fully viewable with NO CAPTCHA.
  * Bids are loaded via XHR, not server-rendered HTML:
        POST https://bidplus.gem.gov.in/all-bids-data
        form data: payload=<json>, csrf_bd_gem_nk=<token>
  * The CSRF token is embedded in the /all-bids page HTML and must be paired
    with the session cookie from that same GET.
  * Response is Solr-style JSON:
        response.response.docs[]   (10 bids per page)
        response.response.numFound (total bids available)

We parse the JSON directly (far more reliable than scraping the rendered cards).
"""

from __future__ import annotations

import asyncio
import json
import math
import re

import aiohttp

from pipeline import ResultSink, ScraperConfig, fetch

BASE = "https://bidplus.gem.gov.in"
LISTING_PAGE = f"{BASE}/all-bids"
DATA_ENDPOINT = f"{BASE}/all-bids-data"
PER_PAGE = 10  # the endpoint returns 10 docs/page (totalPage = numFound/10)

# The all-bids-data endpoint accepts a `filter` object (which the page sends but
# we originally didn't). bidStatusType="ongoing_bids" restricts to currently-OPEN
# bids only — collapsing the 5.5M all-time archive to ~47k open bids, and each
# keyword's hits from tens-of-thousands to hundreds. That makes a complete,
# uncapped per-keyword scan feasible, so no open keyword tender is missed.
ONGOING_FILTER = {
    "bidStatusType": "ongoing_bids",
    "byType": "all",
    "highBidValue": "",
    "byEndDate": {"from": "", "to": ""},
    "sort": "Bid-End-Date-Latest",
}

_CSRF_RE = re.compile(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([0-9a-fA-F]+)['\"]")


async def _get_csrf(session: aiohttp.ClientSession, config: ScraperConfig) -> str:
    """Fetch the listing page to obtain the CSRF token + session cookie."""
    html = await fetch(session, "GET", LISTING_PAGE, config, expect="text")
    m = _CSRF_RE.search(html)
    if not m:
        raise RuntimeError("Could not find csrf_bd_gem_nk on GeM /all-bids page")
    return m.group(1)


def _first(doc: dict, key: str, default: str = "") -> str:
    """GeM Solr docs wrap every value in a list."""
    val = doc.get(key, default)
    if isinstance(val, list):
        return str(val[0]) if val else default
    return str(val) if val is not None else default


def _gem_tender_type(doc: dict) -> str:
    """
    GeM structurally splits the marketplace into Products (goods catalog) and
    Services, and each bid doc carries that split twice: b_type (0 = goods,
    1 = services) and b_cat_id (services categories are namespaced
    "services_*"). Verified live (2026-07-09): perfect agreement between the
    two over a 200-doc sample spanning goods and services searches. This is
    the portal's own category — extracted, not guessed from listing text.
    """
    b_type = _first(doc, "b_type")
    if b_type == "1":
        return "Service"
    if b_type == "0":
        return "Product"
    # Unexpected/missing b_type: hand the raw category id to the pipeline's
    # normalizer ("services_*" still yields Service; anything else is
    # Unidentified).
    return _first(doc, "b_cat_id")


def _parse_doc(doc: dict) -> dict:
    """Map a GeM Solr doc into the normalized tender schema."""
    b_id = _first(doc, "b_id")
    bid_number = _first(doc, "b_bid_number")
    # b_category_name is the short title; bd_category_name is the fuller text.
    title = _first(doc, "b_category_name")
    description = _first(doc, "bd_category_name") or title
    ministry = _first(doc, "ba_official_details_minName")
    dept = _first(doc, "ba_official_details_deptName")
    org = " / ".join(p for p in (ministry, dept) if p and p != "NA")
    return {
        "tender_id": bid_number or b_id,
        "title": title,
        "description": description,
        "organization": org,
        "state": "",  # GeM is national; no per-bid state in the listing
        "published_date": _first(doc, "final_start_date_sort"),
        "closing_date": _first(doc, "final_end_date_sort"),
        "source": "gem",
        # public bid document page for this bid
        "source_url": f"{BASE}/showbidDocument/{b_id}" if b_id else LISTING_PAGE,
        "corrigendum": "",
        "tender_type": _gem_tender_type(doc),
    }


async def _scrape_page(session, config, sink, csrf, page, search_term=""):
    """
    Fetch and parse a single listing page of 10 bids. Every page is fetched
    fresh on every run: GeM's "ongoing bids" result set is not append-only —
    bids open and close daily, so a page that was fully scraped last run can
    have entirely different content today. Each keyword's ongoing set is
    already small (the bidStatusType=ongoing_bids filter keeps it to
    tens-to-hundreds of hits), so a full fresh scan per keyword per run is
    cheap and is the only way to catch newly opened/closed bids.
    """
    postdata = {"page": page,
                "param": {"searchBid": search_term, "searchType": "fullText"},
                "filter": ONGOING_FILTER}
    try:
        data = await fetch(
            session, "POST", DATA_ENDPOINT, config, expect="json",
            data={"payload": json.dumps(postdata), "csrf_bd_gem_nk": csrf},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": LISTING_PAGE},
        )
    except Exception as exc:  # noqa: BLE001 - log and skip the whole page
        await sink.emit_error("gem", f"page:{page}", f"fetch failed: {exc!r}")
        return

    docs = (data.get("response", {}) or {}).get("response", {}).get("docs", [])
    for doc in docs:
        try:
            await sink.emit(_parse_doc(doc))
        except Exception as exc:  # noqa: BLE001
            await sink.emit_error("gem", _first(doc, "b_bid_number") or _first(doc, "b_id"),
                                  f"parse failed: {exc!r}")


async def scrape_gem(session, config: ScraperConfig, sink: ResultSink,
                     max_pages: int | None = None) -> None:
    """
    Entry point. Discovers the total number of pages, then fans out page
    fetches across the shared concurrency semaphore.
    """
    print("[GeM] fetching CSRF token / session ...")
    csrf = await _get_csrf(session, config)

    # Discover total via page 1's numFound.
    postdata = {"page": 1, "param": {"searchBid": "", "searchType": "fullText"}}
    probe = await fetch(
        session, "POST", DATA_ENDPOINT, config, expect="json",
        data={"payload": json.dumps(postdata), "csrf_bd_gem_nk": csrf},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": LISTING_PAGE},
    )
    num_found = (probe.get("response", {}) or {}).get("response", {}).get("numFound", 0)
    total_pages = math.ceil(num_found / PER_PAGE) if num_found else 1
    if max_pages:
        total_pages = min(total_pages, max_pages)
    print(f"[GeM] numFound={num_found:,} -> scraping {total_pages:,} pages "
          f"({total_pages * PER_PAGE:,} bids)")
    sink.set_total(total_pages * PER_PAGE)

    sem = asyncio.Semaphore(config.concurrency)

    async def worker(page: int) -> None:
        async with sem:
            await _scrape_page(session, config, sink, csrf, page)

    await asyncio.gather(*(worker(p) for p in range(1, total_pages + 1)))
    await sink.flush()
    print("[GeM] done.")


async def _numfound(session, config, csrf, term):
    postdata = {"page": 1,
                "param": {"searchBid": term, "searchType": "fullText"},
                "filter": ONGOING_FILTER}
    data = await fetch(
        session, "POST", DATA_ENDPOINT, config, expect="json",
        data={"payload": json.dumps(postdata), "csrf_bd_gem_nk": csrf},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": LISTING_PAGE},
    )
    return (data.get("response", {}) or {}).get("response", {}).get("numFound", 0)


async def scrape_gem_keywords(session, config: ScraperConfig, sink: ResultSink,
                              keywords: list[str],
                              pages_per_keyword: int = 2000) -> None:
    """
    Keyword-targeted GeM scrape over ONGOING (open) bids only.

    Because we send bidStatusType="ongoing_bids", each keyword returns only its
    currently-open bids — hundreds, not tens-of-thousands — so we scan EVERY
    page (no artificial cap) and let the KeywordMatcher keep the true matches.
    GeM's token-OR search is a superset of real phrase matches, so scanning the
    complete ongoing set per keyword means no open keyword tender is missed.
    `pages_per_keyword` is only a runaway safety backstop.
    """
    print("[GeM] fetching CSRF token / session ...")
    csrf = await _get_csrf(session, config)
    # GeM is search-based (not full-scan), so empanelments with no IT keyword
    # would never be fetched. Add empanelment search terms so GeM empanelments
    # are pulled too (the sink keeps them via its empanelment exemption). We omit
    # "rate contract" here — on GeM that's a goods-procurement mode (~600 bids),
    # not vendor panels; genuine ones still get flagged if an IT search returns them.
    search_terms = list(keywords) + ["empanelment", "expression of interest",
                                      "enlistment"]
    print(f"[GeM] ONGOING bids only: {len(keywords)} keywords + empanelment terms")

    sem = asyncio.Semaphore(config.concurrency)

    async def worker(term, page):
        async with sem:
            await _scrape_page(session, config, sink, csrf, page, search_term=term)

    for term in search_terms:
        # GeM's search endpoint 404s on hyphens (e.g. "AI-based"); use a spaced
        # query for the server call. The client-side matcher still catches both
        # "AI-based" and "AI based" forms in the returned docs.
        query = term.replace("-", " ").strip()
        try:
            nf = await _numfound(session, config, csrf, query)
        except Exception as exc:  # noqa: BLE001
            await sink.emit_error("gem", f"kw:{term}", f"numFound failed: {exc!r}")
            continue
        pages = math.ceil(nf / PER_PAGE) if nf else 0
        if pages == 0:
            print(f"[GeM]   '{term}': 0 open bids")
            continue
        capped = ""
        if pages > pages_per_keyword:      # safety backstop only; shouldn't trigger
            pages = pages_per_keyword
            capped = f" (SAFETY-CAPPED at {pages_per_keyword})"
        print(f"[GeM]   '{term}': {nf:,} open raw hits -> scanning all {pages} pages{capped}")
        sink.set_total(pages * PER_PAGE)
        await asyncio.gather(*(worker(query, p) for p in range(1, pages + 1)))
        await sink.flush()
    print("[GeM] keyword scrape done.")
