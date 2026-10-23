
import csv
from pathlib import Path

import xlrd

BASE_DIR = Path(__file__).resolve().parent

INPUT_FOLDER = BASE_DIR / "boq_files"
MANIFEST_FILE = BASE_DIR / "boq_extraction_manifest.csv"
OUTPUT_FILE = BASE_DIR / "boq_all_items.csv"

COLUMNS = [
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


def extract_boq(file_path, manifest_record):
    items = []

    workbook = xlrd.open_workbook(
        str(file_path),
        on_demand=True
    )

    try:
        for sheet in workbook.sheets():
            if sheet.name.lower() == "macros":
                continue

            if sheet.ncols < 6:
                continue

            for row_index in range(sheet.nrows):
                serial = sheet.cell_value(row_index, 0)
                description = sheet.cell_value(row_index, 1)
                quantity = sheet.cell_value(row_index, 3)
                unit = sheet.cell_value(row_index, 4)
                rate = sheet.cell_value(row_index, 5)

                if (
                    not isinstance(serial, (int, float))
                    or isinstance(serial, bool)
                ):
                    continue

                if (
                    not isinstance(description, str)
                    or not description.strip()
                ):
                    continue

                if (
                    not isinstance(quantity, (int, float))
                    or isinstance(quantity, bool)
                ):
                    continue

                valid_rate = (
                    isinstance(rate, (int, float))
                    and not isinstance(rate, bool)
                )

                items.append({
                    "source_file": file_path.name,
                    "tender_id": "",
                    "sheet": sheet.name,
                    "item_number": int(serial),
                    "description": description.strip(),
                    "quantity": quantity,
                    "unit": unit,
                    "estimated_rate_inr": (
                        rate if valid_rate else ""
                    ),
                    "estimated_amount_inr": (
                        round(quantity * rate, 2)
                        if valid_rate else ""
                    ),
                    "zip_filename": manifest_record[
                        "zip_filename"
                    ],
                    "original_filename": manifest_record[
                        "original_filename"
                    ],
                    "archive_member": manifest_record[
                        "archive_member"
                    ],
                })

    finally:
        workbook.release_resources()

    return items


def main():
    if not MANIFEST_FILE.exists():
        print(
            "ERROR: boq_extraction_manifest.csv "
            "not found. Run extract_tender_zip.py first."
        )
        return

    with MANIFEST_FILE.open(
        "r",
        newline="",
        encoding="utf-8-sig"
    ) as file:
        manifest = list(csv.DictReader(file))

    all_items = []
    processed = 0
    skipped = 0
    failed = 0

    seen_files = set()

    for record in manifest:
        if record.get("status") not in (
            "extracted",
            "unchanged"
        ):
            skipped += 1
            continue

        filename = record.get(
            "extracted_filename", ""
        ).strip()

        if not filename:
            skipped += 1
            continue

        # Prevent accidental duplicate manifest entries.
        if filename in seen_files:
            print(f"SKIPPED duplicate: {filename}")
            skipped += 1
            continue

        seen_files.add(filename)

        file_path = INPUT_FOLDER / filename

        if not file_path.is_file():
            print(f"MISSING: {filename}")
            failed += 1
            continue

        try:
            items = extract_boq(
                file_path, record
            )

            all_items.extend(items)
            processed += 1

            print(
                f"{filename}: {len(items)} items "
                f"[{record['zip_filename']}]"
            )

        except Exception as error:
            failed += 1
            print(
                f"ERROR reading {filename}: {error}"
            )

    # Write only the current manifest's BOQ items.
    with OUTPUT_FILE.open(
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=COLUMNS
        )
        writer.writeheader()
        writer.writerows(all_items)

    print("\n" + "=" * 50)
    print("BOQ PARSING SUMMARY")
    print("=" * 50)

    print(f"Manifest records: {len(manifest)}")
    print(f"BOQ files processed: {processed}")
    print(f"Skipped records: {skipped}")
    print(f"Failed files: {failed}")
    print(f"Total BOQ items: {len(all_items)}")
    print(f"Saved to: {OUTPUT_FILE.name}")


if __name__ == "__main__":
    main()
