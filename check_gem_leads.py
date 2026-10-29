"""Check three pilot GeM leads, retaining evidence and honest unknowns.

Uses the existing scraper's public ongoing-bid search. A missing search match
never means closed or awarded. Uploaded PDFs are historical evidence only.
"""
import argparse
import csv
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor, Request
from http.cookiejar import CookieJar

import pymupdf

BASE = "https://bidplus.gem.gov.in"
IST = timezone(timedelta(hours=5, minutes=30))
PILOT = [("GEM/2026/B/8128667", "9998472"),
         ("GEM/2026/B/8154781", "10028410"),
         ("GEM/2026/B/8154474", "10028087")]
FIELDS = ["tender_id", "checked_at", "procurement_status", "procurement_basis",
          "award_status", "award_check_status", "supplier_name", "event_at",
          "publication_date_document", "latest_deadline", "document_deadline",
          "deadline_check_status", "estimated_bid_value_inr", "budget_check_status",
          "emd_inr", "financial_notes", "source_url", "pdf_source", "pdf_sha256",
          "date_evidence", "budget_evidence", "live_search_status", "error",
          "timezone_note", "manual_next_step"]


def scalar(doc, key):
    v = doc.get(key, "")
    return str(v[0] if isinstance(v, list) and v else v or "")


def parse_portal_time(raw):
    """Do not silently reinterpret portal Z timestamps as verified UTC."""
    if not raw:
        return ""
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return raw


