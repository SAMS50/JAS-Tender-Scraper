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
from urllib.parse import quote

def page_url(page):
    inner_url = (
        f"{BASE}/cppp/latestactivetendersnew/"
        f"cpppdata?page={page}"
    )
    encoded = base64.b64encode(
        inner_url.encode("utf-8")
    ).decode("ascii")

    return (
        f"{BASE}/cppp/latestactivetendersnew/"
        f"cpppdata?url={quote(encoded, safe='')}"
    )

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
        "opening_date": cells[3].get_text(" ", strip=True),   # tender opening
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
    """Estimate page count from visible pagination links."""
    pages = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        matches = re.findall(r"[?&]page=(\d+)", href)
        pages.extend(int(m) for m in matches)

    return max(pages) if pages else 1



async def _scrape_page(session, config, sink, page) -> None:
    """Fetch + parse one listing page."""
    url = page_url(page)
    try:
        html = await fetch(session, "GET", url, config, expect="text")
    except Exception as exc:  # noqa: BLE001
        await sink.emit_error("cppp", f"page:{page}", f"fetch failed: {exc!r}")
        return

    # Safety net: never parse a page that turned out to be CAPTCHA-gated.
    if 'name="captchaText"' in html:
        await sink.emit_error("cppp", f"page:{page}", "unexpected captcha on page")
        return

    soup = BeautifulSoup(html, "html.parser")
    table = _find_listing_table(soup)
    if table is None:
        await sink.emit_error("cppp", f"page:{page}", "listing table not found")
        return

    rows = table.find_all("tr")[1:]
    for row in rows:
        try:
            tender = _parse_row(row)
            if not tender:
                continue
            await sink.emit(tender)
        except Exception as exc:  # noqa: BLE001
            await sink.emit_error("cppp", "unknown", f"row parse failed: {exc!r}")


async def scrape_cppp(session, config: ScraperConfig, sink: ResultSink,
                      max_pages: int | None = None) -> None:
    """Full scan every run: walk every listing page, no early-stop."""
    print("[CPPP] fetching first page to discover pagination ...")
    html = await fetch(session, "GET", FIRST_PAGE, config, expect="text")
    soup = BeautifulSoup(html, "html.parser")
    total_pages = max_pages if max_pages else _total_pages(soup)
    print(f"[CPPP] scraping all {total_pages:,} pages "
          f"(~{total_pages * 10:,} tenders)")
    sink.set_total(total_pages * 10)

    sem = asyncio.Semaphore(config.concurrency)

    async def worker(page: int) -> None:
        async with sem:
            await _scrape_page(session, config, sink, page)

    await asyncio.gather(*(worker(p) for p in range(1, total_pages + 1)))
    await sink.flush()
    print("[CPPP] done.")
