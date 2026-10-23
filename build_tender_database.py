
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

BASE = Path(__file__).resolve().parent

METADATA_FILE = BASE / "tender_metadata.csv"
ELIGIBILITY_FILE = BASE / "tender_eligibility.json"
BOQ_FILE = BASE / "boq_items_enriched.csv"

OUTPUT_JSON = BASE / "tender_database.json"
OUTPUT_CSV = BASE / "tender_database.csv"

# Only build records for the two tested tenders.
TEST_ZIPS = {
    "work_341915.zip",
    "work_343127.zip",
}


# ============================================================
# HELPERS
# ============================================================

def read_csv(path):
    if not path.exists():
        print(f"WARNING: Missing file: {path.name}")
        return []

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        return list(csv.DictReader(file))


def read_json(path):
    if not path.exists():
        print(f"WARNING: Missing file: {path.name}")
        return []

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        return [data]

    raise ValueError(
        f"Unexpected JSON format in {path.name}"
    )


def normalize(value):
    return str(value or "").strip()


def is_missing(value):
    return value is None or (
        isinstance(value, str)
        and not value.strip()
    )


def compact_metadata(row):
    # Preserve all metadata fields so no source
    # columns are silently discarded.
    return {
        key: value
        for key, value in row.items()
        if key is not None
    }


def choose_tender_id(metadata, eligibility):
    metadata_id = normalize(
        metadata.get("tender_id")
    )
    eligibility_id = normalize(
        eligibility.get("tender_id")
    )

    if metadata_id and eligibility_id:
        if metadata_id != eligibility_id:
            return "", (
                "Metadata and eligibility tender IDs conflict"
            )

    tender_id = metadata_id or eligibility_id

    if not tender_id:
        return "", "Tender ID is missing"

    return tender_id, None


def get_boq_tender_id(row):
    # Different pipeline versions may use
    # slightly different tender ID headers.
    for column in [
        "tender_id",
        "resolved_tender_id",
        "matched_tender_id",
    ]:
        value = normalize(row.get(column))
        if value:
            return value

    return ""


def get_boq_zip(row):
    for column in [
        "zip_filename",
        "source_zip",
    ]:
        value = normalize(row.get(column))
        if value:
            return value

    return ""


# ============================================================
# BUILD DATABASE
# ============================================================

def build_database():
    metadata_rows = read_csv(METADATA_FILE)
    eligibility_rows = read_json(ELIGIBILITY_FILE)
    boq_rows = read_csv(BOQ_FILE)

    metadata_by_zip = defaultdict(list)
    eligibility_by_zip = defaultdict(list)

    for row in metadata_rows:
        zip_name = normalize(
            row.get("zip_filename")
        )
        if zip_name in TEST_ZIPS:
            metadata_by_zip[zip_name].append(row)

    for row in eligibility_rows:
        zip_name = normalize(
            row.get("zip_filename")
        )
        if zip_name in TEST_ZIPS:
            eligibility_by_zip[zip_name].append(row)

    records = []
    all_warnings = []

    for zip_name in sorted(TEST_ZIPS):
        warnings = []

        metadata_matches = metadata_by_zip[zip_name]
        eligibility_matches = eligibility_by_zip[zip_name]

        if len(metadata_matches) > 1:
            warnings.append(
                "Multiple metadata records for ZIP"
            )

        if len(eligibility_matches) > 1:
            warnings.append(
                "Multiple eligibility records for ZIP"
            )

        metadata = (
            metadata_matches[0]
            if metadata_matches else {}
        )
        eligibility = (
            eligibility_matches[0]
            if eligibility_matches else {}
        )

        if not metadata:
            warnings.append(
                "Metadata record missing"
            )

        if not eligibility:
            warnings.append(
                "Eligibility record missing"
            )

        tender_id, id_warning = choose_tender_id(
            metadata,
            eligibility,
        )

        if id_warning:
            warnings.append(id_warning)

        # Only link BOQ items when tender identity
        # is unambiguous.
        matching_boq = []

        if tender_id and not id_warning:
            for row in boq_rows:
                boq_zip = get_boq_zip(row)
                boq_id = get_boq_tender_id(row)

                if boq_zip == zip_name:
                    if boq_id == tender_id:
                        matching_boq.append(row)

                    elif boq_id:
                        warnings.append(
                            "BOQ tender ID conflict: "
                            f"{boq_id}"
                        )

                elif not boq_zip and boq_id == tender_id:
                    # Fallback only when BOQ ZIP is
                    # unavailable but tender ID matches.
                    matching_boq.append(row)

        warnings = list(dict.fromkeys(warnings))

        record = {
            "zip_filename": zip_name,
            "tender_id": tender_id,
            "metadata": compact_metadata(metadata),
            "eligibility": eligibility,
            "boq_item_count": len(matching_boq),
            "boq_items": matching_boq,
            "warnings": warnings,
            "review_required": (
                bool(warnings)
                or eligibility.get(
                    "review_required", True
                )
            ),
        }

        records.append(record)

        for warning in warnings:
            all_warnings.append({
                "zip_filename": zip_name,
                "warning": warning,
            })

    return records, all_warnings


