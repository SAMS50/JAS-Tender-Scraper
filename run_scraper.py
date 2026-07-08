"""
Combined runner for the government-tender scraper.

Wires the GeM, CPPP, and (stubbed) state-GePNIC modules into one shared
pipeline that writes a single normalized CSV plus an errors log, with
checkpoint/resume support across all sources.

Examples
--------
    # Everything, resumable, default outputs/ dir
    python run_scraper.py --source all

    # Just GeM, first 5 pages (quick smoke test)
    python run_scraper.py --source gem --max-pages 5

    # CPPP only, fresh run (ignore checkpoints)
    python run_scraper.py --source cppp --no-resume

    # Inspect a newly-registered state before enabling it
    python run_scraper.py --source state --state telangana --dry-run

Output defaults to tender_scraper/outputs/, anchored to this file's location
(not the current working directory) -- override with --out-dir.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import cppp_scraper
import gem_scraper
import psu_portals
import state_gepnic_scraper as gepnic
import state_portals
from keywords import KeywordMatcher, load_keywords
from pipeline import ResultSink, ScraperConfig, make_session

# Anchor the default output dir to this file's location (not the process's
# CWD), so `outputs/` always lands inside tender_scraper/ regardless of where
# the script is invoked from.
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")


def build_config(args) -> ScraperConfig:
    return ScraperConfig(
        concurrency=args.concurrency,
        rate_limit=args.rate_limit,
        checkpoint_every=args.checkpoint_every,
        out_dir=args.out_dir,
        resume=not args.no_resume,
    )


async def run(args) -> None:
    config = build_config(args)

    # Keyword filtering is ON by default; --no-filter disables it.
    matcher = None
    if not args.no_filter:
        kws = load_keywords(args.keywords_file)
        matcher = KeywordMatcher(kws)
        print(f"Keyword filter ENABLED: {len(kws)} keywords "
              f"(matched_keywords column populated).")
    sink = ResultSink(config, matcher=matcher)

    async with make_session(config) as session:
        # --dry-run short-circuits everything else (state inspection only).
        if args.dry_run:
            if not args.state:
                print("--dry-run requires --state <name>")
                return
            await gepnic.dry_run_state(session, config, args.state)
            return

        want = args.source
        if want in ("all", "gem"):
            print("\n=== GeM ===")
            if matcher is not None:
                await gem_scraper.scrape_gem_keywords(
                    session, config, sink, matcher.keywords,
                    pages_per_keyword=args.gem_pages_per_keyword)
            else:
                await gem_scraper.scrape_gem(session, config, sink, args.max_pages)
        if want in ("all", "cppp"):
            print("\n=== CPPP ===")
            await cppp_scraper.scrape_cppp(session, config, sink, args.max_pages)
        if want in ("all", "states"):
            print("\n=== State portals (Gujarat, Haryana) ===")
            portals = ([args.state] if args.state
                       else list(state_portals.STATE_ASPNET_PORTALS))
            for key in portals:
                await state_portals.scrape_state_portal(
                    session, config, sink, key, args.max_pages)
        if want in ("all", "psus"):
            print("\n=== PSU portals (GAIL, Coal India, NTPC, BHEL) ===")
            psus = ([args.state] if args.state
                    else list(psu_portals.PSU_PORTALS))
            for key in psus:
                await psu_portals.PSU_PORTALS[key](
                    session, config, sink, args.max_pages)
        if want in ("tsap", "playwright"):  # optional: needs Playwright
            import playwright_states
            print("\n=== Playwright portals (TS / AP / BPCL) ===")
            if args.state:
                keys = [args.state]
            elif want == "tsap":
                keys = list(playwright_states.TSAP_PORTALS)
            else:
                keys = playwright_states.PLAYWRIGHT_PORTALS
            for key in keys:
                await playwright_states.run_playwright_portal(
                    config, sink, key, args.max_pages)
        if want in ("gepnic",):  # NIC GePNIC states are CAPTCHA-gated; stub only
            print("\n=== State GePNIC (stub) ===")
            states = [args.state] if args.state else list(gepnic.STATE_PORTALS)
            for st in states:
                await gepnic.scrape_state(session, config, sink, st, args.max_pages)

        await sink.finalize()


def parse_args():
    p = argparse.ArgumentParser(description="GeM + CPPP + state GePNIC tender scraper")
    p.add_argument("--source",
                   choices=["all", "gem", "cppp", "states", "psus", "tsap",
                            "playwright", "gepnic"],
                   default="all",
                   help="which source(s): all | gem | cppp | states "
                        "(Gujarat/Haryana) | psus (GAIL/CoalIndia/NTPC/BHEL) | "
                        "tsap (Telangana/AP) | playwright (TS/AP/BPCL) | gepnic (stub)")
    p.add_argument("--state",
                   help="single portal key for --source states (gujarat|haryana) "
                        "or state name for --source gepnic / --dry-run")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch one page of a state and print its structure")
    p.add_argument("--max-pages", type=int, default=None,
                   help="cap listing pages per source (CPPP full set ~3450 pages)")
    p.add_argument("--no-filter", action="store_true",
                   help="disable keyword filtering (scrape everything)")
    p.add_argument("--keywords-file",
                   help="file with one keyword per line (default: built-in IT/AI list)")
    p.add_argument("--gem-pages-per-keyword", type=int, default=2000,
                   help="runaway safety backstop for GeM pages/keyword. Not a real "
                        "limit: we scan only OPEN bids, so keyword sets are small "
                        "and fully covered well under this.")
    p.add_argument("--concurrency", type=int, default=60)
    p.add_argument("--rate-limit", type=float, default=0.25,
                   help="polite delay between requests (seconds)")
    p.add_argument("--checkpoint-every", type=int, default=20)
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                   help="output directory (default: tender_scraper/outputs, "
                        "regardless of current working directory)")
    p.add_argument("--no-resume", action="store_true",
                   help="ignore existing checkpoints and start fresh")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is checkpointed; re-run to resume.")
