
"""
JAS Tender Scraper — V9.1.1 Financial Quality Validation

Inputs:
    tender_metadata_batch_v8_1.csv
    tender_financial_candidates_v9.csv

Outputs:
    tender_metadata_batch_v9_1.csv
    tender_financial_review_v9_1.csv
    tender_financial_summary_v9_1.json

Safety:
    - Preserve existing financial values
    - Never modify tender IDs
    - Reject unrelated amounts and section/page references
    - Hold conditional and multi-basis financial amounts
    - Retain candidate-level decisions even for existing fields
    - Record DVC estimate basis only when evidence supports it
    - No changes to production database
"""

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parent

METADATA_INPUT = ROOT / "tender_metadata_batch_v8_1.csv"
CANDIDATES_INPUT = ROOT / "tender_financial_candidates_v9.csv"

METADATA_OUTPUT = ROOT / "tender_metadata_batch_v9_1.csv"
REVIEW_OUTPUT = ROOT / "tender_financial_review_v9_1.csv"
SUMMARY_OUTPUT = ROOT / "tender_financial_summary_v9_1.json"

FINANCIAL_FIELDS = (
    "estimated_value_inr",
    "emd_inr",
    "tender_fee_inr",
)

EXTRA_FIELDS = [
    "v9_1_financial_status",
    "v9_1_financial_provenance_json",
]

REVIEW_FIELDS = [
    "zip_filename",
    "tender_id",
    "document",
    "location",
    "candidate_field",
    "candidate_value",
    "candidate_basis",
    "decision",
    "reason",
    "evidence",
]

ID_PATTERN = re.compile(
    r"^20\d{2}_[A-Za-z0-9]+_\d+_\d+$"
)

AMOUNT_RE = re.compile(
    r"(?<![\w.])(?:₹|Rs\.?|INR)\s*"
    r"([\d,]+(?:\.\d{1,2})?)"
    r"\s*(crores?|cr\.?|lakhs?|lacs?|thousands?)?",
    re.I,
)

BARE_RE = re.compile(
    r"^\s*[:=\-–]\s*"
    r"([\d,]+(?:\.\d{1,2})?)"
    r"\s*(crores?|cr\.?|lakhs?|lacs?|thousands?)?"
    r"(?=\s*(?:/-|[+.,;)]|$))",
    re.I,
)