# ============================================================
# EXPORT
# ============================================================

def export_database(records):
    payload = {
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "record_count": len(records),
        "records": records,
    }

    with OUTPUT_JSON.open(
        "w", encoding="utf-8"
    ) as file:
        json.dump(
            payload,
            file,
            indent=2,
            ensure_ascii=False,
        )

    columns = [
        "zip_filename",
        "tender_id",
        "organization",
        "tender_title",
        "estimated_value_inr",
        "emd_inr",
        "tender_fee_inr",
        "contract_duration",
        "experience_period_years",
        "experience_options",
        "similar_work_definition",
        "minimum_average_turnover_inr",
        "turnover_period_years",
        "minimum_net_worth_inr",
        "joint_venture_allowed",
        "boq_item_count",
        "review_required",
        "warnings",
    ]

    with OUTPUT_CSV.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
        )
        writer.writeheader()

        for record in records:
            metadata = record["metadata"]
            eligibility = record["eligibility"]

            row = {
                "zip_filename": record["zip_filename"],
                "tender_id": record["tender_id"],
                "organization": metadata.get(
                    "organization", ""
                ),
                "tender_title": metadata.get(
                    "tender_title", ""
                ),
                "estimated_value_inr": metadata.get(
                    "estimated_value_inr", ""
                ),
                "emd_inr": metadata.get(
                    "emd_inr", ""
                ),
                "tender_fee_inr": metadata.get(
                    "tender_fee_inr", ""
                ),
                "contract_duration": metadata.get(
                    "contract_duration", ""
                ),
                "experience_period_years": eligibility.get(
                    "experience_period_years"
                ),
                "experience_options": json.dumps(
                    [
                        {
                            "number_of_projects": item.get(
                                "number_of_projects"
                            ),
                            "minimum_value_each_inr": item.get(
                                "minimum_value_each_inr"
                            ),
                        }
                        for item in eligibility.get(
                            "experience_options", []
                        )
                    ],
                    ensure_ascii=False,
                ),
                "similar_work_definition": eligibility.get(
                    "similar_work_definition", ""
                ),
                "minimum_average_turnover_inr": eligibility.get(
                    "minimum_average_turnover_inr"
                ),
                "turnover_period_years": eligibility.get(
                    "turnover_period_years"
                ),
                "minimum_net_worth_inr": eligibility.get(
                    "minimum_net_worth_inr"
                ),
                "joint_venture_allowed": eligibility.get(
                    "joint_venture_allowed"
                ),
                "boq_item_count": record["boq_item_count"],
                "review_required": record["review_required"],
                "warnings": " | ".join(
                    record["warnings"]
                ),
            }

            writer.writerow(row)


# ============================================================
# MAIN
# ============================================================

def main():
    records, warnings = build_database()

    export_database(records)

    print("\n" + "=" * 60)
    print("JAS UNIFIED TENDER DATABASE")
    print("=" * 60)

    print(f"Total tender records: {len(records)}")

    for record in records:
        metadata = record["metadata"]
        eligibility = record["eligibility"]

        print("\n" + "-" * 60)
        print(f"ZIP: {record['zip_filename']}")
        print(f"Tender ID: {record['tender_id']}")
        print(
            "Organization: "
            f"{metadata.get('organization', '')}"
        )
        print(
            "Title: "
            f"{metadata.get('tender_title', '')}"
        )
        print(
            "Estimated Value: "
            f"{metadata.get('estimated_value_inr', '')}"
        )
        print(
            "Experience Options: "
            f"{len(eligibility.get('experience_options', []))}"
        )
        print(
            f"BOQ Items: {record['boq_item_count']}"
        )
        print(
            f"Review Required: {record['review_required']}"
        )

        if record["warnings"]:
            print("Warnings:")
            for warning in record["warnings"]:
                print(f"  - {warning}")

    print("\n" + "=" * 60)
    print(f"Total warnings: {len(warnings)}")
    print(f"Saved: {OUTPUT_JSON.name}")
    print(f"Saved: {OUTPUT_CSV.name}")
    print("=" * 60)


if __name__ == "__main__":
    main()
