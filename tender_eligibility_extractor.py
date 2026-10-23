
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

# Controlled test: only these two tenders.
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
# GENERAL HELPERS
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
            for row in csv.DictReader(file):
                name = row.get("zip_filename", "").strip()
                tender_id = row.get("tender_id", "").strip()

                if name and tender_id:
                    ids.setdefault(name, set()).add(tender_id)

    return ids


def make_evidence(page, match):
    """
    Evidence is taken directly from the PDF text.

    The page number is 1-based, matching the
    page position in a PDF viewer.
    """
    start = max(0, match.start() - 65)
    end = min(len(page["text"]), match.end() + 100)

    return {
        "document": page["document"],
        "page": page["page"],
        "matched_text": page["text"][
            match.start():match.end()
        ],
        "context": page["text"][start:end],
    }


def find_first(pages, patterns, converter=None):
    """
    Search each page independently.

    Returns:
        (value, evidence)

    This prevents evidence from being attributed
    to the wrong page.
    """
    for page in pages:
        for pattern in patterns:
            match = re.search(
                pattern,
                page["text"],
                re.IGNORECASE,
            )

            if match:
                if converter:
                    value = converter(match)
                else:
                    value = match.group(0)

                return value, make_evidence(page, match)

    return None, None


# ============================================================
# EXPERIENCE REQUIREMENTS
# ============================================================

def find_experience_options(pages):
    patterns = [
        re.compile(
            r"\b(Single|One|Two|Three)\s+"
            r"orders?\s+of\s+value\s+"
            r"not\s+less\s+than\s+"
            + MONEY_RE,
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(three|two|one)\s+"
            r"works?,?\s+each\s+of\s+"
            + MONEY_RE,
            re.IGNORECASE,
        ),
        re.compile(
            r"\bone\s+work\s+of\s+"
            + MONEY_RE,
            re.IGNORECASE,
        ),
    ]

    options = []
    seen = set()

    for page in pages:
        for pattern_index, pattern in enumerate(patterns):
            for match in pattern.finditer(page["text"]):
                if pattern_index == 2:
                    count = 1
                    amount = to_inr(
                        match.group(1),
                        match.group(2),
                    )
                else:
                    count = NUMBER_WORDS[
                        match.group(1).lower()
                    ]
                    amount = to_inr(
                        match.group(2),
                        match.group(3),
                    )

                key = (count, amount)

                if key in seen:
                    continue

                seen.add(key)

                options.append({
                    "number_of_projects": count,
                    "minimum_value_each_inr": amount,
                    "evidence": make_evidence(
                        page, match
                    ),
                })

    return options


def find_experience_years(pages):
    patterns = [
        r"(?:last|preceding)\s+07\s*\(seven\)\s+years",
        r"(?:last|preceding)\s+seven\s*\(07\)\s+years",
        r"(?:last|preceding)\s+seven\s+years",
        r"(?:last|preceding)\s+(\d{1,2})\s+years",
    ]

    def convert(match):
        if match.lastindex:
            return int(match.group(1))
        return 7

    return find_first(
        pages, patterns, convert
    )


# ============================================================
# FINANCIAL REQUIREMENTS
# ============================================================

def find_turnover(pages):
    patterns = [
        (
            r"average annual turnover.{0,300}?"
            r"not be less than\s+"
            + MONEY_RE
        ),
        (
            r"Average Annual financial turnover\s+of\s+"
            + MONEY_RE
        ),
    ]

    def convert(match):
        return to_inr(
            match.group(1),
            match.group(2),
        )

    return find_first(
        pages, patterns, convert
    )


def find_turnover_period(pages):
    patterns = [
        r"(?:preceding|last)\s+three\s*\(?3\)?\s+(?:financial\s+)?years",
        r"(?:preceding|last)\s+three\s+(?:financial\s+)?years",
        r"(?:preceding|last)\s+3\s+(?:financial\s+)?years",
    ]

    return find_first(
        pages,
        patterns,
        lambda match: 3,
    )


def find_net_worth(pages):
    pattern = (
        r"minimum net worth of\s+"
        + MONEY_RE
    )

    return find_first(
        pages,
        [pattern],
        lambda match: to_inr(
            match.group(1),
            match.group(2),
        ),
    )


# ============================================================
# OTHER ELIGIBILITY CONDITIONS
# ============================================================

def find_joint_venture(pages):
    pattern = (
        r"Consortium\s*/\s*JV\s+companies\s+"
        r"shall\s+not\s+be\s+permitted"
    )

    return find_first(
        pages,
        [pattern],
        lambda match: False,
    )


def find_similar_work(pages):
    patterns = [
        r"Similar Works Meaning:\s*(.*?)(?=For clause no\.|Financial Criteria|$)",
        r"Aeronautical Ground Lighting Works\s+"
        r"which includes installations?\s+of\s+"
        r"inset fittings",
    ]

    def convert(match):
        if match.lastindex:
            return clean(match.group(1))
        return clean(match.group(0))

    return find_first(
        pages, patterns, convert
    )


# ============================================================
# PDF READING
# ============================================================

def read_pdf_pages(zip_path):
    pages = []
    warnings = []

    with ZipFile(zip_path) as archive:
        for member in archive.namelist():
            if not member.lower().endswith(".pdf"):
                continue

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

                    for i in range(min(12, len(pdf))):
                        text = clean(
                            pdf[i].get_text()
                        )

                        if text:
                            pages.append({
                                "document": member,
                                "page": i + 1,
                                "text": text,
                            })

            except Exception as error:
                warnings.append(
                    f"{member}: {error}"
                )

    return pages, warnings


# ============================================================
# MAIN EXTRACTION
# ============================================================

def extract_from_zip(zip_name, id_mapping):
    result = {
        "zip_filename": zip_name,
        "tender_id": "",
        "experience_period_years": None,
        "experience_options": [],
        "similar_work_definition": "",
        "minimum_average_turnover_inr": None,
        "turnover_period_years": None,
        "minimum_net_worth_inr": None,
        "joint_venture_allowed": None,
        "field_evidence": {},
        "warnings": [],
        "review_required": True,
    }

    ids = id_mapping.get(zip_name, set())

    if len(ids) == 1:
        result["tender_id"] = next(iter(ids))
    elif len(ids) > 1:
        result["warnings"].append(
            "Conflicting tender IDs"
        )

    zip_path = ZIP_DIR / zip_name

    if not zip_path.exists():
        result["warnings"].append(
            "ZIP file not found"
        )
        return result

    pages, warnings = read_pdf_pages(zip_path)
    result["warnings"].extend(warnings)

    extractors = {
        "experience_period_years": find_experience_years,
        "similar_work_definition": find_similar_work,
        "minimum_average_turnover_inr": find_turnover,
        "turnover_period_years": find_turnover_period,
        "minimum_net_worth_inr": find_net_worth,
        "joint_venture_allowed": find_joint_venture,
    }

    for field, extractor in extractors.items():
        value, evidence = extractor(pages)

        result[field] = value

        if evidence:
            result["field_evidence"][field] = evidence

    result["experience_options"] = (
        find_experience_options(pages)
    )

    # Never automatically approve an eligibility
    # decision from regex matches alone.
    result["review_required"] = True

    return result


def main():
    id_mapping = load_ids()
    results = []

    for zip_name in TEST_ZIPS:
        print(f"\nProcessing: {zip_name}")

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
