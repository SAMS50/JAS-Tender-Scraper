
from pathlib import Path
from zipfile import ZipFile, BadZipFile

ZIP_FOLDER = Path("downloaded_tenders")
BOQ_FOLDER = Path("boq_files")

BOQ_FOLDER.mkdir(exist_ok=True)

for zip_path in ZIP_FOLDER.glob("*.zip"):
    print(f"Processing: {zip_path.name}")

    try:
        with ZipFile(zip_path) as archive:
            for member in archive.infolist():
                filename = Path(member.filename).name

                if not filename.lower().endswith(".xls"):
                    continue

                if not filename.lower().startswith("boq"):
                    continue

                if member.is_dir():
                    continue

                destination = BOQ_FOLDER / filename

                with archive.open(member) as source:
                    with destination.open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)

                print(f"Extracted: {destination}")

    except BadZipFile:
        print(f"Invalid ZIP: {zip_path.name}")

print("\nExtraction complete!")
