# Government Tender Scraper (GeM + CPPP + State GePNIC)

Async scraper for public Indian government tender listings. No login, no CAPTCHA
solving — it only touches listing pages that are plainly viewable.

## Setup (fresh Windows machine)

Requires Python 3.10+ (tested on 3.12). In PowerShell or cmd:

```powershell
# 1. Get the code onto the new machine (copy the whole tender_scraper/ folder,
#    or clone/pull the repo it lives in).

# 2. From inside tender_scraper/, create and activate a virtual environment
cd path\to\tender_scraper
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run it
python run_scraper.py --source all
```

To deactivate the virtual environment later: `deactivate`. On the next session,
just re-run `venv\Scripts\activate` before using the scraper again — no need
to reinstall.

All output (`tenders_gem_cppp.csv`, `errors_log.csv`, checkpoints,
`seen_ids.json`) is written to `tender_scraper\outputs\` by default, regardless
of which directory you run the command from.

## Reconnaissance summary

Before any scraping logic was written, both primary sources were inspected for
CAPTCHA gating and their real data-loading mechanism:

| Source | Listing viewable? | How data actually loads |
|--------|-------------------|--------------------------|
| **GeM** (`bidplus.gem.gov.in/all-bids`) | ✅ No CAPTCHA | XHR `POST /all-bids-data` returning **Solr-style JSON** (10 docs/page). We send `filter.bidStatusType="ongoing_bids"` so only **open** bids are returned (see "GeM completeness" below), then scan every page per keyword. |
| **CPPP** (`eprocure.gov.in`) | ✅ No CAPTCHA *via the Drupal frontend* | `GET /cppp/latestactivetendersnew/cpppdata?page=N` — server-rendered HTML table, 10 rows/page. Tender IDs are base64-encoded inside the `tendersfullview` detail link (segments split on `A13h1`). **The detail link itself is not a shareable URL** — see below. |
| **Gujarat (GIL)** (`gil.gujarat.gov.in/Tenders`) | ✅ No CAPTCHA | ASP.NET GridView; paginates via `__doPostBack` + `__VIEWSTATE` (full-form postback). Working `TenderDetails.aspx?TenID=` detail links. |
| **Haryana (Works)** (`works.haryana.gov.in/LiveTender.aspx`) | ✅ No CAPTCHA | ASP.NET GridView; captures estimated value + EMD. Detail is a postback button (no shareable URL). |
| **PSUs: GAIL** (`gailtenders.in`) | ✅ No CAPTCHA | Classic-ASP; `POST tender_report.asp val=4`, paginated via `gotopage`. ~114 tenders. |
| **PSUs: Coal India** (`coalindia.in/tenders`) | ✅ No CAPTCHA | One `<table>` per tender (label/value cards); ~250 tenders in one page, with value/EMD. |
| **PSUs: NTPC** (`ntpctender.ntpc.co.in/Index/Search`) | ✅ No CAPTCHA (list) | Single-page DataTable (~86); the CAPTCHA is on the search filter, not the listing. Detail links `/NITDetails/NITs/<id>`. |
| **PSUs: BHEL** (`tenders.bhel.com/tenders`) | ✅ No CAPTCHA | `?page=N` pagination; we stop at closed tenders (live-only), ~600 live. Real detail URLs. |
| **Other PSU portals** | ❌ CAPTCHA / GePNIC / SPA | ONGC, HPCL, SAIL, IOCL, BHEL-GePNIC, NALCO gate their listings; NMDC is a JS SPA. Most PSU/CPSE tenders are also in CPPP. |
| **Telangana** (APTS) | ✅ via Playwright | Full list is JS-loaded + bot-guarded against raw HTTP, so we drive headless Chromium: land → click "More..." → read the table → page via "Next". ~226 pages. See `playwright_states.py` (optional install). |
| **Andhra Pradesh** (APTS) | ✅ via Playwright | Same as TS, but first close the ad modal (`.sp-close-btn`) then JS-click `#viewCurrentall`. Header-based column mapping handles AP's 9-col layout vs TS's 10. |
| **Other NIC/GePNIC states + most PSU portals** | ❌ CAPTCHA-gated | All swept state portals (Odisha, MH, WB, TN, Kerala, UP, MP, …) and most PSU portals (ONGC, HPCL, SAIL, IOCL, BHEL, Coal India) gate their listings behind a CAPTCHA. Not scraped. Most PSU/CPSE tenders are covered via CPPP anyway. |

> **CPPP caveat:** the Tapestry pages at `/eprocure/app?page=FrontEndLatestActiveTenders`
> **are CAPTCHA-gated** ("Provide Captcha and click Search to list all active tenders").
> Those are deliberately *not* used. Only the CAPTCHA-free Drupal `/cppp/` frontend is scraped.

