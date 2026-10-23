
import csv
import re
from pathlib import Path
from zipfile import ZipFile

import pymupdf

BASE = Path(__file__).resolve().parent
ZIP_DIR = BASE / "downloaded_tenders"
OUTPUT = BASE / "tender_metadata.csv"

PORTAL_MAPPING = BASE / "tender_zip_mapping.csv"
DOCUMENT_MAPPING = BASE / "tender_document_mapping.csv"

# Only these two ZIPs will be processed in this test.
TEST_ZIPS = ["work_341915.zip", "work_343127.zip"]

ID_PATTERN = re.compile(
    r"\b20\d{2}_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_\d+_\d+\b"
)

LABELS = {
    "tender_title": [
        r"Name\s+of\s+Work",
        r"Name\s+of\s+Package",
    ],
    "estimated_value_inr": [
        r"Total\s+Estimated\s+Value",
        r"Estimated\s+Cost",
        r"Estimated\s+Value",
    ],
    "emd_inr": [
        r"EMD\s+Amount",
        r"Earnest\s+Money\s+Deposit",
    ],
    "tender_fee_inr": [
        r"Tender\s+Fee",
        r"Cost\s+of\s+Tender\s+Document",
    ],
    "contract_duration": [
        r"Contract/Completion\s+Period",
        r"Time\s+Allowed",
        r"Completion\s+Period",
    ],
}

