"""
State e-procurement portals that are NOT NIC/GePNIC and are CAPTCHA-free.

These run on ASP.NET (WebForms) and paginate via __doPostBack against a
GridView, so we can't just bump a ?page= query param — each page is a POST that
must echo back the full form (__VIEWSTATE, __EVENTVALIDATION, every input) plus
__EVENTTARGET=<grid> and __EVENTARGUMENT=Page$N. We page sequentially because
the viewstate changes per page.

Confirmed CAPTCHA-free during recon (2026-07-05):
  * Gujarat (GIL)     https://gil.gujarat.gov.in/Tenders        grid ctl00$body$gvTenderList
  * Haryana (Works)   https://works.haryana.gov.in/LiveTender.aspx  grid gd_Tender

These are small (tens of pages), so we full-scan every run rather than
early-stopping — cheap and guarantees nothing is missed. Dedup is handled by
the shared pipeline on (source, tender_id).
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline import ResultSink, ScraperConfig, fetch

MAX_PAGES_HARD_CAP = 500  # safety backstop against a pager that never terminates


# --------------------------------------------------------------------------- parsers
def _split_br(cell) -> list[str]:
    """Split a cell on <br> into its text parts (Haryana packs 2 values/cell)."""
    for br in cell.find_all("br"):
        br.replace_with("||")
    return [p.strip() for p in cell.get_text().split("||") if p.strip()]


def _parse_gujarat(row, cfg) -> dict | None:
    cells = row.find_all("td")
    if len(cells) < 6:
        return None
    link = cells[5].find("a", href=True) or cells[1].find("a", href=True)
    href = link["href"] if link else ""
    m = re.search(r"TenID=(\d+)", href)
    if not m:
        return None  # skips the pager row
    return {
        "tender_id": m.group(1),
        "title": cells[1].get_text(" ", strip=True),
        "description": "",
        "organization": "",  # not exposed in the listing
        "state": "Gujarat",
        "published_date": cells[2].get_text(" ", strip=True),   # pre-bid meeting
        "closing_date": cells[3].get_text(" ", strip=True),     # last date of submission
        "source": "gujarat",
        "source_url": urljoin(cfg["url"], href),
        "corrigendum": "",
    }


def _parse_haryana(row, cfg) -> dict | None:
    cells = row.find_all("td")
    if len(cells) < 7:
        return None
    id_parts = _split_br(cells[1])
    tid = ""
    for p in id_parts:                       # prefer the human ref e.g. 2026_HRY_532500_1
        mm = re.search(r"(20\d\d_[A-Za-z0-9_]+)", p)
        if mm:
            tid = mm.group(1)
            break
    if not tid and id_parts:                 # fallback: the hex e-tender id
        mm = re.search(r"([0-9A-F]{8,})", id_parts[0])
        tid = mm.group(1) if mm else ""
    if not tid:
        return None
    title_parts = _split_br(cells[2])
    org_parts = _split_br(cells[3])
    est = cells[5].get_text(" ", strip=True)
    emd = cells[6].get_text(" ", strip=True)
    return {
        "tender_id": tid,
        "title": max(title_parts, key=len) if title_parts else "",
        "description": f"Estimated: {est}; EMD: {emd}".strip("; "),
        "organization": " - ".join(org_parts),
        "state": "Haryana",
        "published_date": "",
        "closing_date": cells[4].get_text(" ", strip=True),
        "source": "haryana",
        # Detail is a postback "Download" button, not a shareable URL -> blank.
        "source_url": "",
        "corrigendum": "",
    }


STATE_ASPNET_PORTALS: dict[str, dict] = {
    "gujarat": {
        "source": "gujarat",
        "url": "https://gil.gujarat.gov.in/Tenders",
        "grid": "ctl00$body$gvTenderList",
        "table_needle": r"Announcements of Bids|Last Date of Submission",
        "parse_row": _parse_gujarat,
    },
    "haryana": {
        "source": "haryana",
        "url": "https://works.haryana.gov.in/LiveTender.aspx",
        "grid": "gd_Tender",
        "table_needle": r"e-Tender ID|Tender Name",
        "parse_row": _parse_haryana,
    },
}


# --------------------------------------------------------------------------- engine
def _form_fields(soup: BeautifulSoup) -> dict[str, str]:
    """Serialize every input/select in the form (ASP.NET needs the whole form)."""
    data: dict[str, str] = {}
    form = soup.find("form")
    if form is None:
        return data
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        t = (inp.get("type") or "text").lower()
        if t in ("submit", "button", "image"):
            continue
        if t in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        data[name] = inp.get("value", "")
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        data[name] = opt.get("value", "") if opt else ""
    return data


def _find_table(soup: BeautifulSoup, needle: str):
    for t in soup.find_all("table"):
        if re.search(needle, t.get_text(" ", strip=True), re.I) and len(t.find_all("tr")) > 2:
            return t
    return None


async def scrape_state_portal(session, config: ScraperConfig, sink: ResultSink,
                              key: str, max_pages: int | None = None) -> None:
    cfg = STATE_ASPNET_PORTALS.get(key)
    if not cfg:
        print(f"[{key}] not a registered ASP.NET state portal; skipping.")
        return
    url = cfg["url"]
    print(f"[{key}] scraping {url}")

    try:
        html = await fetch(session, "GET", url, config, expect="text")
    except Exception as exc:  # noqa: BLE001
        await sink.emit_error(cfg["source"], "page:1", f"initial fetch failed: {exc!r}")
        return

    page = 1
    prev_sig = None
    total_emitted = 0
    while page <= (max_pages or MAX_PAGES_HARD_CAP):
        soup = BeautifulSoup(html, "html.parser")
        if "captcha" in html.lower():
            await sink.emit_error(cfg["source"], f"page:{page}", "unexpected captcha")
            break
        table = _find_table(soup, cfg["table_needle"])
        if table is None:
            await sink.emit_error(cfg["source"], f"page:{page}", "listing table not found")
            break

        data_rows = table.find_all("tr")[1:]
        parsed = [cfg["parse_row"](r, cfg) for r in data_rows]
        parsed = [p for p in parsed if p and p["tender_id"]]
        sig = tuple(p["tender_id"] for p in parsed)
        # ASP.NET clamps out-of-range Page$N to the last page, so a repeated
        # signature means we've run off the end.
        if not sig or sig == prev_sig:
            break
        prev_sig = sig
        sink.set_total(len(parsed))  # set_total accumulates across pages/sources
        for t in parsed:
            await sink.emit(t)
        total_emitted += len(parsed)

        # Next page via postback.
        page += 1
        form = _form_fields(soup)
        form["__EVENTTARGET"] = cfg["grid"]
        form["__EVENTARGUMENT"] = f"Page${page}"
        try:
            html = await fetch(session, "POST", url, config, expect="text",
                               data=form, headers={"Referer": url})
        except Exception as exc:  # noqa: BLE001
            await sink.emit_error(cfg["source"], f"page:{page}", f"page fetch failed: {exc!r}")
            break

    await sink.flush()
    print(f"[{key}] done — {total_emitted} rows scanned.")
