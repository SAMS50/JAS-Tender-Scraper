
import csv
import re
from pathlib import Path
from zipfile import ZipFile, BadZipFile

ZIP_FOLDER = Path("downloaded_tenders")
OUTPUT_FILE = Path("tender_document_mapping.csv")

COLUMNS = [
    "portal",
    "portal_tnid",
    "tender_id",
    "zip_filename",
    "document_filename",
    "document_type",
]

records = []

for zip_path in sorted(ZIP_FOLDER.glob("*.zip")):
    match = re.search(r"work_(\d+)", zip_path.stem, re.IGNORECASE)

    portal_tnid = match.group(1) if match else ""

    try:
        with ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue

                filename = Path(member.filename).name

                if not filename:
                    continue

                extension = Path(filename).suffix.lower()

                if filename.lower().startswith("boq"):
                    document_type = "BOQ"
                elif extension == ".pdf":
                    document_type = "PDF"
                else:
                    document_type = "OTHER"

                records.append({
                    "portal": "etenders.gov.in",
                    "portal_tnid": portal_tnid,
                    "tender_id": "",
                    "zip_filename": zip_path.name,
                    "document_filename": filename,
                    "document_type": document_type,
                })

    except BadZipFile:
        print(f"Invalid ZIP: {zip_path.name}")

with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as file:
    writer = csv.DictWriter(file, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(records)

print(f"Documents mapped: {len(records)}")
print(f"Saved to: {OUTPUT_FILE}")
