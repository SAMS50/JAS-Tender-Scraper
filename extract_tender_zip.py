
import csv
import hashlib
import shutil
from pathlib import Path
from zipfile import ZipFile, BadZipFile

BASE_DIR = Path(__file__).resolve().parent

ZIP_FOLDER = BASE_DIR / "downloaded_tenders"
BOQ_FOLDER = BASE_DIR / "boq_files"
MANIFEST_FILE = BASE_DIR / "boq_extraction_manifest.csv"

BOQ_FOLDER.mkdir(parents=True, exist_ok=True)

COLUMNS = [
    "zip_filename",
    "original_filename",
    "archive_member",
    "extracted_filename",
    "extracted_path",
    "status",
]


def safe_name(name):
    return "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in name
    )


def file_hash(path):
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024), b""
        ):
            digest.update(chunk)

    return digest.hexdigest()


def main():
    records = []
    extracted = 0
    unchanged = 0
    failed = 0

    if not ZIP_FOLDER.exists():
        print(f"Missing ZIP folder: {ZIP_FOLDER}")
        return

    for zip_path in sorted(ZIP_FOLDER.glob("*.zip")):
        print(f"\nProcessing: {zip_path.name}")

        try:
            with ZipFile(zip_path) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue

                    original_name = Path(
                        member.filename.replace("\\", "/")
                    ).name

                    if not (
                        original_name.lower().startswith("boq")
                        and original_name.lower().endswith(".xls")
                    ):
                        continue

                    # Unique identity includes the ZIP and
                    # full internal archive member path.
                    member_hash = hashlib.sha256(
                        member.filename.encode("utf-8")
                    ).hexdigest()[:10]

                    extracted_name = (
                        f"{safe_name(zip_path.stem)}__"
                        f"{member_hash}__"
                        f"{safe_name(original_name)}"
                    )

                    destination = BOQ_FOLDER / extracted_name
                    temp_path = destination.with_name(
                        destination.name + ".tmp"
                    )

                    status = "extracted"

                    try:
                        with archive.open(member) as source:
                            with temp_path.open("wb") as output:
                                shutil.copyfileobj(source, output)

                        if (
                            destination.exists()
                            and file_hash(destination)
                            == file_hash(temp_path)
                        ):
                            temp_path.unlink()
                            status = "unchanged"
                            unchanged += 1
                        else:
                            temp_path.replace(destination)
                            extracted += 1

                        print(f"{status}: {extracted_name}")

                    except Exception as error:
                        failed += 1
                        status = "failed"

                        if temp_path.exists():
                            temp_path.unlink()

                        print(
                            f"Extraction failed: "
                            f"{original_name}: {error}"
                        )

                    records.append({
                        "zip_filename": zip_path.name,
                        "original_filename": original_name,
                        "archive_member": member.filename,
                        "extracted_filename": extracted_name,
                        "extracted_path": str(destination),
                        "status": status,
                    })

        except (BadZipFile, OSError) as error:
            failed += 1
            print(f"Invalid ZIP: {zip_path.name}: {error}")

    with MANIFEST_FILE.open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file, fieldnames=COLUMNS
        )
        writer.writeheader()
        writer.writerows(records)

    print("\n" + "=" * 50)
    print("BOQ EXTRACTION SUMMARY")
    print("=" * 50)

    print(f"New/updated BOQs: {extracted}")
    print(f"Unchanged BOQs: {unchanged}")
    print(f"Failed extractions: {failed}")
    print(f"Manifest records: {len(records)}")
    print(f"Saved manifest: {MANIFEST_FILE.name}")


if __name__ == "__main__":
    main()