MULTIPLIERS = {
    "crore": Decimal("10000000"),
    "crores": Decimal("10000000"),
    "cr": Decimal("10000000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "lac": Decimal("100000"),
    "lacs": Decimal("100000"),
    "thousand": Decimal("1000"),
    "thousands": Decimal("1000"),
}

LABEL_PATTERNS = {
    "estimated_value_inr": re.compile(
        r"\b(?:estimated\s+(?:cost|value|amount)"
        r"(?:\s+of\s+(?:work|the\s+work))?|"
        r"tender\s+value)\b",
        re.I,
    ),
    "emd_inr": re.compile(
        r"\b(?:earnest\s+money(?:\s+deposit)?|"
        r"bid\s+security|EMD)\b",
        re.I,
    ),
    "tender_fee_inr": re.compile(
        r"\b(?:tender\s+(?:document\s+|processing\s+)?fee|"
        r"tender\s+cost|document\s+fee|"
        r"document\s+cost|cost\s+of\s+tender\s+document)\b",
        re.I,
    ),
}

BAD_CONTEXT = re.compile(
    r"(?:"
    r"non[- ]judicial|stamp\s+paper|"
    r"bank\s+guarantee\s+format|"
    r"annexure\s*[-–:]?\s*[A-Z]\s*:|"
    r"average\s+annual\s+turnover|"
    r"turnover\s+should|"
    r"should\s+be\s+at\s+least\s+\d+\s*%|"
    r"\d+\s*%\s+of\s+the\s+estimated\s+cost|"
    r"at\s+least\s+\d+\s*%|"
    r"contractor'?s\s+option|"
    r"clause\s+6A|"
    r"for\s+works\s+having\s+estimated\s+cost|"
    r"works\s+estimated\s+to\s+cost\s+up\s*to"
    r")",
    re.I,
)

CONDITIONAL_CONTEXT = re.compile(
    r"(?:"
    r"\bgroup\s*[12]\b|"
    r"\bboth\s+groups\b|"
    r"\bdiamond\s+category\b|"
    r"\bplatinum\s+category\b|"
    r"\bgold\s+category\b|"
    r"\bforeign\s+bidders?\b|"
    r"\bindian\s+bidders?\b|"
    r"\bterritor(?:y|ies)\b|"
    r"\b(?:exempt|exemption|waiver|waived)\b"
    r")",
    re.I,
)

BASIS_CONTEXT = re.compile(
    r"\b(?:one\s+year|two\s+year|"
    r"with\s+GST|without\s+GST|"
    r"including\s+GST|excluding\s+GST)\b",
    re.I,
)


def norm(value):
    return re.sub(
        r"\s+", " ", str(value or "")
    ).strip()


def decimal_amount(value):
    try:
        return Decimal(
            norm(value).replace(",", "")
        )
    except (InvalidOperation, ValueError):
        return None


def amount_from_match(match):
    amount = decimal_amount(match.group(1))

    if amount is None:
        return None

    unit = norm(
        match.group(2)
    ).lower().rstrip(".")

    return amount * MULTIPLIERS.get(
        unit, Decimal("1")
    )


def amount_in_fragment(fragment, expected):
    expected_value = decimal_amount(expected)

    if expected_value is None:
        return False

    for match in AMOUNT_RE.finditer(fragment):
        if amount_from_match(match) == expected_value:
            return True

    match = BARE_RE.search(fragment)

    return bool(
        match
        and amount_from_match(match) == expected_value
    )


def source_lines(evidence):
    return [
        norm(line)
        for line in str(evidence or "").split("|")
        if norm(line)
    ]


def label_amount_supported(candidate):
    field = norm(
        candidate.get("candidate_field")
    )

    pattern = LABEL_PATTERNS.get(field)

    if pattern is None:
        return False

    lines = source_lines(
        candidate.get("evidence")
    )

    for index, line in enumerate(lines):
        for match in pattern.finditer(line):
            fragment = line[match.end():]

            boundaries = [
                other.start()
                for other_pattern in LABEL_PATTERNS.values()
                for other in other_pattern.finditer(fragment)
            ]

            if boundaries:
                fragment = fragment[:min(boundaries)]

            if amount_in_fragment(
                fragment,
                candidate.get("candidate_value"),
            ):
                return True

            if (
                not fragment.strip(" :-=\t")
                and index + 1 < len(lines)
            ):
                following = lines[index + 1]

                has_other_label = any(
                    p.search(following)
                    for p in LABEL_PATTERNS.values()
                )

                if (
                    not has_other_label
                    and amount_in_fragment(
                        following,
                        candidate.get("candidate_value"),
                    )
                ):
                    return True

    return False


def context_is_bad(candidate):
    evidence = norm(
        candidate.get("evidence")
    )

    if BAD_CONTEXT.search(evidence):
        return True

    field = norm(
        candidate.get("candidate_field")
    )

    if field == "emd_inr":
        if (
            re.search(
                r"\b(?:annexure|section|chapter)\b",
                evidence,
                re.I,
            )
            and not re.search(
                r"(?:₹|Rs\.?|INR)\s*[\d,]+",
                evidence,
                re.I,
            )
        ):
            return True

    return False


def classify_candidate(candidate):
    field = norm(
        candidate.get("candidate_field")
    )
    value = decimal_amount(
        candidate.get("candidate_value")
    )
    evidence = norm(
        candidate.get("evidence")
    )

    if field not in FINANCIAL_FIELDS:
        return (
            "REJECTED",
            "Unsupported financial field",
        )

    if value is None or value < 0:
        return (
            "REJECTED",
            "Invalid financial amount",
        )

    if context_is_bad(candidate):
        return (
            "REJECTED",
            "Unrelated, generic or misleading context",
        )

    if CONDITIONAL_CONTEXT.search(evidence):
        return (
            "CONDITIONAL_REVIEW",
            "Group, category, bidder or exemption condition",
        )

    if norm(
        candidate.get("review_status")
    ) == "CONDITIONAL_REVIEW":
        return (
            "CONDITIONAL_REVIEW",
            "Conditional context flagged by V9",
        )

    if not label_amount_supported(candidate):
        return (
            "REJECTED",
            "Amount not supported by its source label",
        )

    if (
        field == "estimated_value_inr"
        and BASIS_CONTEXT.search(evidence)
    ):
        return (
            "BASIS_REVIEW",
            "GST or contract-period basis requires review",
        )

    return (
        "SOURCE_BACKED",
        "Explicit label and amount supported",
    )


def mapped_identity(row):
    return (
        norm(
            row.get("tender_id_status")
        ).lower() == "mapped"
        and bool(
            ID_PATTERN.fullmatch(
                norm(row.get("tender_id"))
            )
        )
    )


def review_record(candidate, decision, reason):
    return {
        "zip_filename": candidate.get(
            "zip_filename", ""
        ),
        "tender_id": candidate.get(
            "tender_id", ""
        ),
        "document": candidate.get(
            "document", ""
        ),
        "location": candidate.get(
            "location", ""
        ),
        "candidate_field": candidate.get(
            "candidate_field", ""
        ),
        "candidate_value": candidate.get(
            "candidate_value", ""
        ),
        "candidate_basis": candidate.get(
            "candidate_basis", ""
        ),
        "decision": decision,
        "reason": reason,
        "evidence": candidate.get(
            "evidence", ""
        ),
    }


def validate_and_promote(metadata, candidates):
    grouped = defaultdict(list)

    for candidate in candidates:
        key = (
            norm(candidate.get("zip_filename")),
            norm(candidate.get("candidate_field")),
        )
        grouped[key].append(candidate)

    output = []
    reviews = []

    for original in metadata:
        row = dict(original)
        zip_name = norm(
            row.get("zip_filename")
        )

        statuses = {}
        provenance = {}

        for field in FINANCIAL_FIELDS:
            field_candidates = grouped.get(
                (zip_name, field), []
            )

            evaluated = [
                (
                    candidate,
                    *classify_candidate(candidate),
                )
                for candidate in field_candidates
            ]

            supported = [
                candidate
                for candidate, decision, _ in evaluated
                if decision == "SOURCE_BACKED"
            ]

            supported_values = {
                decimal_amount(
                    candidate.get("candidate_value")
                )
                for candidate in supported
            }

            special_review = any(
                decision in (
                    "CONDITIONAL_REVIEW",
                    "BASIS_REVIEW",
                )
                for _, decision, _ in evaluated
            )

            existing = norm(row.get(field))

            if existing:
                statuses[field] = (
                    "EXISTING_VALUE_PRESERVED"
                )

            elif not mapped_identity(row):
                statuses[field] = (
                    "HELD_IDENTITY_REVIEW"
                    if field_candidates
                    else "NO_CANDIDATE"
                )

            elif special_review:
                statuses[field] = (
                    "HELD_CONTEXT_REVIEW"
                )

            elif len(supported_values) > 1:
                statuses[field] = (
                    "HELD_CONFLICTING_VALUES"
                )

            elif len(supported_values) == 1:
                selected_value = next(
                    iter(supported_values)
                )

                selected = next(
                    candidate
                    for candidate in supported
                    if decimal_amount(
                        candidate.get("candidate_value")
                    ) == selected_value
                )

                row[field] = format(
                    selected_value.quantize(
                        Decimal("0.01")
                    ),
                    "f",
                )

                provenance[field] = {
                    "source": "V9_FINANCIAL_EVIDENCE",
                    "verification": (
                        "SOURCE_BACKED_PENDING_PORTAL_VERIFICATION"
                    ),
                    "document": selected.get(
                        "document", ""
                    ),
                    "location": selected.get(
                        "location", ""
                    ),
                    "evidence": selected.get(
                        "evidence", ""
                    ),
                    "basis": selected.get(
                        "candidate_basis", ""
                    ),
                }

                statuses[field] = (
                    "PROMOTED_SOURCE_BACKED"
                )

            else:
                statuses[field] = (
                    "NO_SUPPORTED_CANDIDATE"
                )

            # V9.1.1: candidate decisions are independent
            # of field-level preservation.
            for candidate, decision, reason in evaluated:

                if decision == "SOURCE_BACKED":
                    candidate_amount = decimal_amount(
                        candidate.get("candidate_value")
                    )
                    existing_amount = decimal_amount(
                        existing
                    )

                    if existing:
                        if (
                            existing_amount is not None
                            and candidate_amount
                            == existing_amount
                        ):
                            decision = (
                                "EXISTING_VALUE_CONFIRMED"
                            )
                            reason = (
                                "Source-backed amount agrees "
                                "with existing metadata"
                            )
                        else:
                            decision = (
                                "EXISTING_VALUE_CONFLICT"
                            )
                            reason = (
                                "Source-backed amount differs "
                                "from existing metadata; "
                                "existing value preserved"
                            )

                    elif statuses[field] == (
                        "PROMOTED_SOURCE_BACKED"
                    ):
                        decision = (
                            "PROMOTED_SOURCE_BACKED"
                        )
                        reason = (
                            "Source-backed amount promoted "
                            "under field safeguards"
                        )

                    else:
                        decision = statuses[field]
                        reason = (
                            "Source-backed candidate held: "
                            + statuses[field]
                        )

                reviews.append(
                    review_record(
                        candidate,
                        decision,
                        reason,
                    )
                )

        row["v9_1_financial_status"] = json.dumps(
            statuses,
            ensure_ascii=False,
        )
        row[
            "v9_1_financial_provenance_json"
        ] = json.dumps(
            provenance,
            ensure_ascii=False,
        )

        output.append(row)

    return output, reviews


def annotate_dvc_basis(output, candidates):
    """
    Annotate only when:
      1. Expected DVC ZIP and value are present.
      2. V9 candidate explicitly supports the same value.
      3. Evidence states two-year and excluding GST.

    No financial values are changed.
    """
    expected = Decimal("2828575.20")

    for row in output:
        if norm(
            row.get("zip_filename")
        ) != "work_343524.zip":
            continue

        if decimal_amount(
            row.get("estimated_value_inr")
        ) != expected:
            continue

        matching = []

        for candidate in candidates:
            if (
                norm(candidate.get("zip_filename"))
                != "work_343524.zip"
            ):
                continue

            if (
                norm(candidate.get("candidate_field"))
                != "estimated_value_inr"
            ):
                continue

            if decimal_amount(
                candidate.get("candidate_value")
            ) != expected:
                continue

            evidence = norm(
                candidate.get("evidence")
            )

            if (
                re.search(
                    r"\btwo\s+years?\b",
                    evidence,
                    re.I,
                )
                and re.search(
                    r"\bwithout\s+GST\b",
                    evidence,
                    re.I,
                )
                and norm(
                    candidate.get("document")
                ) == "ANNEXURE-BOQ_PriceBreak-up.pdf"
            ):
                matching.append(candidate)

        if not matching:
            continue

        selected = matching[0]

        provenance = json.loads(
            row.get(
                "v9_1_financial_provenance_json"
            ) or "{}"
        )

        provenance["estimated_value_inr"] = {
            "source": "V9_FINANCIAL_EVIDENCE",
            "document": selected.get(
                "document", ""
            ),
            "location": selected.get(
                "location", ""
            ),
            "evidence": selected.get(
                "evidence", ""
            ),
            "basis": "TWO_YEARS_EXCLUDING_GST",
            "verification": (
                "SOURCE_BACKED_PENDING_PORTAL_VERIFICATION"
            ),
            "note": (
                "Existing amount preserved; "
                "source evidence supports this basis"
            ),
        }

        row[
            "v9_1_financial_provenance_json"
        ] = json.dumps(
            provenance,
            ensure_ascii=False,
        )


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(path)

    with path.open(
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(
            reader.fieldnames or []
        )


def write_csv(path, rows, fields):
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main():
    metadata, fields = read_csv(
        METADATA_INPUT
    )
    candidates, _ = read_csv(
        CANDIDATES_INPUT
    )

    required = {
        "zip_filename",
        "tender_id",
        "tender_id_status",
        *FINANCIAL_FIELDS,
    }

    if not required.issubset(fields):
        raise ValueError(
            "V8.1 metadata missing required columns"
        )

    zip_names = [
        norm(row.get("zip_filename"))
        for row in metadata
    ]

    if (
        not all(zip_names)
        or len(zip_names) != len(set(zip_names))
    ):
        raise ValueError(
            "Missing or duplicate ZIP identifiers"
        )

    output, reviews = validate_and_promote(
        metadata,
        candidates,
    )

    annotate_dvc_basis(
        output,
        candidates,
    )

    for before, after in zip(
        metadata, output
    ):
        for field in (
            "zip_filename",
            "tender_id",
            "tender_id_status",
        ):
            if before.get(field) != after.get(field):
                raise AssertionError(
                    f"Protected field changed: {field}"
                )

        for field in FINANCIAL_FIELDS:
            if norm(before.get(field)):
                if before.get(field) != after.get(field):
                    raise AssertionError(
                        f"Existing value changed: {field}"
                    )

    write_csv(
        METADATA_OUTPUT,
        output,
        list(
            dict.fromkeys(
                fields + EXTRA_FIELDS
            )
        ),
    )

    write_csv(
        REVIEW_OUTPUT,
        reviews,
        REVIEW_FIELDS,
    )

    promotions = []

    for before, after in zip(
        metadata, output
    ):
        for field in FINANCIAL_FIELDS:
            if (
                not norm(before.get(field))
                and norm(after.get(field))
            ):
                promotions.append(field)

    summary = {
        "version": "v9.1.1",
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "tender_count": len(output),
        "candidate_count": len(candidates),
        "promoted_total": len(promotions),
        "promoted_by_field": dict(
            Counter(promotions)
        ),
        "review_decisions": dict(
            Counter(
                review["decision"]
                for review in reviews
            )
        ),
        "dvc_basis_annotated": any(
            row.get("zip_filename")
            == "work_343524.zip"
            and "TWO_YEARS_EXCLUDING_GST"
            in row.get(
                "v9_1_financial_provenance_json",
                "",
            )
            for row in output
        ),
    }

    SUMMARY_OUTPUT.write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("=" * 65)
    print(
        "JAS V9.1.1 — FINANCIAL QUALITY VALIDATION"
    )
    print("=" * 65)

    print("Tender records:", len(output))
    print("Input candidates:", len(candidates))
    print(
        "Promoted financial fields:",
        len(promotions),
    )
    print(
        "Promotions by field:",
        summary["promoted_by_field"],
    )

    print(
        "DVC basis annotated:",
        summary["dvc_basis_annotated"],
    )

    print("\nCandidate decisions:")

    for decision, count in (
        summary["review_decisions"].items()
    ):
        print(f"  {decision}: {count}")

    print("\nOutputs:")
    print(METADATA_OUTPUT.name)
    print(REVIEW_OUTPUT.name)
    print(SUMMARY_OUTPUT.name)


if __name__ == "__main__":
    main()
