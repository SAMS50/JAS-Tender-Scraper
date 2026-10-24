
"""
JAS Tender Scraper — V8.1 Controlled BOQ Title Promotion

Inputs:
  tender_metadata_batch_v8.csv
  tender_boq_candidates_v8.csv

Outputs:
  tender_metadata_batch_v8_1.csv
  tender_title_promotion_audit_v8_1.csv
  tender_title_promotion_summary_v8_1.json

Never modifies the input files.
Never invents or changes tender IDs.
Never overwrites an existing title.
"""

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

METADATA_INPUT = ROOT / "tender_metadata_batch_v8.csv"
CANDIDATES_INPUT = ROOT / "tender_boq_candidates_v8.csv"

METADATA_OUTPUT = ROOT / "tender_metadata_batch_v8_1.csv"
AUDIT_OUTPUT = ROOT / "tender_title_promotion_audit_v8_1.csv"
SUMMARY_OUTPUT = ROOT / "tender_title_promotion_summary_v8_1.json"

EXTRA_FIELDS = [
    "v8_1_title_source",
    "v8_1_title_verification",
    "v8_1_title_evidence",
    "v8_1_title_document",
    "v8_1_title_sheet",
    "v8_1_title_row",
    "v8_1_title_promotion_status",
]

AUDIT_FIELDS = [
    "zip_filename",
    "tender_id",
    "tender_id_status",
    "old_title",
    "candidate_title",
    "new_title",
    "decision",
    "reason",
    "boq_filename",
    "sheet",
    "row_number",
    "evidence",
]

OFFICIAL_ID_PATTERN = re.compile(
    r"^20\d{2}_[A-Za-z0-9]+_\d+_\d+$"
)


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_title(value):
    """
    Only removes surrounding quotes and whitespace.
    Does not rewrite spelling, punctuation, or source text.
    """
    value = normalize(value)

    while value and value[0] in "\"'“”":
        value = value[1:].strip()

    while value and value[-1] in "\"'“”":
        value = value[:-1].strip()

    return value


def valid_mapped_identity(row):
    return (
        normalize(row.get("tender_id_status")).lower()
        == "mapped"
        and bool(
            OFFICIAL_ID_PATTERN.fullmatch(
                normalize(row.get("tender_id"))
            )
        )
    )


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing required input: {path.name}"
        )

    with path.open(
        "r", newline="", encoding="utf-8-sig"
    ) as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"No CSV header: {path.name}")

        return list(reader), list(reader.fieldnames)


def write_csv(path, rows, fields):
    with path.open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def audit_entry(row, candidate, decision, reason):
    candidate = candidate or {}

    return {
        "zip_filename": row.get("zip_filename", ""),
        "tender_id": row.get("tender_id", ""),
        "tender_id_status": row.get(
            "tender_id_status", ""
        ),
        "old_title": row.get("tender_title", ""),
        "candidate_title": candidate.get(
            "candidate_value", ""
        ),
        "new_title": row.get("tender_title", ""),
        "decision": decision,
        "reason": reason,
        "boq_filename": candidate.get(
            "boq_filename", ""
        ),
        "sheet": candidate.get("sheet", ""),
        "row_number": candidate.get(
            "row_number", ""
        ),
        "evidence": candidate.get("evidence", ""),
    }


