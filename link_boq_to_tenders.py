
import csv
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

BOQ_FILE = BASE_DIR / "boq_all_items.csv"
DOCUMENT_MAPPING_FILE = BASE_DIR / "tender_document_mapping.csv"
TENDER_MAPPING_FILE = BASE_DIR / "tender_zip_mapping.csv"
OUTPUT_FILE = BASE_DIR / "boq_items_enriched.csv"


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def normalize(value):
    return str(value or "").strip().lower()


def main():
    for path in [
        BOQ_FILE,
        DOCUMENT_MAPPING_FILE,
        TENDER_MAPPING_FILE,
    ]:
        if not path.exists():
            print(f"Missing file: {path.name}")
            return

    items = read_csv(BOQ_FILE)
    document_mappings = read_csv(DOCUMENT_MAPPING_FILE)
    tender_mappings = read_csv(TENDER_MAPPING_FILE)

    # Map BOQ filenames to document records.
    boq_lookup = {}

    for document in document_mappings:
        if normalize(document.get("document_type")) != "boq":
            continue

        filename = normalize(document.get("document_filename"))

        if filename:
            boq_lookup.setdefault(filename, []).append(document)

    # Map ZIP filenames to their known tender IDs.
    tender_lookup = {}

    for tender in tender_mappings:
        zip_filename = normalize(tender.get("zip_filename"))

        if zip_filename:
            tender_lookup.setdefault(
                zip_filename, []
            ).append(tender)

    output_rows = []

    for item in items:
        row = dict(item)

        row["tender_id"] = ""
        row["portal"] = ""
        row["portal_tnid"] = ""
        row["zip_filename"] = ""
        row["mapping_status"] = "unmatched"

        filename = normalize(item.get("source_file"))
        document_matches = boq_lookup.get(filename, [])

        if len(document_matches) > 1:
            row["mapping_status"] = "ambiguous"

        elif len(document_matches) == 1:
            document = document_matches[0]

            row["portal"] = document.get("portal", "")
            row["portal_tnid"] = document.get("portal_tnid", "")
            row["zip_filename"] = document.get("zip_filename", "")

            zip_filename = normalize(row["zip_filename"])
            tender_matches = tender_lookup.get(zip_filename, [])

            document_id = document.get(
                "tender_id", ""
            ).strip()

            # Gather all known tender IDs for this ZIP.
            candidate_ids = set()

            if document_id:
                candidate_ids.add(document_id)

            for tender in tender_matches:
                tender_id = tender.get(
                    "tender_id", ""
                ).strip()

                if tender_id:
                    candidate_ids.add(tender_id)

            # Check for conflicting portal document IDs.
            portal_ids = {
                normalize(document.get("portal_tnid"))
            }

            for tender in tender_matches:
                portal_ids.add(
                    normalize(tender.get("portal_tnid"))
                )

            portal_ids.discard("")

            if len(candidate_ids) > 1 or len(portal_ids) > 1:
                row["mapping_status"] = "ambiguous"

            elif len(candidate_ids) == 1:
                row["tender_id"] = next(iter(candidate_ids))
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
    ]

    for column in [
        "portal",
        "portal_tnid",
        "zip_filename",
        "mapping_status",
    ]:
        if column not in columns:
            columns.append(column)

    with OUTPUT_FILE.open(
        "w", encoding="utf-8-sig", newline=""
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
        rate = (
            counts["matched"] / len(output_rows)
        ) * 100

        print(f"Matching rate: {rate:.2f}%")

    print(f"\nSaved to: {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
