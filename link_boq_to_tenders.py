
import csv
from pathlib import Path

BOQ_FILE = Path("boq_all_items.csv")
MAPPING_FILE = Path("tender_document_mapping.csv")
OUTPUT_FILE = Path("boq_items_enriched.csv")

with MAPPING_FILE.open("r", encoding="utf-8-sig", newline="") as file:
    mappings = list(csv.DictReader(file))

boq_lookup = {}

for mapping in mappings:
    if mapping["document_type"] == "BOQ":
        filename = mapping["document_filename"].strip().lower()

        boq_lookup.setdefault(filename, []).append(mapping)

with BOQ_FILE.open("r", encoding="utf-8-sig", newline="") as file:
    items = list(csv.DictReader(file))

output_rows = []

for item in items:
    filename = item["source_file"].strip().lower()
    matches = boq_lookup.get(filename, [])

    row = dict(item)

    row["portal"] = ""
    row["portal_tnid"] = ""
    row["zip_filename"] = ""
    row["mapping_status"] = "unmatched"

    if len(matches) == 1:
        match = matches[0]

        row["portal"] = match["portal"]
        row["portal_tnid"] = match["portal_tnid"]
        row["zip_filename"] = match["zip_filename"]

        if match["tender_id"]:
            row["tender_id"] = match["tender_id"]

        row["mapping_status"] = "matched"

    elif len(matches) > 1:
        row["mapping_status"] = "ambiguous"

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

columns += [
    "portal",
    "portal_tnid",
    "zip_filename",
    "mapping_status",
]

with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as file:
    writer = csv.DictWriter(file, fieldnames=columns)
    writer.writeheader()
    writer.writerows(output_rows)

matched = sum(row["mapping_status"] == "matched" for row in output_rows)
unmatched = sum(row["mapping_status"] == "unmatched" for row in output_rows)
ambiguous = sum(row["mapping_status"] == "ambiguous" for row in output_rows)

print(f"Total BOQ items: {len(output_rows)}")
print(f"Matched: {matched}")
print(f"Unmatched: {unmatched}")
print(f"Ambiguous: {ambiguous}")
print(f"Saved to: {OUTPUT_FILE}")
