
import csv
import json
import re
from pathlib import Path
from zipfile import ZipFile

import pymupdf


# ============================================================
# CONFIGURATION
# ============================================================

BASE = Path(__file__).resolve().parent
ZIP_DIR = BASE / "downloaded_tenders"
OUTPUT = BASE / "tender_eligibility.json"

# Only process these two ZIPs for testing.
TEST_ZIPS = [
    "work_341915.zip",
    "work_343127.zip",
]

MAPPING_FILES = [
    BASE / "tender_zip_mapping.csv",
    BASE / "tender_document_mapping.csv",
]

NUMBER_WORDS = {
    "single": 1,
    "one": 1,
    "two": 2,
    "three": 3,
}

MONEY_RE = (
    r"(?:₹|Rs\.?|INR)\s*"
    r"([\d,]+(?:\.\d+)?)\s*"
    r"(crores?|lakhs?)?"
)


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def to_inr(value, unit=None):
    amount = float(value.replace(",", ""))
    unit = (unit or "").lower()

    if unit.startswith("crore"):
        amount *= 10000000

    elif unit.startswith("lakh"):
        amount *= 100000

    return round(amount, 2)


def load_ids():
    ids = {}

    for path in MAPPING_FILES:
        if not path.exists():
            continue

        with path.open(
            newline="",
            encoding="utf-8-sig",
        ) as file:

            reader = csv.DictReader(file)

            for row in reader:
                zip_name = row.get(
                    "zip_filename", ""
                ).strip()

                tender_id = row.get(
                    "tender_id", ""
                ).strip()

                if zip_name and tender_id:
                    ids.setdefault(
                        zip_name, set()
                    ).add(tender_id)

    return ids


# ============================================================
# EXPERIENCE EXTRACTION
# ============================================================

def find_experience_options(text):
    options = []

    # NTPC format:
    # Single order of value not less than Rs 84.01 lakhs

    pattern_one = re.compile(
        r"\b(Single|One|Two|Three)\s+"
        r"orders?\s+of\s+value\s+"
        r"not\s+less\s+than\s+"
        + MONEY_RE,
        re.IGNORECASE,
    )

    for match in pattern_one.finditer(text):
        options.append({
            "number_of_projects": NUMBER_WORDS[
                match.group(1).lower()
            ],
            "minimum_value_each_inr": to_inr(
                match.group(2),
                match.group(3),
            ),
        })

    # AAI format:
    # three works, each of ₹2,18,80,886

    pattern_two = re.compile(
        r"\b(three|two|one)\s+"
        r"works?,?\s+each\s+of\s+"
        + MONEY_RE,
        re.IGNORECASE,
    )

    for match in pattern_two.finditer(text):
        options.append({
            "number_of_projects": NUMBER_WORDS[
                match.group(1).lower()
            ],
            "minimum_value_each_inr": to_inr(
                match.group(2),
                match.group(3),
            ),
        })

    # AAI format:
    # one work of ₹4,37,61,771

    pattern_three = re.compile(
        r"\bone\s+work\s+of\s+"
        + MONEY_RE,
        re.IGNORECASE,
    )

    for match in pattern_three.finditer(text):
        options.append({
            "number_of_projects": 1,
            "minimum_value_each_inr": to_inr(
                match.group(1),
                match.group(2),
            ),
        })

    # Remove duplicate experience options.
    unique = {}

    for option in options:
        key = (
            option["number_of_projects"],
            option["minimum_value_each_inr"],
        )

        unique[key] = option

    return list(unique.values())


