"""
PSU tender portals that are CAPTCHA-free and scrapable.

Recon (2026-07-05) found that MOST top-PSU portals are CAPTCHA-gated (ONGC,
HPCL, SAIL) or run GePNIC with captcha'd listings (IOCL, BHEL, Coal India), and
that CPPP already aggregates most PSU/CPSE tenders. The ones scrapable directly
are registered here.

  * GAIL (gailtenders.in) — classic-ASP site. The active-tender list is a POST
    to tender_report.asp with val=4 ("All Active Tenders"); pagination is the
    `gotopage` form field (20 rows/page). Each row's 4 direct <td>s are:
    Sr.No | Closing Date | Subject (+ hidden internal id) | Ref.No. Detail is a
    JS `chek_it(...)` call, so there's no shareable URL (source_url left blank).

Add more PSUs by writing a scrape_<psu>() coroutine and registering it in
PSU_PORTALS. Dedup across CPPP/GeM is handled by the shared pipeline on
(source, tender_id), so redundancy is harmless.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

from pipeline import ResultSink, ScraperConfig, fetch

GAIL_HOME = "https://gailtenders.in/Gailtenders/Home.asp"
GAIL_REPORT = "https://gailtenders.in/Gailtenders/tender_report.asp"
GAIL_MAX_PAGES = 100  # safety backstop


def _gail_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for tb in soup.find_all("table"):
        if "Tender Subject" not in tb.get_text():
            continue
        for tr in tb.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 4 or not re.match(r"^\d+\.$", cells[0].get_text(strip=True)):
                continue
            subject = cells[2].get_text(" ", strip=True)
            ref = cells[3].get_text(" ", strip=True)
            if not (subject and ref):
                continue
            out.append({
                "tender_id": ref,
                "title": subject,
                "description": "",
                "organization": "GAIL (India) Limited",
                "state": "",
                "published_date": "",
                "closing_date": cells[1].get_text(" ", strip=True),
                "source": "gail",
                "source_url": "",   # detail is a JS chek_it() call, not a URL
                "corrigendum": "",
            })
    return out


async def scrape_gail(session, config: ScraperConfig, sink: ResultSink,
                      max_pages: int | None = None) -> None:
    print("[GAIL] scraping active tenders ...")
    try:
        await fetch(session, "GET", GAIL_HOME, config, expect="text")  # warm session
    except Exception as exc:  # noqa: BLE001
        await sink.emit_error("gail", "home", f"warm-up failed: {exc!r}")
        return

    page = 1
    prev_sig = None
    total = 0
    limit = max_pages or GAIL_MAX_PAGES
    while page <= limit:
        data = {"val": "4"}
        if page > 1:
            data["gotopage"] = str(page)
        try:
            html = await fetch(session, "POST", GAIL_REPORT, config, expect="text",
                               data=data, headers={"Referer": GAIL_HOME})
        except Exception as exc:  # noqa: BLE001
            await sink.emit_error("gail", f"page:{page}", f"fetch failed: {exc!r}")
            break
        rows = _gail_rows(html)
        sig = tuple(r["tender_id"] for r in rows)
        if not rows or sig == prev_sig:
            break
        prev_sig = sig
        sink.set_total(len(rows))
        for r in rows:
            await sink.emit(r)
        total += len(rows)
        page += 1

    await sink.flush()
    print(f"[GAIL] done — {total} rows scanned.")


# --------------------------------------------------------------------------- Coal India
COALINDIA_URL = "https://www.coalindia.in/tenders/"
_DATE_RE = re.compile(r"\d{2}-\d{2}-\d{4}")


_COAL_LABELS = {"Tender Reference No.", "Title", "Tender Value", "EMD",
                "Document Cost", "Sale start date", "Sale end date",
                "Tender Opening date", "Pre BID / Pre NIT Meet Date", "Contact"}


def _coal_kv(table) -> dict:
    """Each Coal India tender is its own <table> of label/value cell pairs."""
    cells = [c.get_text(" ", strip=True) for c in table.find_all("td")]
    d, i = {}, 0
    while i < len(cells) - 1:
        if cells[i] in _COAL_LABELS:
            d[cells[i]] = cells[i + 1]
            i += 2
        else:
            i += 1
    return d


async def scrape_coalindia(session, config: ScraperConfig, sink: ResultSink,
                           max_pages: int | None = None) -> None:
    print("[CoalIndia] scraping tenders (one <table> per tender) ...")
    try:
        html = await fetch(session, "GET", COALINDIA_URL, config, expect="text")
    except Exception as exc:  # noqa: BLE001
        await sink.emit_error("coalindia", "page", f"fetch failed: {exc!r}")
        return
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    for tb in soup.find_all("table"):
        if "Tender Reference No." not in tb.get_text():
            continue
        d = _coal_kv(tb)
        ref = d.get("Tender Reference No.", "")
        if not ref or ref.startswith("Tender Reference"):
            continue
        pdf = tb.find("a", href=re.compile(r"/documents/"))
        # Prefer the bid-submission end (Sale end date); fall back to opening.
        closing = d.get("Sale end date") or d.get("Tender Opening date", "")
        desc = "; ".join(p for p in (
            f"Value: {d['Tender Value']}" if d.get("Tender Value") else "",
            f"EMD: {d['EMD']}" if d.get("EMD") else "",
        ) if p)
        sink.set_total(1)
        await sink.emit({
            "tender_id": ref, "title": d.get("Title", ""), "description": desc,
            "organization": "Coal India Limited", "state": "",
            "published_date": d.get("Sale start date", ""),
            "opening_date": d.get("Tender Opening date", ""),
            "closing_date": closing,
            "source": "coalindia",
            "source_url": ("https://www.coalindia.in" + pdf["href"]) if pdf and pdf["href"].startswith("/") else (pdf["href"] if pdf else ""),
            "corrigendum": "",
        })
        total += 1
    await sink.flush()
    print(f"[CoalIndia] done — {total} tenders.")


# --------------------------------------------------------------------------- NTPC
NTPC_BASE = "https://ntpctender.ntpc.co.in"
NTPC_URL = f"{NTPC_BASE}/Index/Search"


async def scrape_ntpc(session, config: ScraperConfig, sink: ResultSink,
                      max_pages: int | None = None) -> None:
    print("[NTPC] scraping tenders (single-page DataTable) ...")
    try:
        html = await fetch(session, "GET", NTPC_URL, config, expect="text")
    except Exception as exc:  # noqa: BLE001
        await sink.emit_error("ntpc", "page", f"fetch failed: {exc!r}")
        return
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    for tb in soup.find_all("table"):
        if not re.search(r"Tender/NIT Ref|Tender Title", tb.get_text(), re.I):
            continue
        for tr in tb.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 6:
                continue
            ref = tds[1].get_text(" ", strip=True)
            title = tds[3].get_text(" ", strip=True)
            if not ref or not title:
                continue
            link = tds[6].find("a", href=True) if len(tds) > 6 else None
            href = link["href"] if link else (tr.find("a", href=re.compile("NITDetails")) or {}).get("href", "")
            sink.set_total(1)
            await sink.emit({
                "tender_id": ref, "title": title, "description": "",
                "organization": tds[4].get_text(" ", strip=True), "state": "",
                "published_date": "", "closing_date": tds[5].get_text(" ", strip=True),
                "source": "ntpc",
                "source_url": (NTPC_BASE + href) if href.startswith("/") else href,
                "corrigendum": "",
            })
            total += 1
        break
    await sink.flush()
    print(f"[NTPC] done — {total} tenders.")


# --------------------------------------------------------------------------- BHEL
BHEL_BASE = "https://tenders.bhel.com"
BHEL_URL = f"{BHEL_BASE}/tenders"
BHEL_MAX_PAGES = 400
_BHEL_DT = re.compile(r"(\d{2}-\d{2}-\d{4})")


def _bhel_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tb in soup.find_all("table"):
        if not re.search(r"NIT Number|Tender Description", tb.get_text(), re.I):
            continue
        for tr in tb.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            nit = tds[0].get_text(" ", strip=True)
            if not re.match(r"^\d{4,}$", nit):
                continue
            link = tds[1].find("a", href=True)
            title = tds[1].get_text(" ", strip=True)
            title = re.sub(r"^Tender NIT Number\s*:\s*\d+\s*", "", title)
            rows.append({
                "tender_id": nit,
                "title": title,
                "description": "",
                "organization": "BHEL - " + tds[2].get_text(" ", strip=True),
                "state": "",
                "published_date": "",
                "closing_date": tds[3].get_text(" ", strip=True),
                "source": "bhel",
                "source_url": link["href"] if link else "",
                "corrigendum": "",
            })
        break
    return rows


async def scrape_bhel(session, config: ScraperConfig, sink: ResultSink,
                      max_pages: int | None = None) -> None:
    print("[BHEL] scraping tenders (paginated) ...")
    now = datetime.now()
    limit = max_pages or BHEL_MAX_PAGES
    page = 1
    total = 0
    prev_sig = None
    while page <= limit:
        url = BHEL_URL if page == 1 else f"{BHEL_URL}?page={page}"
        try:
            html = await fetch(session, "GET", url, config, expect="text")
        except Exception as exc:  # noqa: BLE001
            await sink.emit_error("bhel", f"page:{page}", f"fetch failed: {exc!r}")
            break
        rows = _bhel_rows(html)
        sig = tuple(r["tender_id"] for r in rows)
        if not rows or sig == prev_sig:
            break
        prev_sig = sig
        sink.set_total(len(rows))
        for r in rows:
            await sink.emit(r)
        total += len(rows)
        # Stop once a whole page is past its opening date (list is newest-first),
        # i.e. we've walked past all live tenders.
        def _future(r):
            m = _BHEL_DT.search(r["closing_date"])
            if not m:
                return True
            try:
                return datetime.strptime(m.group(1), "%d-%m-%Y") >= now
            except ValueError:
                return True
        if not any(_future(r) for r in rows):
            break
        page += 1
    await sink.flush()
    print(f"[BHEL] done — {total} tenders across {page} page(s).")


# Registry of PSU scrapers: name -> coroutine(session, config, sink, max_pages)
PSU_PORTALS = {
    "gail": scrape_gail,
    "coalindia": scrape_coalindia,
    "ntpc": scrape_ntpc,
    "bhel": scrape_bhel,
}
