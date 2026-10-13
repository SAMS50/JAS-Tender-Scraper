
import csv
import re
from pathlib import Path

import xlrd

INPUT_FOLDER = Path("boq_files")
OUTPUT_FILE = Path("boq_all_items.csv")

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
]


def extract_boq(file_path):
    items = []

    workbook = xlrd.open_workbook(str(file_path), on_demand=True)

    for sheet in workbook.sheets():
        if sheet.name.lower() == "macros":
            continue

        for row_index in range(sheet.nrows):
            if sheet.ncols < 6:
                continue

            serial = sheet.cell_value(row_index, 0)
            description = sheet.cell_value(row_index, 1)
            quantity = sheet.cell_value(row_index, 3)
            unit = sheet.cell_value(row_index, 4)
            rate = sheet.cell_value(row_index, 5)

            if not isinstance(serial, (int, float)):
                continue

            if not isinstance(description, str) or not description.strip():
                continue

            if not isinstance(quantity, (int, float)):
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
                "estimated_rate_inr": rate if valid_rate else "",
                "estimated_amount_inr": round(quantity * rate, 2)
                    if valid_rate else "",
            })

    workbook.release_resources()
    return items


def main():
    INPUT_FOLDER.mkdir(exist_ok=True)

    files = sorted(INPUT_FOLDER.glob("*.xls"))

    if not files:
        print("No .xls files found in boq_files")
        return

    all_items = []

    for file_path in files:
        try:
            items = extract_boq(file_path)
            all_items.extend(items)
            print(f"{file_path.name}: {len(items)} items")
        except Exception as error:
            print(f"ERROR reading {file_path.name}: {error}")

    with OUTPUT_FILE.open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(all_items)

    print(f"\nTotal BOQ items: {len(all_items)}")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
