
"""
JAS Tender Scraper — V7 Document-Aware OCR

Requires:
    tender_pdf_extractor_v5.py
    tender_pdf_extractor_v6.py

Preserves V5/V6 output files.
Does not automatically verify or promote OCR candidates.
"""

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import pymupdf

import tender_pdf_extractor_v5 as v5
import tender_pdf_extractor_v6 as v6


ROOT = Path(__file__).resolve().parent
ZIP_DIR = ROOT / "downloaded_tenders"

OUTPUT = ROOT / "tender_metadata_batch_v7.csv"
REVIEW = ROOT / "tender_ocr_review_v7.csv"
AUDIT = ROOT / "tender_metadata_v7_audit.json"

# These are confirmed SHA-256 prefixes from your ZIPs.
# Hashes are used only to classify shared documents.
SHARED_HASHES = {
    "253f3751960a772a": "shared_circular",
    "03b049531944a625": "shared_general_conditions",
    "467343d85e9b47a5": "shared_conditions",
}

# Explicitly reject boilerplate-style titles.
BAD_TITLE_PATTERNS = [
    r"contractual\s+cost",
    r"funding\s+of\s+project",
    r"name\s+of\s+contractor\s+with\s+mobile",
    r"signature\s+of\s+contractor",
    r"general\s+conditions\s+of\s+contract",
]

EXTRA_FIELDS = [
    "v7_documents_scanned",
    "v7_documents_skipped",
    "v7_ocr_pages_attempted",
    "v7_ocr_pages_with_text",
    "v7_candidate_count",
    "v7_review_status",
    "v7_document_audit_json",
    "v7_errors_json",
]


def sha256_digest(data):
    return hashlib.sha256(data).hexdigest()


def classify_document(filename, digest):
    """
    Classify PDFs using verified hashes and filenames.
    A matching shared hash is excluded from tender-specific OCR.
    """
    if digest[:16] in SHARED_HASHES:
        return SHARED_HASHES[digest[:16]]

    name = Path(filename).name.lower()

    if re.search(r"gcc|general.conditions", name):
        return "generic_by_filename_review"

    if re.search(r"circular", name):
        return "generic_by_filename_review"

    if re.search(
        r"conditions|condition|nit|tender|schedule|work",
        name,
    ):
        return "tender_specific"

    if re.match(r"17_\d+\.pdf$", name):
        return "tender_specific"

    return "unclassified"


def should_scan(category):
    """
    Only known shared documents are skipped automatically.
    Unknown files remain eligible for OCR.
    """
    return not (
        category.startswith("shared_")
        or category == "generic_by_filename_review"
    )


def valid_title(title):
    title = v6.normalize(title)

    if not v5.title_looks_valid(title):
        return False

    if any(
        re.search(pattern, title, re.I)
        for pattern in BAD_TITLE_PATTERNS
    ):
        return False

    return True


def filter_candidates(candidates, base_row):
    """
    Reject obvious boilerplate and duplicate candidates.

    Financial values and IDs are always kept as review-only
    candidates, never promoted automatically.
    """
    filtered = []
    seen = set()

    for item in candidates:
        field = item["candidate_field"]
        value = v6.normalize(item["candidate_value"])

        if field == "tender_title" and not valid_title(value):
            continue

        if not value:
            continue

        # Don't repeat information already present in V5.
        if field in base_row and base_row[field]:
            if field not in (
                "estimated_value_inr",
                "emd_inr",
                "tender_fee_inr",
                "tender_id",
            ):
                continue

            if str(base_row[field]) == value:
                continue

        key = (field, value.casefold())

        if key in seen:
            continue

        seen.add(key)

        item = dict(item)
        item["review_status"] = "NEEDS_REVIEW"
        filtered.append(item)

    return filtered