## Files

- `pipeline.py` — shared: async HTTP w/ retries + backoff, 60-worker semaphore,
  rolling-ETA progress, atomic CSV writes, per-source checkpoint/resume, dedup, error log.
- `gem_scraper.py` — Module 1 (GeM), fully implemented (JSON).
- `cppp_scraper.py` — Module 2 (CPPP), fully implemented (HTML table).
- `state_gepnic_scraper.py` — Module 3, **STUB**: generic GePNIC parser +
  `STATE_PORTALS` registry + `add_state_portal(...)` + Telangana wiring + `--dry-run`.
- `run_scraper.py` — combined runner / CLI.
- `psu_portals.py` — PSU scrapers (GAIL, Coal India, NTPC, BHEL).
- `state_portals.py` — non-NIC state portals (Gujarat, Haryana).
- `playwright_states.py` — **optional** Telangana/AP scraper (headless browser).
- `dashboard.py` — local Flask web dashboard (4 tabs + refresh button).
- `requirements.txt` — third-party deps (`aiohttp`, `beautifulsoup4`, `flask`);
  everything else used is Python stdlib.

## Dashboard

A small local web UI over the scraped data:

```bash
pip install -r requirements.txt
python dashboard.py            # then open http://127.0.0.1:5000
```

Tabs:
- **GeM / CPPP** — central-portal tenders (`source` in gem/cppp).
- **State-wise** — state e-procurement tenders (Gujarat, Haryana).
- **PSUs** — dedicated PSU portals (GAIL, Coal India, NTPC, BHEL).
- **Empanelments** — cross-cut view of empanelment / EOI / rate-contract /
  enlistment notices detected across all sources.

**Keyword filtering applies to EVERY source.** Only tenders matching the target
IT/AI keyword list are kept and displayed — from GeM, CPPP, states, and PSUs
alike. Portals with no current IT tenders (e.g. GAIL, NTPC, Telangana civil
works) simply contribute 0 rows until such tenders appear; we still scrape them
so nothing is missed, but non-matching tenders are dropped rather than stored.

**Links per row:** every row is clickable. Sources with a stable detail URL
(GeM, Gujarat, Coal India, NTPC, BHEL) show a **direct** "open" link to the
tender. Sources whose detail is a JS postback or is session/WAF-guarded (CPPP,
Haryana, Telangana, AP, GAIL, BPCL) show a **"portal ↗"** link to that source's
live-tender listing, where you can search the ref number — because a direct link
there would hit an "Invalid Url"/error page.

The **Refresh** button runs an incremental re-scrape (`run_scraper.py --source
all`) in the background and streams progress. A **5-minute cooldown** is
enforced — refreshing sooner returns how long to wait. Because scraping is
incremental, a refresh only fetches what's new (usually seconds to a couple
minutes), not a full re-scrape.

## Usage

```bash
# Everything (resumable), writes to ./outputs/
python run_scraper.py --source all

# Quick smoke test: GeM, first 5 listing pages
python run_scraper.py --source gem --max-pages 5

# CPPP only, fresh run (ignore checkpoints)
python run_scraper.py --source cppp --no-resume

# State portals only (Gujarat + Haryana)
python run_scraper.py --source states
python run_scraper.py --source states --state gujarat   # single portal

# PSU portals (GAIL, Coal India, NTPC, BHEL)
python run_scraper.py --source psus

# Telangana / AP / BPCL via Playwright (OPTIONAL — needs the extra install below)
pip install playwright && python -m playwright install chromium
python run_scraper.py --source playwright                       # TS + AP + BPCL
python run_scraper.py --source tsap --state telangana           # single portal
python run_scraper.py --source tsap --state andhra_pradesh --max-pages 25

# Inspect a state's page structure BEFORE enabling it
python run_scraper.py --source state --state telangana --dry-run
```

