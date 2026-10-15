
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

STEPS = [
    ("Download tender ZIPs", "download_tender.py"),
    ("Extract BOQ files", "extract_tender_zip.py"),
    ("Extract BOQ items", "boq_extractor.py"),
    ("Create tender document mapping", "create_tender_mapping.py"),
    ("Link BOQ items to tender IDs", "link_boq_to_tenders.py"),
]


def main():
    print("\nJAS TENDER DOCUMENT PROCESSING PIPELINE")
    print("=" * 50)

    for step_number, (name, script) in enumerate(STEPS, start=1):
        script_path = BASE_DIR / script

        print(f"\n{'=' * 50}", flush=True)
        print(f"STEP {step_number}/{len(STEPS)}: {name}", flush=True)
        print("=" * 50, flush=True)

        if not script_path.is_file():
            print(f"ERROR: Script not found: {script}")
            sys.exit(1)

        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=BASE_DIR,
            check=False,
        )

        if result.returncode != 0:
            print(
                f"\nPIPELINE STOPPED: {script} failed "
                f"(exit code {result.returncode})."
            )
            sys.exit(result.returncode)

        print(f"\nCOMPLETED: {name}", flush=True)

    output_file = BASE_DIR / "boq_items_enriched.csv"

    print("\n" + "=" * 50)
    print("PIPELINE COMPLETE!")
    print("=" * 50)

    if output_file.exists():
        print(f"Enriched BOQ data: {output_file.name}")
    else:
        print("WARNING: Enriched BOQ output was not found.")

    print("\nAll five stages finished.")


if __name__ == "__main__":
    main()