def inspect_pdf(path, tender_id):
    result = {"pdf_source": str(path), "budget_check_status": "NOT_PUBLISHED_IN_MAIN_PDF"}
    with pymupdf.open(path) as doc:
        pages = [p.get_text() for p in doc]
    text = "\n".join(pages)
    if tender_id not in text:
        raise ValueError("PDF bid number does not match requested tender")
    result["pdf_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    m = re.search(r"Bid End Date/Time\s*(\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2})", text)
    if m:
        dt = datetime.strptime(m[1], "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)
        result["document_deadline"] = dt.isoformat()
        result["date_evidence"] = "Main bid p1: " + m[0]
    m = re.search(r"/Dated:\s*(\d{2}-\d{2}-\d{4})", text)
    if m:
        result["publication_date_document"] = datetime.strptime(m[1], "%d-%m-%Y").date().isoformat()
    # Only a numeric line immediately after an explicit estimate heading.
    # Boilerplate references to estimated value and EMD do not create a budget.
    candidates = []
    for page, t in enumerate(pages, 1):
        for m in re.finditer(r"Estimated\s+(?:Bid|Tender|Contract)\s+Value(?:\s+in\s+INR)?(?:\s*\(Inclusive of all\s+taxes\))?\s*\n\s*([\d,]+(?:\.\d+)?)\s*(?:\n|$)", t, re.I):
            amount = float(m[1].replace(",", ""))
            candidates.append((amount, page, m[0]))
    if len({x[0] for x in candidates}) == 1:
        amount, page, excerpt = candidates[0]
        if amount > 1:
            result.update(estimated_bid_value_inr=amount, budget_check_status="EXPLICIT_ESTIMATE",
                          budget_evidence=f"Main bid p{page}: {excerpt}")
        else:
            result.update(budget_check_status="PLACEHOLDER_REQUIRES_REVIEW", budget_evidence=excerpt)
    elif candidates:
        result["budget_check_status"] = "CONFLICTING_ESTIMATES_REVIEW"
    emds = re.findall(r"EMD Amount\s*\n\s*([\d,]+(?:\.\d+)?)\s*(?:\n|$)", text)
    if len(emds) == 1:
        result["emd_inr"] = float(emds[0].replace(",", ""))
    elif "EMD Amount (In" in text:
        result["financial_notes"] = "Group-specific EMD: inspect main PDF; do not aggregate or use as budget."
    return result


class PublicGeM:
    def __init__(self, timeout=20):
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.timeout = timeout
        self.csrf = ""

    def request(self, url, body=None, headers=None):
        req = Request(url, data=body, headers={"User-Agent": "JAS-Tender-Research/1.0", **(headers or {})})
        with self.opener.open(req, timeout=self.timeout) as response:
            content = response.read(25 * 1024 * 1024 + 1)
        if len(content) > 25 * 1024 * 1024:
            raise ValueError("Response exceeds 25 MiB limit")
        return content

    def search(self, bid):
        if not self.csrf:
            html = self.request(BASE + "/all-bids").decode("utf-8", "replace")
            m = re.search(r"csrf_bd_gem_nk['\"]?\s*[:=]\s*['\"]([0-9a-fA-F]+)['\"]", html)
            if not m:
                raise ValueError("Public search token unavailable; login/challenge/page change may require manual check")
            self.csrf = m[1]
        payload = {"page": 1, "param": {"searchBid": bid, "searchType": "fullText"},
                   "filter": {"bidStatusType": "ongoing_bids", "byType": "all", "highBidValue": "",
                              "byEndDate": {"from": "", "to": ""}, "sort": "Bid-End-Date-Latest"}}
        raw = self.request(BASE + "/all-bids-data",
                           urlencode({"payload": json.dumps(payload), "csrf_bd_gem_nk": self.csrf}).encode(),
                           {"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/all-bids"})
        data = json.loads(raw)
        response = data.get("response", {}).get("response", {})
        if not isinstance(response.get("docs"), list):
            raise ValueError("Unexpected search response; cannot conclude no match")
        return next((d for d in response["docs"] if scalar(d, "b_bid_number").strip() == bid), None), data


def apply_live_match(row, match):
    if match is None:
        row["live_search_status"] = "NO_EXACT_MATCH_IN_ONGOING_SEARCH"
        return
    row.update(procurement_status="OPEN", procurement_basis="Exact match in public ongoing-bid search at checked_at",
               live_search_status="EXACT_ONGOING_MATCH")
    row["latest_deadline"] = parse_portal_time(scalar(match, "final_end_date_sort"))
    row["event_at"] = parse_portal_time(scalar(match, "final_start_date_sort"))
    row["deadline_check_status"] = "LIVE_LISTING_TIMESTAMP_TIMEZONE_REQUIRES_REVIEW" if row["latest_deadline"] else "LIVE_DATE_UNAVAILABLE"


def run(args):
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    client = PublicGeM(args.timeout)
    network_error = ""
    rows = []
    for bid, document_id in PILOT:
        row = {k: "" for k in FIELDS}
        row.update(tender_id=bid, checked_at=datetime.now(timezone.utc).isoformat(),
                   source_url=BASE + "/showbidDocument/" + document_id,
                   award_check_status="NOT_VERIFIED_RESULTS_LOOKUP_REQUIRED",
                   deadline_check_status="DOCUMENT_ONLY_NOT_LIVE_VERIFIED",
                   timezone_note="PDF times assumed Asia/Kolkata (+05:30). Portal Z timestamps preserved but require validation.",
                   manual_next_step="Check exact bid updates/cancellations and results/contracts. No ongoing match is not proof of closure or award.")
        if not args.offline and not network_error:
            try:
                match, raw = client.search(bid)
                (out / (document_id + "_search.json")).write_text(json.dumps(raw, indent=2), encoding="utf-8")
                apply_live_match(row, match)
            except Exception as exc:
                row["error"] = str(exc)
                row["live_search_status"] = "REQUEST_FAILED"
                # Shared connectivity failures affect all bids; do not repeat them.
                network_error = str(exc)
        elif args.offline:
            row["live_search_status"] = "OFFLINE_NOT_CHECKED"
        else:
            row["live_search_status"] = "SKIPPED_AFTER_SHARED_REQUEST_FAILURE"
            row["error"] = network_error
        path = Path(args.pdf_dir) / ("GeM-Bidding-" + document_id + ".pdf") if args.pdf_dir else out / ("GeM-Bidding-" + document_id + ".pdf")
        if not path.is_file() and not args.offline and not network_error:
            try:
                blob = client.request(row["source_url"])
                if not blob.startswith(b"%PDF"):
                    raise ValueError("Bid download returned non-PDF; manual session check needed")
                path = out / ("GeM-Bidding-" + document_id + ".pdf")
                path.write_bytes(blob)
            except Exception as exc:
                row["error"] += " | PDF download: " + str(exc)
        if path.is_file():
            try:
                row.update(inspect_pdf(path, bid))
            except Exception as exc:
                row["error"] += " | PDF extraction: " + str(exc)
        else:
            row["budget_check_status"] = "PDF_UNAVAILABLE"
        rows.append(row)
        print(bid, row["live_search_status"], row["budget_check_status"])
        time.sleep(0 if args.offline else 1)
    with (out / "three_lead_checks.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (out / "three_lead_checks.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("Saved:", out / "three_lead_checks.csv")
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pdf-dir", help="Folder containing existing GeM-Bidding PDFs")
    p.add_argument("--out-dir", default="outputs/three_lead_checks")
    p.add_argument("--offline", action="store_true", help="Extract existing PDFs without network")
    p.add_argument("--timeout", type=int, default=20)
    run(p.parse_args())
