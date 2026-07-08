"""
Module 2 - CPPP (Central Public Procurement Portal / eprocure.gov.in).

Recon findings:
  * The Tapestry pages at /eprocure/app?page=FrontEndLatestActiveTenders are
    CAPTCHA-GATED ("Provide Captcha and click Search to list all active
    tenders"). We deliberately DO NOT use those.
  * The Drupal frontend is CAPTCHA-FREE and paginated:
        GET https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata?page=N
    Returns a server-rendered HTML table, 10 rows/page, 1-indexed
    (page=1 == the base listing). ~3,400+ pages of active tenders.
  * Each row: Sl.No | e-Published Date | Bid Submission Closing Date |
              Tender Opening Date | Title/Ref.No./Tender Id | Organisation | Corrigendum
  * The Title cell links to /cppp/tendersfullview/<segments> where the segments
    are split on the literal "A13h1"; the LAST segment is base64(tender_id) and
    the 2nd-last is base64(ref_no).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re

from bs4 import BeautifulSoup

from pipeline import ResultSink, ScraperConfig, fetch

BASE = "https://eprocure.gov.in"
FIRST_PAGE = f"{BASE}/cppp/latestactivetendersnew"
DATA_URL = f"{BASE}/cppp/latestactivetendersnew/cpppdata?page={{page}}"

_HEADER_RE = re.compile(r"Title/Ref\.No\./Tender Id", re.I)
_SEG_SPLIT = "A13h1"


def _b64(seg: str) -> str:
    """Decode a URL segment; tolerate missing padding and junk."""
    try:
        return base64.b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def _find_listing_table(soup: BeautifulSoup):
    for t in soup.find_all("table"):
        if _HEADER_RE.search(t.get_text()):
            return t
    return None


def _parse_detail_href(href: str) -> tuple[str, str]:
    """Return (tender_id, ref_no) decoded from the tendersfullview link."""
    segs = href.split(_SEG_SPLIT)
    tender_id = _b64(segs[-1]) if len(segs) >= 1 else ""
    ref_no = _b64(segs[-2]) if len(segs) >= 2 else ""
    return tender_id.strip(), ref_no.strip()


def _parse_row(row) -> dict | None:
    cells = row.find_all("td")
    if len(cells) < 6:
        return None
    link = cells[4].find("a", href=re.compile("tendersfullview"))
    href = link["href"] if link and link.has_attr("href") else ""
    tender_id, ref_no = _parse_detail_href(href) if href else ("", "")

    # The title cell reads "<TITLE> /<REF_NO>/<TENDER_ID>". Split on the first
    # " /" to isolate the human title from the ref/id suffix.
    raw = cells[4].get_text(" ", strip=True)
    title = re.split(r"\s+/", raw, maxsplit=1)[0].strip() or raw
    if not tender_id:
        # Fall back to the trailing token of the cell text.
        tail = raw.rsplit("/", 1)
        tender_id = tail[-1].strip() if len(tail) > 1 else ""

    corrig = cells[6].get_text(" ", strip=True) if len(cells) > 6 else ""
    corrig = "" if corrig in ("--", "-", "") else corrig

    return {
        "tender_id": tender_id,
        "title": title,
        "description": ref_no,   # ref number is the closest listing-level detail
        "organization": cells[5].get_text(" ", strip=True),
        "state": "",             # this view exposes Organisation, not State
        "published_date": cells[1].get_text(" ", strip=True),
        "closing_date": cells[2].get_text(" ", strip=True),
        "source": "cppp",
        # NOTE: the tendersfullview href is NOT a stable/shareable link. It
        # only renders if the same browser session first visited the listing
        # page (a server-side anti-deep-linking check) -- opened cold, it
        # returns "Invalid Url. Please Check". Verified: no session -> broken,
        # even a session from a different eprocure.gov.in page -> still
        # broken, only a session that just loaded the listing -> works. There
        # is no stateless per-tender URL on CPPP, so we leave this blank
        # rather than hand out a link that will fail for the user.
        "source_url": "",
        "corrigendum": corrig,
    }


def _total_pages(soup: BeautifulSoup) -> int:
    """Read the highest page number from the Drupal pager."""
    pages = [int(m) for m in re.findall(r"[?&]page=(\d+)",
                                        " ".join(a.get("href", "")
                                                 for a in soup.find_all("a")))]
    return max(pages) if pages else 1


async def _scrape_page(session, config, sink, page) -> bool | None:
    """
    Fetch + parse one listing page. Returns:
      True  -> page fetched, every row on it was already known from a prior run
      False -> page fetched and had at least one new/updated row
      None  -> page failed or was empty/unparseable (never counts toward early-stop)
    """
    url = DATA_URL.format(page=page)
    try:
        html = await fetch(session, "GET", url, config, expect="text")
    except Exception as exc:  # noqa: BLE001
        await sink.emit_error("cppp", f"page:{page}", f"fetch failed: {exc!r}")
        return None

    # Safety net: never parse a page that turned out to be CAPTCHA-gated.
    if 'name="captchaText"' in html:
        await sink.emit_error("cppp", f"page:{page}", "unexpected captcha on page")
        return None

    soup = BeautifulSoup(html, "html.parser")
    table = _find_listing_table(soup)
    if table is None:
        await sink.emit_error("cppp", f"page:{page}", "listing table not found")
        return None

    rows = table.find_all("tr")[1:]
    if not rows:
        return None

    all_known = True
    for row in rows:
        try:
            tender = _parse_row(row)
            if not tender:
                continue
            # Check BEFORE emit (emit will add it), so this reflects prior runs.
            if not sink.is_known("cppp", tender["tender_id"]):
                all_known = False
            await sink.emit(tender)
        except Exception as exc:  # noqa: BLE001
            all_known = False
            await sink.emit_error("cppp", "unknown", f"row parse failed: {exc!r}")
    await sink.save_page("cppp", page)
    return all_known


async def scrape_cppp(session, config: ScraperConfig, sink: ResultSink,
                      max_pages: int | None = None) -> None:
    """
    CPPP's active-tenders listing is sorted newest-first, so on a resumed run
    we page sequentially in concurrency-sized batches and stop as soon as an
    entire batch comes back all-known — everything past that point was already
    collected last time. A cold/--no-resume run always walks every page.
    """
    print("[CPPP] fetching first page to discover pagination ...")
    html = await fetch(session, "GET", FIRST_PAGE, config, expect="text")
    soup = BeautifulSoup(html, "html.parser")
    total_pages = _total_pages(soup)
    if max_pages:
        total_pages = min(total_pages, max_pages)
    print(f"[CPPP] scraping up to {total_pages:,} pages "
          f"(~{total_pages * 10:,} tenders){' [incremental]' if config.resume else ''}")
    sink.set_total(total_pages * 10)

    sem = asyncio.Semaphore(config.concurrency)

    async def worker(page: int) -> bool | None:
        async with sem:
            return await _scrape_page(session, config, sink, page)

    batch_size = config.concurrency
    page = 1
    while page <= total_pages:
        batch = list(range(page, min(page + batch_size, total_pages + 1)))
        results = await asyncio.gather(*(worker(p) for p in batch))
        # Early-stop as soon as an entire batch (including the first) comes
        # back all-known from a previous run.
        checked = [r for r in results if r is not None]
        if config.resume and checked and all(checked):
            print(f"[CPPP] pages {batch[0]}-{batch[-1]} were all already "
                  f"collected -> stopping early (incremental update). "
                  f"Pass --no-resume for a full rescan.")
            break
        page += batch_size

    await sink.flush()
    print("[CPPP] done.")