def promote(metadata_rows, candidates):
    """
    Returns new metadata and complete decision audit.

    A title is promoted only when:
    - Existing title is empty
    - Tender ID is mapped and well-formed
    - Exactly one distinct actionable title exists
    - Candidate came from an inline Name of Work label
    - Evidence contains the candidate title
    """
    by_zip = defaultdict(list)

    for candidate in candidates:
        zip_name = normalize(
            candidate.get("zip_filename")
        )
        if zip_name:
            by_zip[zip_name].append(candidate)

    output = []
    audit = []

    for original in metadata_rows:
        row = dict(original)

        for field in EXTRA_FIELDS:
            row[field] = ""

        zip_name = normalize(
            row.get("zip_filename")
        )

        matches = by_zip.get(zip_name, [])

        actionable = [
            c for c in matches
            if normalize(
                c.get("review_status")
            ) == "NEEDS_REVIEW"
            and normalize(
                c.get("candidate_field")
            ) == "tender_title"
        ]

        if normalize(row.get("tender_title")):
            row["v8_1_title_promotion_status"] = (
                "EXISTING_TITLE_PRESERVED"
            )
            row["v8_1_title_source"] = normalize(
                row.get("title_source")
            ) or "V5_BASELINE"

            audit.append(
                audit_entry(
                    row,
                    actionable[0] if actionable else None,
                    "EXISTING_TITLE_PRESERVED",
                    "Existing title cannot be overwritten",
                )
            )
            output.append(row)
            continue

        if not actionable:
            row["v8_1_title_promotion_status"] = (
                "NO_ACTIONABLE_CANDIDATE"
            )
            audit.append(
                audit_entry(
                    row,
                    None,
                    "NO_ACTIONABLE_CANDIDATE",
                    "No V8 title awaiting review",
                )
            )
            output.append(row)
            continue

        if not valid_mapped_identity(row):
            row["v8_1_title_promotion_status"] = (
                "HELD_IDENTITY_REVIEW"
            )
            for candidate in actionable:
                audit.append(
                    audit_entry(
                        row,
                        candidate,
                        "HELD_IDENTITY_REVIEW",
                        "Missing or unverified tender ID mapping",
                    )
                )
            output.append(row)
            continue

        distinct = {}

        for candidate in actionable:
            title = clean_title(
                candidate.get("candidate_value")
            )
            if title:
                distinct.setdefault(
                    title.casefold(), []
                ).append(candidate)

        if len(distinct) != 1:
            row["v8_1_title_promotion_status"] = (
                "HELD_CONFLICTING_CANDIDATES"
            )
            for candidate in actionable:
                audit.append(
                    audit_entry(
                        row,
                        candidate,
                        "HELD_CONFLICTING_CANDIDATES",
                        "No unique title candidate",
                    )
                )
            output.append(row)
            continue

        candidate = next(
            iter(distinct.values())
        )[0]

        title = clean_title(
            candidate.get("candidate_value")
        )
        evidence = normalize(
            candidate.get("evidence")
        )

        # Explicit source evidence is mandatory.
        # The label must be visible in the source row.
        has_label = bool(
            re.search(
                r"name\s+of\s+work\s*:",
                evidence,
                re.I,
            )
        )

        # Compare cleaned text without quotation marks.
        evidence_clean = clean_title(
            re.sub(
                r"^\s*name\s+of\s+work\s*:\s*[-:]?\s*",
                "",
                evidence,
                flags=re.I,
            )
        )

        evidence_matches = (
            title.casefold()
            in evidence_clean.casefold()
        )

        if (
            normalize(candidate.get("candidate_type"))
            != "explicit_label_inline"
            or not has_label
            or not evidence_matches
            or len(title) < 25
        ):
            row["v8_1_title_promotion_status"] = (
                "HELD_EVIDENCE_REVIEW"
            )
            audit.append(
                audit_entry(
                    row,
                    candidate,
                    "HELD_EVIDENCE_REVIEW",
                    "Candidate lacks qualifying inline evidence",
                )
            )
            output.append(row)
            continue

        # Controlled promotion.
        row["tender_title"] = title
        row["v8_1_title_source"] = (
            "BOQ_NAME_OF_WORK"
        )
        row["v8_1_title_verification"] = (
            "SOURCE_BACKED_PENDING_PORTAL_VERIFICATION"
        )
        row["v8_1_title_evidence"] = evidence
        row["v8_1_title_document"] = candidate.get(
            "boq_filename", ""
        )
        row["v8_1_title_sheet"] = candidate.get(
            "sheet", ""
        )
        row["v8_1_title_row"] = candidate.get(
            "row_number", ""
        )
        row["v8_1_title_promotion_status"] = (
            "PROMOTED_SOURCE_BACKED"
        )

        # Maintain the existing provenance field too.
        if "title_source" in row:
            row["title_source"] = "BOQ_NAME_OF_WORK"

        audit.append(
            audit_entry(
                row,
                candidate,
                "PROMOTED_SOURCE_BACKED",
                "Explicit BOQ Name of Work; portal verification pending",
            )
        )

        output.append(row)

    return output, audit


def main():
    metadata, metadata_fields = read_csv(
        METADATA_INPUT
    )
    candidates, _ = read_csv(
        CANDIDATES_INPUT
    )

    required_metadata = {
        "zip_filename",
        "tender_id",
        "tender_id_status",
        "tender_title",
    }

    required_candidates = {
        "zip_filename",
        "candidate_field",
        "candidate_value",
        "candidate_type",
        "review_status",
        "evidence",
    }

    if not required_metadata.issubset(
        metadata_fields
    ):
        raise ValueError(
            "V8 metadata is missing required columns"
        )

    if candidates and not required_candidates.issubset(
        candidates[0]
    ):
        raise ValueError(
            "V8 candidates are missing required columns"
        )

    # Reject ambiguous source identities.
    zip_names = [
        normalize(row["zip_filename"])
        for row in metadata
    ]

    if len(zip_names) != len(set(zip_names)):
        raise ValueError(
            "Duplicate ZIP records in metadata input"
        )

    output, audit = promote(
        metadata, candidates
    )

    # Protect baseline identifiers.
    for before, after in zip(metadata, output):
        for field in (
            "zip_filename",
            "tender_id",
            "tender_id_status",
        ):
            if before.get(field) != after.get(field):
                raise AssertionError(
                    f"Protected field changed: {field}"
                )

    fields = list(
        dict.fromkeys(
            metadata_fields + EXTRA_FIELDS
        )
    )

    write_csv(
        METADATA_OUTPUT,
        output,
        fields,
    )
    write_csv(
        AUDIT_OUTPUT,
        audit,
        AUDIT_FIELDS,
    )

    counts = Counter(
        row["v8_1_title_promotion_status"]
        for row in output
    )

    summary = {
        "version": "v8.1",
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "tender_count": len(output),
        "titles_before": sum(
            bool(normalize(r.get("tender_title")))
            for r in metadata
        ),
        "titles_after": sum(
            bool(normalize(r.get("tender_title")))
            for r in output
        ),
        "promoted": counts[
            "PROMOTED_SOURCE_BACKED"
        ],
        "identity_review": counts[
            "HELD_IDENTITY_REVIEW"
        ],
        "status_counts": dict(counts),
    }

    SUMMARY_OUTPUT.write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("=" * 65)
    print("JAS V8.1 — CONTROLLED TITLE PROMOTION")
    print("=" * 65)

    print("Tender records:", len(output))
    print(
        "Titles before:",
        summary["titles_before"],
    )
    print(
        "Titles after:",
        summary["titles_after"],
    )
    print(
        "Promoted:",
        summary["promoted"],
    )
    print(
        "Held for identity review:",
        summary["identity_review"],
    )

    print("\nDecisions:")
    for status, count in counts.items():
        print(f"  {status}: {count}")

    print("\nOutputs:")
    print(METADATA_OUTPUT.name)
    print(AUDIT_OUTPUT.name)
    print(SUMMARY_OUTPUT.name)


if __name__ == "__main__":
    main()