Key flags: `--max-pages N` (cap pages/source — GeM's full set is ~550k pages!),
`--concurrency` (default 60), `--rate-limit` (default 0.25s), `--out-dir`,
`--no-resume`.

## Output

Written to `tender_scraper/outputs/` by default (anchored to this folder
regardless of your current working directory); override with `--out-dir`:

- `tenders_gem_cppp.csv` — one normalized row per tender, **sorted by
  closing_date descending** (latest/furthest-out deadline first; rows with a
  missing or unparseable closing date sort last):
  `tender_id, title, description, organization, state, published_date,
  closing_date, source, source_url, corrigendum, matched_keywords`
- `errors_log.csv` — anything that failed to fetch/parse, with the reason.
- `<source>_checkpoint.json` — completed listing pages per source, for resume.
- `seen_ids.json` — every tender ID ever scanned per source, whether it passed
  the keyword filter or not. This is what makes repeat runs fast (see below).

## Keyword filtering (default ON)

By default the scraper only keeps tenders matching an IT/software/AI keyword
list (`keywords.py`), tagging each kept row with which keyword(s) hit in
`matched_keywords`. Use `--no-filter` to disable and keep everything, or
`--keywords-file path.txt` for a custom list (one keyword per line).

GeM's server-side search is a **token-OR** match, not a phrase match — e.g.
searching "face recognition" returns paint and brooms because it matches
either word. So GeM keyword mode uses the server search only to shrink the
candidate set (`--gem-pages-per-keyword`, default 100 pages/keyword), then
applies a strict word-boundary/phrase-aware regex client-side to actually
decide what's kept.

## Incremental re-runs (why a second run is fast)

CPPP's active-tenders listing is sorted **newest-first**. On a resumed run
(the default — no `--no-resume`), the scraper walks pages in
concurrency-sized batches and stops the moment an entire batch comes back
"already seen" — everything past that point was already collected last time.
A cold run takes ~5 min for CPPP's ~34.5k active tenders; a same-day re-run
that finds nothing new takes ~10 seconds.

This depends on tracking **every** tender ID ever scanned, not just the ones
that passed the keyword filter — a filtered-out tender is still "seen" and
must not force a full rescan forever. That's what `seen_ids.json` is for;
don't delete it if you want fast incremental runs. `--no-resume` ignores it
and does a full rescan (use this if you suspect the ledger is stale or corrupt).

GeM keyword mode always does a fresh per-keyword search every run (see below).

## GeM completeness (open bids, no cap)

GeM's search is relevance-ranked (not date-sorted) and returns the entire 5.5M
all-time archive by default, so an early cap silently missed open tenders. The
fix: the `all-bids-data` payload accepts a `filter` object we now send with
`bidStatusType="ongoing_bids"` — this restricts to currently-open bids only,
collapsing each keyword's result set from tens-of-thousands to a few hundred.
That lets us scan **every** page per keyword (no cap), so no open keyword tender
is missed. GeM's token-OR search is a superset of true phrase matches, and the
client-side `KeywordMatcher` narrows to real hits.

## `source_url` — valid for GeM, blank for CPPP

GeM's `source_url` (`/showbidDocument/<id>`) is a real, stable, cold-clickable
link — verified to serve the actual bid PDF with zero prior session state.

CPPP's `tendersfullview` link is **not** a shareable URL: the detail page only
renders if the same browser session first loaded the listing page (a
server-side anti-deep-linking check). Opened cold — e.g. pasted from this CSV
into a fresh browser — it returns "Invalid Url. Please Check" no matter how
the link was constructed; there's no query-based or ID-based lookup on CPPP
that works statelessly either. Since there's no way to produce a working
direct link, `source_url` is left blank for CPPP rows rather than handing out
a URL that will fail.

## Design notes

- **Concurrency unit** = one listing page (~10 tenders), fanned out across the
  60-worker semaphore. GeM/CPPP expose a total up front, so pages run concurrently.
- **Checkpointing**: every 20 emitted rows the full CSV is rewritten atomically
  (temp file → `os.replace`) and checkpoints saved. On resume, prior rows are
  preloaded so already-done pages aren't lost when the file is rewritten.
- **Dedup**: keyed on `(source, tender_id)`; the latest version wins
  (explicit corrigendum > later published/closing date).
- **Retries**: 3× exponential backoff + jitter on network/5xx/429; a persistently
  failing page is logged to `errors_log.csv` and skipped, never crashing the run.

## Adding a new state (Module 3)

1. Register it (or add an entry to `STATE_PORTALS`):
   ```python
   add_state_portal("kerala",
                    "https://etenders.kerala.gov.in",
                    "/nicgep/app?page=FrontEndLatestActiveTenders"
                    "&service=direct&session=T&sp=S{page}")
   ```
2. `python run_scraper.py --source state --state kerala --dry-run` — eyeball the
   printed table/rows. If it shows a CAPTCHA, **stop** (leave it disabled).
3. If the generic parser fits, set `captcha_confirmed_free=True` for that state,
   then run without `--dry-run`.

The GePNIC pager fix (ported from the `find_company_names.py` pattern) stops
paging when the **Next** control's parent is disabled *or* the page's content
fingerprint repeats — GePNIC re-serves the last page instead of 404ing.
