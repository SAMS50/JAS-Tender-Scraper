
import csv
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

BOQ_FILE = BASE_DIR / "boq_all_items.csv"
DOCUMENT_MAPPING_FILE = BASE_DIR / "tender_document_mapping.csv"
TENDER_MAPPING_FILE = BASE_DIR / "tender_zip_mapping.csv"
OUTPUT_FILE = BASE_DIR / "boq_items_enriched.csv"


def read_csv(path):
    with path.open(
        "r", newline="", encoding="utf-8-sig"
    ) as file:
        return list(csv.DictReader(file))


def normalize(value):
    return str(value or "").strip().lower()


def main():
    required_files = [
        BOQ_FILE,
        DOCUMENT_MAPPING_FILE,
        TENDER_MAPPING_FILE,
    ]

    for path in required_files:
        if not path.exists():
            print(f"ERROR: Missing {path.name}")
            return

    items = read_csv(BOQ_FILE)
    documents = read_csv(DOCUMENT_MAPPING_FILE)
    tenders = read_csv(TENDER_MAPPING_FILE)

    # Build a ZIP -> tender ID lookup using both
    # portal mappings and PDF-recovered document IDs.
    zip_ids = defaultdict(set)
    zip_portal_ids = defaultdict(set)
    zip_portals = defaultdict(set)

    for record in tenders + documents:
        zip_name = normalize(record.get("zip_filename"))

        if not zip_name:
            continue

        tender_id = str(
            record.get("tender_id") or ""
        ).strip()

        portal_tnid = str(
            record.get("portal_tnid") or ""
        ).strip()

        portal = str(
            record.get("portal") or ""
        ).strip()

        if tender_id:
            zip_ids[zip_name].add(tender_id)

        if portal_tnid:
            zip_portal_ids[zip_name].add(portal_tnid)

        if portal:
            zip_portals[zip_name].add(portal)

    output_rows = []

    for item in items:
        row = dict(item)

        zip_filename = str(
            item.get("zip_filename") or ""
        ).strip()

        zip_key = normalize(zip_filename)

        row["tender_id"] = ""
        row["portal"] = ""
        row["portal_tnid"] = ""
        row["mapping_status"] = "unmatched"

        if not zip_key:
            output_rows.append(row)
            continue

        ids = zip_ids.get(zip_key, set())
        portal_ids = zip_portal_ids.get(zip_key, set())
        portals = zip_portals.get(zip_key, set())

        if len(portal_ids) == 1:
            row["portal_tnid"] = next(iter(portal_ids))

        if len(portals) == 1:
            row["portal"] = next(iter(portals))

        if (
            len(ids) > 1
            or len(portal_ids) > 1
            or len(portals) > 1
        ):
            row["mapping_status"] = "ambiguous"

        elif len(ids) == 1:
            row["tender_id"] = next(iter(ids))
            row["mapping_status"] = "matched"

        else:
            row["mapping_status"] = "missing_tender_id"

        output_rows.append(row)

    columns = list(items[0].keys()) if items else [
        "source_file",
        "tender_id",
        "sheet",
        "item_number",
        "description",
        "quantity",
        "unit",
        "estimated_rate_inr",
        "estimated_amount_inr",
        "zip_filename",
        "original_filename",
        "archive_member",
    ]

    for column in [
        "portal",
        "portal_tnid",
        "mapping_status",
    ]:
        if column not in columns:
            columns.append(column)

    with OUTPUT_FILE.open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file, fieldnames=columns
        )
        writer.writeheader()
        writer.writerows(output_rows)

    counts = Counter(
        row["mapping_status"]
        for row in output_rows
    )

    print("\n" + "=" * 50)
    print("BOQ LINKING SUMMARY")
    print("=" * 50)

    print(f"Total BOQ items: {len(output_rows)}")

    for status in [
        "matched",
        "unmatched",
        "ambiguous",
        "missing_tender_id",
    ]:
        print(f"{status}: {counts[status]}")

    if output_rows:
        matching_rate = (
            counts["matched"] / len(output_rows)
        ) * 100

        print(f"Matching rate: {matching_rate:.2f}%")

    print(f"\nSaved to: {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
