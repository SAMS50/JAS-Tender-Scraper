
import csv
import re
from pathlib import Path
from zipfile import ZipFile, BadZipFile

import pymupdf

BASE_DIR = Path(__file__).resolve().parent
ZIP_FOLDER = BASE_DIR / "downloaded_tenders"
MAPPING_FILE = BASE_DIR / "tender_zip_mapping.csv"
OUTPUT_FILE = BASE_DIR / "tender_document_mapping.csv"

COLUMNS = [
    "portal",
    "portal_tnid",
    "tender_id",
    "zip_filename",
    "document_filename",
    "document_type",
]

TENDER_ID_PATTERN = re.compile(
    r"\b20\d{2}_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_\d+_\d+\b"
)


def load_existing_mappings():
    mappings = {}

    if not MAPPING_FILE.exists():
        print("Existing mapping CSV not found.")
        return mappings

    with MAPPING_FILE.open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        for row in csv.DictReader(file):
            zip_name = row.get("zip_filename", "").strip()
            tender_id = row.get("tender_id", "").strip()

            if not zip_name or not tender_id:
                continue

            key = zip_name.lower()

            if key in mappings and mappings[key] != tender_id:
                print(
                    f"CONFLICT in mapping CSV: {zip_name}"
                )
                mappings[key] = ""
            elif key not in mappings:
                mappings[key] = tender_id

    return mappings


def recover_id_from_pdf(archive):
    found_ids = set()

    for member in archive.infolist():
        if member.is_dir():
            continue

        if not member.filename.lower().endswith(".pdf"):
            continue

        try:
            pdf_bytes = archive.read(member)

            with pymupdf.open(
                stream=pdf_bytes, filetype="pdf"
            ) as pdf:
                for page in pdf:
                    text = page.get_text()
                    found_ids.update(
                        TENDER_ID_PATTERN.findall(text)
                    )

        except Exception as error:
            print(
                f"Could not read PDF {member.filename}: "
                f"{error}"
            )

    if len(found_ids) == 1:
        return next(iter(found_ids))

    if len(found_ids) > 1:
        print(
            "Multiple tender IDs found in PDFs: "
            + ", ".join(sorted(found_ids))
        )

    return ""


def classify_document(filename):
    extension = Path(filename).suffix.lower()

    if filename.lower().startswith("boq"):
        return "BOQ"

    if extension == ".pdf":
        return "PDF"

    return "OTHER"


def main():
    records = []
    existing_mappings = load_existing_mappings()

    recovered = 0
    unresolved = 0
    conflicts = 0

    for zip_path in sorted(ZIP_FOLDER.glob("*.zip")):
        match = re.fullmatch(
            r"work_(\d+)", zip_path.stem, re.IGNORECASE
        )

        portal_tnid = match.group(1) if match else ""
        zip_key = zip_path.name.lower()

        try:
            with ZipFile(zip_path) as archive:
                tender_id = existing_mappings.get(
                    zip_key, ""
                )

                pdf_tender_id = recover_id_from_pdf(
                    archive
                )

                if tender_id and pdf_tender_id:
                    if tender_id != pdf_tender_id:
                        print(
                            f"CONFLICT: {zip_path.name}"
                            f" | mapping={tender_id}"
                            f" | PDF={pdf_tender_id}"
                        )
                        tender_id = ""
                        conflicts += 1

                elif not tender_id and pdf_tender_id:
                    tender_id = pdf_tender_id
                    recovered += 1

                    print(
                        f"RECOVERED: {zip_path.name}"
                        f" -> {tender_id}"
                    )

                if not tender_id:
                    unresolved += 1
                    print(
                        f"UNRESOLVED: {zip_path.name}"
                    )

                for member in archive.infolist():
                    if member.is_dir():
                        continue

                    filename = Path(
                        member.filename
                    ).name

                    if not filename:
                        continue

                    records.append({
                        "portal": "etenders.gov.in",
                        "portal_tnid": portal_tnid,
                        "tender_id": tender_id,
                        "zip_filename": zip_path.name,
                        "document_filename": filename,
                        "document_type":
                            classify_document(filename),
                    })

        except BadZipFile:
            print(f"Invalid ZIP: {zip_path.name}")

    with OUTPUT_FILE.open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file, fieldnames=COLUMNS
        )
        writer.writeheader()
        writer.writerows(records)

    print("\n" + "=" * 50)
    print("DOCUMENT MAPPING SUMMARY")
    print("=" * 50)
    print(f"Documents mapped: {len(records)}")
    print(f"Tender IDs recovered from PDFs: {recovered}")
    print(f"ZIPs with unresolved tender IDs: {unresolved}")
    print(f"Mapping/PDF conflicts: {conflicts}")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
