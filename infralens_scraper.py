"""
Module - Infralens / BidEasy aggregator scraper (tenders.infralens.in).

Infralens aggregates tenders from portals across India (Central, State, PSU).
Its `/api/search` endpoint is gated by a lightweight SHA-256 proof-of-work:
    1. GET  /api/challenge            -> { c, zeros }
    2. find nonce n where sha256(f"{c}:{n}") starts with `zeros` hex zeros
    3. POST /api/pass {c, n}          -> sets the `bqs` (+nl/sid) cookie
    4. GET  /api/search?q=...         -> structured JSON results, with that cookie

There's no full listing endpoint, so like the TS collector this scrapes is
search-driven: we run one query per target keyword/phrase and let the
downstream KeywordMatcher do the strict re-filtering (infralens' own search is
fuzzy/relevance-based, not a phrase match).
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from pipeline import ResultSink, ScraperConfig, fetch

BASE = "https://tenders.infralens.in"
CHALLENGE_URL = f"{BASE}/api/challenge"
PASS_URL = f"{BASE}/api/pass"
SEARCH_URL = f"{BASE}/api/search"

# Extra queries beyond the shared KEYWORDS list, mirroring the TS collector's
# broader relevance-search coverage (phrases that don't appear verbatim in
# keywords.py but surface good candidates for the strict matcher to re-check).
EXTRA_QUERIES = [
    "artificial intelligence", "machine learning", "computer vision",
    "natural language processing", "generative ai", "website development",
    "web application", "web portal", "mobile application",
    "software development", "custom software", "application development",
    "api integration", "erp", "crm", "management information system",
    "system integration", "cloud", "saas", "gis", "internet of things",
    "robotic process automation", "data analytics", "business intelligence",
    "digital transformation", "e-governance", "digital platform",
    "it consultancy", "information technology",
]


def _sha256hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _extract_cookie(headers) -> str | None:
    """aiohttp exposes repeated Set-Cookie headers via getall()."""
    pieces = []
    for raw in headers.getall("Set-Cookie", []):
        piece = raw.split(";")[0]
        if piece.split("=")[0] in ("bqs", "nl", "sid"):
            pieces.append(piece)
    return "; ".join(pieces) if pieces else None


async def _solve_challenge(session, config: ScraperConfig) -> str:
    """Fetch the PoW challenge, solve it, and return the resulting cookie header."""
    async with session.get(CHALLENGE_URL, timeout=config.request_timeout) as resp:
        cookie = _extract_cookie(resp.headers)
        data = json.loads(await resp.text())

    c = data["c"]
    zeros = data.get("zeros", 3)
    target = "0" * zeros
    n = 0
    max_n = 8_000_000
    while n < max_n:
        if _sha256hex(f"{c}:{n}").startswith(target):
            break
        n += 1
    else:
        raise RuntimeError("Infralens PoW not solved within bound")

    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    async with session.post(
        PASS_URL, json={"c": c, "n": n}, headers=headers,
        timeout=config.request_timeout,
    ) as resp:
        pass_cookie = _extract_cookie(resp.headers)
    if not pass_cookie:
        raise RuntimeError("Infralens /api/pass returned no access token")
    return f"{cookie}; {pass_cookie}" if cookie else pass_cookie


def _first_url(u: str | None) -> str:
    if not u:
        return ""
    return u if u.startswith("http") else f"{BASE}{u}"


def _parse_result(r: dict) -> dict:
    """Map a raw infralens search hit into the normalized tender schema."""
    title = (r.get("t") or r.get("wd") or "").strip()
    org = (r.get("or") or "").strip()
    dept = (r.get("dp") or "").strip()
    organization = " / ".join(p for p in (dept, org) if p)
    slug = (r.get("u") or "").split("/")[-1] or title
    return {
        "tender_id": slug[:120],
        "title": title,
        "description": (r.get("wd") or "").strip(),
        "organization": organization,
        "state": (r.get("st") or r.get("ds") or "").strip(),
        "published_date": "",  # infralens does not expose a floated/published date
        "closing_date": (r.get("cn") or r.get("cl") or "").strip(),
        "source": "infralens",
        "source_url": _first_url(r.get("u")) or BASE,
        "corrigendum": "",
        # Raw work-type field ("Consultancy / Service", "New construction", ...).
        # pipeline.normalize_category keeps only values that literally name
        # Service/Product/Works; the rest are honestly Unidentified.
        "tender_type": (r.get("wk") or "").strip(),
    }


async def _search(session, config: ScraperConfig, cookie: str, query: str, limit: int = 100) -> list[dict]:
    url = f"{SEARCH_URL}?q={query}&limit={limit}"
    data = await fetch(
        session, "GET", url, config, expect="json",
        headers={"Cookie": cookie, "Accept": "application/json,text/plain,*/*"},
    )
    if data.get("token_required"):
        raise RetryToken()
    return data.get("results") or []


class RetryToken(Exception):
    """Raised when infralens says our access token expired mid-run."""


async def scrape_infralens(session, config: ScraperConfig, sink: ResultSink,
                            keywords: list[str], max_pages: int | None = None) -> None:
    """
    Entry point: run one search per keyword/query term against the infralens
    aggregator, tag each hit, and let the sink's shared KeywordMatcher decide
    what's actually kept (infralens' relevance search is a superset).
    """
    print("[Infralens] solving proof-of-work challenge ...")
    cookie = await _solve_challenge(session, config)
    print("[Infralens] token acquired.")

    queries = sorted(set(list(keywords) + EXTRA_QUERIES))
    sink.set_total(len(queries) * 100)
    sem = asyncio.Semaphore(min(config.concurrency, 5))
    # Infralens rate-limits aggressively (silently returns empty results when
    # hammered), so cap concurrency well below the shared pool and add a
    # small delay between requests regardless of the configured rate_limit.
    lock = asyncio.Lock()

    async def worker(term: str) -> None:
        nonlocal cookie
        async with sem:
            async with lock:
                await asyncio.sleep(max(config.rate_limit, 0.5))
            try:
                results = await _search(session, config, cookie, term)
            except RetryToken:
                print(f"[Infralens]   token expired, re-solving for '{term}' ...")
                cookie = await _solve_challenge(session, config)
                try:
                    results = await _search(session, config, cookie, term)
                except Exception as exc:  # noqa: BLE001
                    await sink.emit_error("infralens", f"q:{term}", f"retry failed: {exc!r}")
                    return
            except Exception as exc:  # noqa: BLE001
                await sink.emit_error("infralens", f"q:{term}", f"search failed: {exc!r}")
                return

            for r in results:
                try:
                    await sink.emit(_parse_result(r))
                except Exception as exc:  # noqa: BLE001
                    await sink.emit_error("infralens", r.get("u", ""), f"parse failed: {exc!r}")

    await asyncio.gather(*(worker(q) for q in queries))
    await sink.flush()
    print("[Infralens] done.")