def find_experience_years(text):
    patterns = [
        r"(?:last|preceding)\s+07\s*\(seven\)\s+years",
        r"(?:last|preceding)\s+seven\s*\(07\)\s+years",
        r"(?:last|preceding)\s+seven\s+years",
        r"(?:last|preceding)\s+(\d{1,2})\s+years",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            if match.lastindex:
                return int(match.group(1))

            return 7

    return None


# ============================================================
# FINANCIAL ELIGIBILITY
# ============================================================

def find_turnover(text):
    patterns = [
        re.compile(
            r"average annual turnover.{0,300}?"
            r"not be less than\s+"
            + MONEY_RE,
            re.IGNORECASE,
        ),

        re.compile(
            r"Average Annual financial turnover\s+of\s+"
            + MONEY_RE,
            re.IGNORECASE,
        ),
    ]

    for pattern in patterns:
        match = pattern.search(text)

        if match:
            return to_inr(
                match.group(1),
                match.group(2),
            )

    return None


def find_turnover_period(text):
    patterns = [
        r"(?:preceding|last)\s+three\s*\(?3\)?\s+(?:financial\s+)?years",
        r"(?:preceding|last)\s+three\s+(?:financial\s+)?years",
        r"(?:preceding|last)\s+3\s+(?:financial\s+)?years",
    ]

    for pattern in patterns:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return 3

    return None


def find_net_worth(text):
    match = re.search(
        r"minimum net worth of\s+"
        + MONEY_RE,
        text,
        re.IGNORECASE,
    )

    if match:
        return to_inr(
            match.group(1),
            match.group(2),
        )

    return None


# ============================================================
# JOINT VENTURE RULES
# ============================================================

def find_joint_venture_status(text):
    prohibited = re.search(
        r"Consortium\s*/\s*JV\s+companies\s+"
        r"shall\s+not\s+be\s+permitted",
        text,
        re.IGNORECASE,
    )

    if prohibited:
        return False

    # A mention of JV experience does not prove that
    # joint venture bidding is allowed.
    return None


# ============================================================
# SIMILAR WORK DEFINITION
# ============================================================

def find_similar_work(text):
    match = re.search(
        r"Similar Works Meaning:\s*(.*?)"
        r"(?=For clause no\.|Financial Criteria|$)",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1).strip()

    if "Aeronautical Ground Lighting Works" in text:
        return (
            "Aeronautical Ground Lighting Works "
            "including installation of inset fittings"
        )

    return ""


# ============================================================
# ZIP AND PDF PROCESSING
# ============================================================

def extract_from_zip(zip_name, id_mapping):
    path = ZIP_DIR / zip_name

    result = {
        "zip_filename": zip_name,
        "tender_id": "",
        "experience_period_years": None,
        "experience_options": [],
        "minimum_average_turnover_inr": None,
        "turnover_period_years": None,
        "minimum_net_worth_inr": None,
        "joint_venture_allowed": None,
        "similar_work_definition": "",
        "evidence": [],
        "review_required": True,
    }

    ids = id_mapping.get(
        zip_name, set()
    )

    if len(ids) == 1:
        result["tender_id"] = next(
            iter(ids)
        )

    elif len(ids) > 1:
        result["evidence"].append({
            "warning": "Conflicting tender IDs in mappings"
        })

    if not path.exists():
        result["evidence"].append({
            "warning": "ZIP file not found"
        })
        return result

    pages = []

    with ZipFile(path) as archive:
        for member in archive.namelist():
            if not member.lower().endswith(".pdf"):
                continue

            # Skip unrelated PDFs.
            if re.search(
                r"debar|safety.?rules",
                member,
                re.IGNORECASE,
            ):
                continue

            try:
                with pymupdf.open(
                    stream=archive.read(member),
                    filetype="pdf",
                ) as pdf:

                    # Limit to first 12 pages for testing.
                    for i in range(
                        min(12, len(pdf))
                    ):
                        page_text = clean(
                            pdf[i].get_text()
                        )

                        if page_text:
                            pages.append({
                                "document": Path(
                                    member
                                ).name,
                                "page": i + 1,
                                "text": page_text,
                            })

            except Exception as error:
                result["evidence"].append({
                    "warning": (
                        f"{member}: {error}"
                    )
                })

    combined = " ".join(
        page["text"]
        for page in pages
    )

    # Extract technical eligibility.
    result["experience_options"] = (
        find_experience_options(combined)
    )

    result["experience_period_years"] = (
        find_experience_years(combined)
    )

    result["similar_work_definition"] = (
        find_similar_work(combined)
    )

    # Extract financial eligibility.
    result["minimum_average_turnover_inr"] = (
        find_turnover(combined)
    )

    result["minimum_net_worth_inr"] = (
        find_net_worth(combined)
    )

    if (
        result["minimum_average_turnover_inr"]
        is not None
    ):
        result["turnover_period_years"] = (
            find_turnover_period(combined)
        )

    # Extract JV restriction.
    result["joint_venture_allowed"] = (
        find_joint_venture_status(combined)
    )

    # Record relevant source pages.
    indicators = [
        "qualifying requirement",
        "three works",
        "single order",
        "average annual",
        "net worth",
        "consortium /jv",
        "similar works meaning",
    ]

    for page in pages:
        page_text = page["text"].lower()

        if any(
            indicator in page_text
            for indicator in indicators
        ):
            result["evidence"].append({
                "document": page["document"],
                "page": page["page"],
            })

    # Keep manual review enabled until
    # we implement field-level verification.
    result["review_required"] = True

    return result


# ============================================================
# MAIN
# ============================================================

def main():
    id_mapping = load_ids()
    results = []

    for zip_name in TEST_ZIPS:
        print(
            f"\nProcessing: {zip_name}"
        )

        result = extract_from_zip(
            zip_name,
            id_mapping,
        )

        results.append(result)

        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
            )
        )

    with OUTPUT.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            results,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print(
        f"\nSaved {len(results)} records "
        f"to {OUTPUT.name}"
    )


if __name__ == "__main__":
    main()