MONEY_PATTERN = re.compile(
    r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

DURATION_PATTERN = re.compile(
    r"\b\d+\s*(?:\([^)]*\)\s*)?"
    r"(?:months?|days?|years?|weeks?)\b",
    re.IGNORECASE,
)

FIELDS = [
    "zip_filename",
    "portal_tnid",
    "tender_id",
    "tender_id_source",
    "tender_id_status",
    "organization",
    "tender_title",
    "estimated_value_inr",
    "emd_inr",
    "tender_fee_inr",
    "contract_duration",
    "eligibility_text",
    "evidence",
]


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def load_mapping(path):
    mapping = {}

    if not path.exists():
        return mapping

    with path.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        for row in csv.DictReader(file):
            zip_name = row.get("zip_filename", "").strip()
            if not zip_name:
                continue

            entry = mapping.setdefault(
                zip_name, {
                    "ids": set(),
                    "portal_tnids": set(),
                }
            )

            tender_id = row.get("tender_id", "").strip()
            portal_tnid = row.get("portal_tnid", "").strip()

            if tender_id:
                entry["ids"].add(tender_id)

            if portal_tnid:
                entry["portal_tnids"].add(portal_tnid)

    return mapping


def extract_labeled_value(lines, patterns, kind):
    for index, line in enumerate(lines):
        for pattern in patterns:
            match = re.search(
                pattern + r"\s*[:\-]?\s*",
                line,
                re.IGNORECASE,
            )

            if not match:
                continue

            remainder = line[match.end():].strip(" :-")

            # Some PDFs put the label and value on
            # separate lines.
            candidates = [remainder]
            candidates.extend(lines[index + 1:index + 4])

            if kind == "money":
                for candidate in candidates[:2]:
                    found = MONEY_PATTERN.search(candidate)
                    if found:
                        return found.group(1).replace(",", "")

                # Support amounts without currency symbols,
                # but only directly beside the label.
                for candidate in candidates[:2]:
                    found = re.match(
                        r"^\s*([\d,]+(?:\.\d+)?)\b",
                        candidate,
                    )
                    if found:
                        return found.group(1).replace(",", "")
                    # Only accept a currency-free amount when it
                    # # appears directly after the field label.
                    if remainder:
                        found = re.match(
                            r"^\s*([\d,]+(?:\.\d+)?)"
                            r"(?=\s|$|/-)",
                            remainder,
                        )
                        if found:
                            return found.group(1).replace(",", "")

            elif kind == "duration":
                for candidate in candidates:
                    found = DURATION_PATTERN.search(candidate)
                    if found:
                        return clean(found.group())

            elif kind == "title":
                title_parts = []
                if remainder:
                    title_parts.append(remainder)

                for following in lines[index + 1:index + 4]:
                    following = following.strip()

                    if not following:
                        break

                    if re.search(
                        r"(?:Tender\s+ID|Estimated\s+Cost|"
                        r"EMD|Tender\s+Fee|Time\s+Allowed|"
                        r"Qualifying\s+Requirement)",
                        following,
                        re.IGNORECASE,
                    ):
                        break

                    title_parts.append(following)

                    # Avoid absorbing unrelated paragraphs.
                    if len(clean(" ".join(title_parts))) > 180:
                        break

                if title_parts:
                    return clean(" ".join(title_parts))

    return ""


def organization_from_text(text):
    first_lines = [
        clean(line)
        for line in text.splitlines()[:18]
        if clean(line)
    ]

    heading = " ".join(first_lines).upper()

    if "AIRPORTS AUTHORITY OF INDIA" in heading:
        return "Airports Authority of India"

    if (
        "NTPC" in heading
        and "SAIL" in heading
        and "POWER COMPANY" in heading
    ):
        return "NTPC-SAIL Power Company Limited"

    return ""


def extract_zip(zip_name, portal_map, document_map):
    row = {field: "" for field in FIELDS}
    row["zip_filename"] = zip_name

    portal = portal_map.get(zip_name, {})
    document = document_map.get(zip_name, {})

    portal_ids = portal.get("ids", set())
    document_ids = document.get("ids", set())
    portal_tnids = (
        portal.get("portal_tnids", set())
        | document.get("portal_tnids", set())
    )

    if len(portal_tnids) == 1:
        row["portal_tnid"] = next(iter(portal_tnids))

    pdf_ids = set()
    evidence = []

    zip_path = ZIP_DIR / zip_name

    if not zip_path.exists():
        row["tender_id_status"] = "zip_missing"
        return row

    with ZipFile(zip_path) as archive:
        members = [
            name for name in archive.namelist()
            if name.lower().endswith(".pdf")
        ]

        # Prioritize tender and qualification documents.
        members.sort(
            key=lambda name: (
                0 if re.search(
                    r"tender|nit|qualif|section|other",
                    name,
                    re.IGNORECASE,
                ) else 1,
                name.lower(),
            )
        )

        for member in members:
            filename = Path(member).name

            # Ignore generic policy documents for now.
            if re.search(
                r"debar|safety.?rules",
                filename,
                re.IGNORECASE,
            ):
                continue

            try:
                with pymupdf.open(
                    stream=archive.read(member),
                    filetype="pdf",
                ) as pdf:

                    for page_index in range(min(8, len(pdf))):
                        text = pdf[page_index].get_text()

                        if not text.strip():
                            continue

                        lines = [
                            line.strip()
                            for line in text.splitlines()
                        ]

                        source = (
                            f"{filename}:page{page_index + 1}"
                        )

                        # Capture IDs as candidates.
                        pdf_ids.update(ID_PATTERN.findall(text))

                        if not row["organization"]:
                            organization = organization_from_text(text)
                            if organization:
                                row["organization"] = organization
                                evidence.append(
                                    f"organization={source}"
                                )

                        for field, kind in [
                            ("tender_title", "title"),
                            ("estimated_value_inr", "money"),
                            ("emd_inr", "money"),
                            ("tender_fee_inr", "money"),
                            ("contract_duration", "duration"),
                        ]:
                            if row[field]:
                                continue

                            value = extract_labeled_value(
                                lines,
                                LABELS[field],
                                kind,
                            )

                            if value:
                                row[field] = value
                                evidence.append(
                                    f"{field}={source}"
                                )

                        # Capture relevant qualification pages.
                        if (
                            "eligibility_text" not in evidence
                            and (
                                "qualifying requirement"
                                in text.lower()
                                or "technical criteria"
                                in text.lower()
                            )
                        ):
                            if not row["eligibility_text"]:
                                row["eligibility_text"] = (
                                    clean(text)[:3500]
                                )
                                evidence.append(
                                    f"eligibility_text={source}"
                                )

            except Exception as error:
                print(f"WARNING: {filename}: {error}")

    # Determine the ID using source priority.
    if len(portal_ids) > 1 or len(document_ids) > 1:
        row["tender_id_status"] = "mapping_conflict"

    elif len(portal_ids) == 1:
        row["tender_id"] = next(iter(portal_ids))
        row["tender_id_source"] = "portal_mapping"

    elif len(document_ids) == 1:
        row["tender_id"] = next(iter(document_ids))
        row["tender_id_source"] = "document_mapping"

    elif len(pdf_ids) == 1:
        row["tender_id"] = next(iter(pdf_ids))
        row["tender_id_source"] = "pdf_candidate"
        row["tender_id_status"] = "needs_review"

    else:
        row["tender_id_status"] = (
            "ambiguous_pdf_ids" if pdf_ids
            else "missing_tender_id"
        )

    if row["tender_id"] and not row["tender_id_status"]:
        if pdf_ids and row["tender_id"] not in pdf_ids:
            row["tender_id_status"] = "pdf_disagreement"
        else:
            row["tender_id_status"] = "mapped"

    if pdf_ids:
        evidence.append(
            "pdf_id_candidates=" + "|".join(sorted(pdf_ids))
        )

    row["evidence"] = "; ".join(evidence)

    return row


def main():
    portal_map = load_mapping(PORTAL_MAPPING)
    document_map = load_mapping(DOCUMENT_MAPPING)

    results = []

    for zip_name in TEST_ZIPS:
        print(f"\nProcessing: {zip_name}")

        result = extract_zip(
            zip_name,
            portal_map,
            document_map,
        )
        results.append(result)

        for field in FIELDS:
            if field in ("eligibility_text", "evidence"):
                continue
            print(f"{field}: {result[field]}")

    with OUTPUT.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS,
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"\nSaved {len(results)} records to {OUTPUT.name}")


if __name__ == "__main__":
    main()