def inspect_zip(name, base_row):
    path = ZIP_DIR / name

    candidates = []
    document_audit = []
    errors = []

    scanned = 0
    skipped = 0
    ocr_attempted = 0
    ocr_with_text = 0

    if not path.exists():
        return {
            "v7_documents_scanned": 0,
            "v7_documents_skipped": 0,
            "v7_ocr_pages_attempted": 0,
            "v7_ocr_pages_with_text": 0,
            "v7_candidate_count": 0,
            "v7_review_status": "ZIP_MISSING",
            "v7_document_audit_json": "[]",
            "v7_errors_json": json.dumps(["ZIP not found"]),
        }, []

    try:
        with ZipFile(path) as archive:
            members = [
                member
                for member in archive.namelist()
                if member.lower().endswith(".pdf")
            ]

            # Prioritize tender-specific documents.
            documents = []

            for member in members:
                try:
                    data = archive.read(member)
                    digest = sha256_digest(data)
                    category = classify_document(member, digest)

                    priority = (
                        0 if category == "tender_specific"
                        else 1 if should_scan(category)
                        else 2
                    )

                    documents.append(
                        (priority, member, data, digest, category)
                    )
                except Exception as exc:
                    errors.append(f"{member}: {exc}")

            documents.sort(key=lambda item: (item[0], item[1]))

            if not documents:
                errors.append("No PDF documents")

            for _, member, data, digest, category in documents:
                filename = Path(member).name

                record = {
                    "document": member,
                    "sha256": digest,
                    "category": category,
                    "decision": "",
                    "ocr_pages": 0,
                    "candidate_count": 0,
                }

                if not should_scan(category):
                    skipped += 1
                    record["decision"] = "SKIPPED_SHARED_OR_GENERIC"
                    document_audit.append(record)
                    continue

                if ocr_attempted >= v6.MAX_OCR_PAGES_PER_ZIP:
                    record["decision"] = "SKIPPED_OCR_LIMIT"
                    document_audit.append(record)
                    continue

                scanned += 1
                record["decision"] = "SCANNED"

                try:
                    with pymupdf.open(
                        stream=data,
                        filetype="pdf",
                    ) as pdf:
                        page_limit = min(
                            len(pdf),
                            v6.MAX_PAGES_PER_PDF,
                        )

                        for page_index in range(page_limit):
                            if (
                                ocr_attempted
                                >= v6.MAX_OCR_PAGES_PER_ZIP
                            ):
                                break

                            page = pdf[page_index]
                            native_text = page.get_text()

                            # V7 only adds OCR evidence.
                            # V5 handles normal readable text.
                            if not v6.is_low_text(native_text):
                                continue

                            ocr_attempted += 1
                            record["ocr_pages"] += 1

                            try:
                                text = v6.perform_ocr(page)

                                if not v6.normalize(text):
                                    continue

                                ocr_with_text += 1

                                found = v6.extract_ocr_candidates(
                                    name,
                                    base_row,
                                    filename,
                                    page_index + 1,
                                    text,
                                )

                                found = filter_candidates(
                                    found,
                                    base_row,
                                )

                                candidates.extend(found)
                                record["candidate_count"] += len(found)

                            except Exception as exc:
                                errors.append(
                                    f"{filename}:page{page_index + 1}: "
                                    f"{exc}"
                                )

                except Exception as exc:
                    errors.append(f"{filename}: {exc}")

                document_audit.append(record)

    except Exception as exc:
        errors.append(f"ZIP error: {exc}")

    # Deduplicate across pages/documents while retaining
    # the first evidence source in document priority order.
    candidates = filter_candidates(candidates, base_row)

    status = (
        "NEEDS_REVIEW" if candidates
        else "OCR_ERROR" if errors
        else "NO_CANDIDATES_FOUND"
    )

    diagnostics = {
        "v7_documents_scanned": scanned,
        "v7_documents_skipped": skipped,
        "v7_ocr_pages_attempted": ocr_attempted,
        "v7_ocr_pages_with_text": ocr_with_text,
        "v7_candidate_count": len(candidates),
        "v7_review_status": status,
        "v7_document_audit_json": json.dumps(
            document_audit,
            ensure_ascii=False,
        ),
        "v7_errors_json": json.dumps(
            errors,
            ensure_ascii=False,
        ),
    }

    return diagnostics, candidates


def main():
    if not v6.check_tesseract():
        raise RuntimeError("Tesseract OCR is unavailable")

    mappings = v5.load_mappings()

    metadata_rows = []
    review_rows = []
    audit_records = []

    print("=" * 70)
    print("JAS TENDER METADATA — V7 DOCUMENT-AWARE OCR")
    print("=" * 70)

    for index, name in enumerate(v5.BATCH, 1):
        base_row = v5.process_zip(name, mappings)
        diagnostics, candidates = inspect_zip(name, base_row)

        row = dict(base_row)
        row.update(diagnostics)

        metadata_rows.append(row)
        review_rows.extend(candidates)

        audit_records.append({
            "zip_filename": name,
            "tender_id": base_row["tender_id"],
            "tender_id_status": base_row["tender_id_status"],
            "diagnostics": diagnostics,
            "candidates": candidates,
        })

        print(
            f"[{index:02d}/{len(v5.BATCH)}] {name} | "
            f"Scanned: {diagnostics['v7_documents_scanned']} | "
            f"Skipped: {diagnostics['v7_documents_skipped']} | "
            f"OCR pages: {diagnostics['v7_ocr_pages_attempted']} | "
            f"Candidates: {len(candidates)}"
        )

    with OUTPUT.open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=v5.FIELDS + EXTRA_FIELDS,
        )
        writer.writeheader()
        writer.writerows(metadata_rows)

    with REVIEW.open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=v6.OCR_FIELDS,
        )
        writer.writeheader()
        writer.writerows(review_rows)

    coverage = {
        field: sum(bool(row[field]) for row in metadata_rows)
        for field in (
            "tender_id",
            "organization",
            "tender_title",
            "estimated_value_inr",
            "emd_inr",
            "contract_duration",
        )
    }

    audit = {
        "version": "v7",
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "tenders": len(metadata_rows),
        "field_coverage": coverage,
        "ocr_pages_attempted": sum(
            r["v7_ocr_pages_attempted"]
            for r in metadata_rows
        ),
        "ocr_candidates": len(review_rows),
        "review_status_counts": dict(
            Counter(
                r["v7_review_status"]
                for r in metadata_rows
            )
        ),
        "records": audit_records,
    }

    AUDIT.write_text(
        json.dumps(
            audit,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\nV7 SUMMARY")
    print("Tender ZIPs:", len(metadata_rows))
    print("OCR pages:", audit["ocr_pages_attempted"])
    print("OCR candidates:", audit["ocr_candidates"])
    print("Metadata coverage:", coverage)
    print("\nSaved:", OUTPUT.name)
    print("Saved:", REVIEW.name)
    print("Saved:", AUDIT.name)
    print("\nV5 and V6 files remain unchanged.")


if __name__ == "__main__":
    main()
