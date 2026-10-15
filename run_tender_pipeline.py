
import subprocess
import sys

STEPS = [
    ("Download tender ZIPs", "download_tender.py"),
    ("Extract BOQ files", "extract_tender_zip.py"),
    ("Extract BOQ items", "boq_extractor.py"),
]

for name, script in STEPS:
    print(f"\n{'=' * 45}")
    print(f"STEP: {name}")
    print("=" * 45)

    result = subprocess.run(
        [sys.executable, script],
        check=False
    )

    if result.returncode != 0:
        print(f"\nPipeline stopped: {script} failed.")
        sys.exit(result.returncode)

print("\nPIPELINE COMPLETE!")
print("BOQ data saved to boq_all_items.csv")
