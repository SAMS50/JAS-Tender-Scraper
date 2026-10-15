
import csv
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

    # Match BOQ filename to its ZIP.
    boq_lookup = {}

    for mapping in document_mappings:
        if normalize(mapping.get("document_type")) != "boq":
            continue

        filename = normalize(mapping.get("document_filename"))

        if filename:
            boq_lookup.setdefault(filename, []).append(mapping)

    # Match ZIP filename to full tender ID.
    tender_lookup = {}

    for mapping in tender_mappings:
        zip_filename = normalize(mapping.get("zip_filename"))

        if zip_filename:
            tender_lookup.setdefault(zip_filename, []).append(mapping)

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

            if len(tender_matches) > 1:
                row["mapping_status"] = "ambiguous"

            elif len(tender_matches) == 1:
                tender = tender_matches[0]

                document_portal_id = normalize(
                    document.get("portal_tnid")
                )
                tender_portal_id = normalize(
                    tender.get("portal_tnid")
                )

                if (
                    document_portal_id
                    and tender_portal_id
                    and document_portal_id != tender_portal_id
                ):
                    row["mapping_status"] = "ambiguous"

                elif tender.get("tender_id", "").strip():
                    row["tender_id"] = tender["tender_id"].strip()
                    row["mapping_status"] = "matched"

                else:
                    row["mapping_status"] = "missing_tender_id"

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
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(output_rows)

    statuses = [
        "matched",
        "unmatched",
        "ambiguous",
        "missing_tender_id",
    ]

    print(f"\nTotal BOQ items: {len(output_rows)}")

    for status in statuses:
        count = sum(
            row["mapping_status"] == status
            for row in output_rows
        )
        print(f"{status}: {count}")

    print(f"\nSaved to: {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
