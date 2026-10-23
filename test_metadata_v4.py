
import csv
from pathlib import Path

FILE = Path("tender_metadata_batch_v4.csv")

if not FILE.exists():
    raise SystemExit(
        "Run python tender_pdf_extractor.py first"
    )

with FILE.open(encoding="utf-8-sig", newline="") as f:
    rows = {
        row["zip_filename"]: row
        for row in csv.DictReader(f)
    }

tests = [
    ("NTPC estimated value",
     "work_341915.zip",
     "estimated_value_inr", "10502062.35"),

    ("AAI Kolkata estimated value",
     "work_343127.zip",
     "estimated_value_inr", "54702214.00"),

    ("AAI Kolkata EMD",
     "work_343127.zip",
     "emd_inr", "1641066.00"),

    ("AAI Gondia estimated value",
     "work_342456.zip",
     "estimated_value_inr", "4471306.00"),

    ("AAI Gondia EMD",
     "work_342456.zip",
     "emd_inr", "89426.00"),

    ("RBI estimated value",
     "work_343064.zip",
     "estimated_value_inr", "6108000.00"),

    ("RBI GST basis",
     "work_343064.zip",
     "estimated_value_basis",
     "including_gst;unspecified"),

    ("RBI EMD",
     "work_343064.zip",
     "emd_inr", "122160.00"),

    ("ONGC EMD",
     "work_343514.zip",
     "emd_inr", "228000.00"),

    ("DVC two-year estimated value",
     "work_343524.zip",
     "estimated_value_inr", "2828575.20"),

    ("DVC value basis",
     "work_343524.zip",
     "estimated_value_basis",
     "excluding_gst;two_years"),
]

failures = []

for name, zip_name, field, expected in tests:
    actual = rows.get(zip_name, {}).get(field, "")

    passed = actual == expected

    print(
        "PASS" if passed else "FAIL",
        "|", name,
        "| Expected:", expected,
        "| Actual:", actual or "MISSING",
    )

    if not passed:
        failures.append(name)

title_tests = [
    ("work_341915.zip", "NSPCL Rourkela"),
    ("work_343127.zip", "Kolkata"),
    ("work_342456.zip", "Gondia"),
    ("work_343494.zip", "GAIL, USAR"),
    ("work_343514.zip", "Cauvery Asset"),
]

for zip_name, expected_part in title_tests:
    title = rows.get(zip_name, {}).get(
        "tender_title", ""
    )

    passed = (
        expected_part.lower() in title.lower()
    )

    print(
        "PASS" if passed else "FAIL",
        "| Title:", zip_name,
        "| Contains:", expected_part,
        "| Actual:", title or "MISSING",
    )

    if not passed:
        failures.append("Title " + zip_name)

print("\n" + "=" * 55)
print(
    f"RESULT: {16 - len(failures)}/16 PASSED"
)
print("=" * 55)

if failures:
    print("Failed checks:")
    for failure in failures:
        print(" -", failure)
    raise SystemExit(1)
