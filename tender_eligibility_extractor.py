
import json
import re
from pathlib import Path
from zipfile import ZipFile

import pymupdf

BASE = Path(__file__).resolve().parent
ZIP_PATH = BASE / "downloaded_tenders" / "work_341915.zip"
PDF_NAME = "QualifyingRequirement.pdf"
OUTPUT = BASE / "tender_eligibility.json"


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def amount_to_inr(value, unit):
    amount = float(value.replace(",", ""))
    multiplier = {
        "lakh": 100000,
        "lakhs": 100000,
        "crore": 10000000,
        "crores": 10000000,
    }
    return round(amount * multiplier[unit.lower()], 2)


def extract_eligibility(text):
    normalized = normalize(text)

    result = {
        "tender_id": "2026_NSPCL_293262_1",
        "experience_period_years": None,
        "experience_options": [],
        "similar_work_definition": "",
        "minimum_average_turnover_inr": None,
        "turnover_period_years": None,
        "additional_conditions": [],
        "source_document": PDF_NAME,
    }

    # Experience period
    period = re.search(
        r"preceding\s+(?:last\s+)?"
        r"(?:seven\s*)?\(?(\d{1,2})\)?\s+years",
        normalized,
        re.IGNORECASE,
    )

    if period:
        result["experience_period_years"] = int(
            period.group(1)
        )

    # Experience alternatives
    word_counts = {
        "single": 1,
        "one": 1,
        "two": 2,
        "three": 3,
    }

    option_pattern = re.compile(
        r"\b(Single|One|Two|Three)\s+"
        r"orders?\s+of\s+value\s+"
        r"not\s+less\s+than\s+"
        r"(?:Rs\.?|INR|₹)?\s*"
        r"([\d,]+(?:\.\d+)?)\s*"
        r"(lakhs?|crores?)",
        re.IGNORECASE,
    )

    for match in option_pattern.finditer(normalized):
        result["experience_options"].append({
            "number_of_projects": word_counts[
                match.group(1).lower()
            ],
            "minimum_value_each_inr": amount_to_inr(
                match.group(2),
                match.group(3),
            ),
        })

    # Similar works
    similar = re.search(
        r"Similar Works Meaning:\s*(.*?)"
        r"(?=For clause no\.|Financial Criteria|$)",
        normalized,
        re.IGNORECASE,
    )

    if similar:
        result["similar_work_definition"] = (
            similar.group(1).strip()
        )

    # Financial turnover
    turnover = re.search(
        r"average annual turnover.*?"
        r"preceding\s+three\s*\(?3\)?\s+"
        r"financial years.*?"
        r"not be less than\s+"
        r"(?:INR|Rs\.?|₹)\s*"
        r"([\d,]+(?:\.\d+)?)\s*"
        r"(lakhs?|crores?)",
        normalized,
        re.IGNORECASE,
    )

    if turnover:
        result["turnover_period_years"] = 3
        result["minimum_average_turnover_inr"] = (
            amount_to_inr(
                turnover.group(1),
                turnover.group(2),
            )
        )

    # Important additional conditions
    conditions = [
        (
            r"group company\s*/\s*subsidiary",
            "Group/subsidiary company experience "
            "does not qualify automatically.",
        ),
        (
            r"sub-contractor may also be considered",
            "Subcontractor experience may qualify "
            "with the required certification.",
        ),
        (
            r"equity participation less than 10%",
            "JV experience with equity below "
            "10% is not accepted.",
        ),
        (
            r"Statutory Auditor",
            "Supporting documents may require "
            "statutory auditor verification.",
        ),
        (
            r"Third.Party Inspection Agency",
            "Specified third-party inspection "
            "agency verification is accepted.",
        ),
    ]

    for pattern, description in conditions:
        if re.search(pattern, normalized, re.IGNORECASE):
            result["additional_conditions"].append(
                description
            )

    return result


def main():
    with ZipFile(ZIP_PATH) as archive:
        with pymupdf.open(
            stream=archive.read(PDF_NAME),
            filetype="pdf",
        ) as pdf:
            text = "\n".join(
                page.get_text()
                for page in pdf
            )

    result = extract_eligibility(text)

    with OUTPUT.open("w", encoding="utf-8") as file:
        json.dump(result, file, indent=2)

    print(json.dumps(result, indent=2))
    print(f"\nSaved to: {OUTPUT.name}")


if __name__ == "__main__":
    main()
