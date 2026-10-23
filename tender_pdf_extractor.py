
import csv
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pymupdf


ROOT = Path(__file__).resolve().parent
ZIP_DIR = ROOT / "downloaded_tenders"

OUTPUT = ROOT / "tender_metadata_batch_v4.csv"
AUDIT = ROOT / "tender_metadata_v4_audit.json"

BATCH = [
    "work_341915.zip",
    "work_343127.zip",
    "work_342456.zip",
    "work_342679.zip",
    "work_342996.zip",
    "work_343064.zip",
    "work_343288.zip",
    "work_343440.zip",
    "work_343494.zip",
    "work_343514.zip",
    "work_343524.zip",
    "work_343534.zip",
    "work_343545.zip",
    "work_343550.zip",
    "work_343552.zip",
    "work_343553.zip",
    "work_343554.zip",
    "work_344022.zip",
    "work_344025.zip",
    "work_344089.zip",
]

MAX_PAGES = 12

FIELDS = [
    "zip_filename",
    "portal_tnid",
    "tender_id",
    "tender_id_source",
    "tender_id_status",
    "organization",
    "tender_title",
    "estimated_value_inr",
    "estimated_value_basis",
    "emd_inr",
    "tender_fee_inr",
    "contract_duration",
    "document_tender_reference",
    "eligibility_text",
    "evidence",
    "financial_candidates",
    "metadata_fields_found",
    "metadata_fields_total",
    "extraction_warnings",
]

ORGS = {
    "NTPC-SAIL Power Company Limited": [
        "NTPC-SAIL POWER COMPANY",
    ],
    "Airports Authority of India": [
        "AIRPORTS AUTHORITY OF INDIA",
    ],
    "Reserve Bank of India": [
        "RESERVE BANK OF INDIA",
    ],
    "GAIL (India) Limited": [
        "GAIL (INDIA) LIMITED",
    ],
    "Oil and Natural Gas Corporation Limited": [
        "OIL AND NATURAL GAS CORPORATION",
    ],
    "Damodar Valley Corporation": [
        "DAMODAR VALLEY CORPORATION",
    ],
    "Municipal Corporation of Delhi": [
        "MUNICIPAL CORPORATION OF DELHI",
    ],
    "Delhi Metro Rail Corporation": [
        "DELHI METRO RAIL CORPORATION",
    ],
}

ID_RE = re.compile(
    r"\b20\d{2}_[A-Za-z0-9]+"
    r"(?:_[A-Za-z0-9]+)*_\d+_\d+\b"
)

AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"\s*(?P<unit>lakhs?|lacs?|crores?|cr\.?)?",
    re.I,
)

LABELS = {
    "estimated_value_inr": [
        r"estimated\s+cost",
        r"estimated\s+value",
        r"estimated\s+amount",
    ],
    "emd_inr": [
        r"earnest\s+money\s+deposit",
        r"\bEMD\s+amount\b",
        r"\bEMD\s*(?:of|:|-)",
    ],
    "tender_fee_inr": [
        r"tender\s+(?:processing\s+)?fee",
        r"cost\s+of\s+tender\s+document",
    ],
}

TITLE_LABELS = [
    r"name\s+of\s+(?:the\s+)?work",
    r"brief\s+description\s+of\s+work",
    r"name\s+of\s+package",
    r"scope\s+of\s+(?:supply|procurement)",
]

TITLE_STOP = re.compile(
    r"^(?:estimated\s+cost|tender\s+(?:id|no|fee)|"
    r"emd|earnest\s+money|completion\s+period|"
    r"date\s+of|notice\s+inviting|"
    r"invitation\s+for|eligibility|"
    r"qualifying\s+requirement|"
    r"important\s+dates|"
    r"section\s+[ivx\d]+|"
    r"page\s+\d+|"
    r"signature|"
    r"for\s+and\s+on\s+behalf)",
    re.I,
)

DURATION_RE = re.compile(
    r"\b\d+\s*(?:\([^)]*\)\s*)?"
    r"(?:days?|months?|weeks?|years?)\b",
    re.I,
)


def norm(text):
    return re.sub(
        r"\s+", " ", text or ""
    ).strip()


def load_mappings():
    result = {}

    mapping_files = [
        "tender_zip_mapping.csv",
        "tender_document_mapping.csv",
    ]

    for filename in mapping_files:
        path = ROOT / filename

        if not path.exists():
            print("Mapping not found:", filename)
            continue

        with path.open(
            encoding="utf-8-sig",
            newline="",
        ) as file:

            for row in csv.DictReader(file):
                name = norm(
                    row.get("zip_filename")
                )

                if not name:
                    continue

                entry = result.setdefault(
                    name,
                    {
                        "ids": set(),
                        "tnids": set(),
                    },
                )

                if row.get("tender_id"):
                    entry["ids"].add(
                        norm(row["tender_id"])
                    )

                if row.get("portal_tnid"):
                    entry["tnids"].add(
                        norm(row["portal_tnid"])
                    )

    return result


def amount_value(match):
    value = Decimal(
        match.group("num").replace(",", "")
    )

    unit = (
        match.group("unit") or ""
    ).lower()

    if unit.startswith(("lakh", "lac")):
        value *= 100000

    elif unit.startswith(("crore", "cr")):
        value *= 10000000

    return value.quantize(
        Decimal("0.01")
    )


def gst_basis(text):
    text = text.lower()

    if re.search(
        r"(?:excluding|exclusive\s+of|without)"
        r"\s+gst",
        text,
    ):
        return "excluding_gst"

    if re.search(
        r"(?:including|inclusive\s+of|with)"
        r"\s+gst",
        text,
    ):
        return "including_gst"

    return "unspecified"


def period_basis(text):
    if re.search(
        r"\b(?:two|2)\s+years?\b",
        text,
        re.I,
    ):
        return "two_years"

    if re.search(
        r"\b(?:one|1)\s+years?\b",
        text,
        re.I,
    ):
        return "one_year"

    return "unspecified"


def classify_context(text, field):
    lower = text.lower()

    if field == "estimated_value_inr":

        if re.search(
            r"30\s*%\s+of\s+(?:the\s+)?estimated|"
            r"turnover|financial\s+information|"
            r"similar\s+work|"
            r"minimum\s+annual|"
            r"experience\s+criteria",
            lower,
        ):
            return "eligibility_reference"

    return "tender_financial_candidate"


def extract_money(lines, source):
    candidates = []

    for i, line in enumerate(lines):

        for field, patterns in LABELS.items():
            label_match = None

            for pattern in patterns:
                label_match = re.search(
                    pattern,
                    line,
                    re.I,
                )

                if label_match:
                    break

            if not label_match:
                continue

            # First search on the same line,
            # after the financial field label.

            after_label = line[
                label_match.end():
            ]

            match = AMOUNT_RE.search(
                after_label
            )

            amount_line = line

            # FIX:
            # PDF tables can put the amount
            # several lines below its label.
            #
            # RBI's EMD is three lines below
            # "Earnest Money Deposit".
            #
            # Search up to four following lines.

            if not match:

                for offset in range(1, 5):
                    next_index = i + offset

                    if next_index >= len(lines):
                        break

                    next_line = lines[
                        next_index
                    ]

                    # Do not accidentally associate
                    # another financial field's
                    # amount with this label.

                    another_label = any(
                        re.search(
                            pattern,
                            next_line,
                            re.I,
                        )
                        for other_patterns
                        in LABELS.values()
                        for pattern
                        in other_patterns
                    )

                    if another_label:
                        break

                    match = AMOUNT_RE.search(
                        next_line
                    )

                    if match:
                        amount_line = next_line
                        break

            if not match:
                continue

            amount = amount_value(
                match
            )

            context = norm(
                " ".join(
                    lines[
                        max(0, i - 1):
                        min(len(lines), i + 6)
                    ]
                )
            )

            if amount_line == line:
                clause = after_label
            else:
                clause = amount_line

            amount_end = match.end()

            trailing = clause[
                amount_end:
                amount_end + 130
            ]

            local_basis = (
                clause[:amount_end]
                + trailing
            )

            classification = classify_context(
                context,
                field,
            )

            if (
                field == "estimated_value_inr"
                and re.search(
                    r"\b(?:at\s+least|30\s*%|"
                    r"turnover|financial\s+year)",
                    context,
                    re.I,
                )
            ):
                classification = (
                    "eligibility_reference"
                )

            candidates.append(
                {
                    "field": field,
                    "value_inr": str(amount),
                    "gst_basis": gst_basis(
                        local_basis
                    ),
                    "period": period_basis(
                        local_basis
                    ),
                    "classification":
                        classification,
                    "document": source[0],
                    "page": source[1],
                    "context": context[:550],
                }
            )

            break

    return candidates


def clean_title(text):
    text = norm(text)

    text = re.sub(
        r"^[\s:–—/-]+",
        "",
        text,
    )

    # Remove duplicated GAIL-style
    # procurement prefixes.

    text = re.sub(
        r"^(?:PROCUREMENT\s+)+"
        r"(?=PROCUREMENT\s+OF)",
        "",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\s+(?:estimated\s+cost|"
        r"tender\s+id|emd\s+amount)"
        r"\s*[:\-].*$",
        "",
        text,
        flags=re.I,
    )

    return norm(text)


def title_looks_valid(text):
    if not (
        25 <= len(text) <= 350
    ):
        return False

    if re.match(
        r"^(goods\s*:|"
        r"a\s+sum\s+equivalent|"
        r"payment\s+shall|"
        r"the\s+contractor)",
        text,
        re.I,
    ):
        return False

    return True


def collect_title(
    lines,
    start,
    initial,
):
    pieces = []

    if initial:
        pieces.append(initial)

    for line in lines[
        start + 1:
        start + 8
    ]:

        if TITLE_STOP.search(line):
            break

        if not line:
            break

        if len(
            norm(" ".join(pieces))
        ) > 300:
            break

        if (
            pieces
            and pieces[-1]
            .rstrip()
            .endswith(".")
        ):
            break

        pieces.append(line)

    return clean_title(
        " ".join(pieces)
    )


def extract_title(lines):

    for i, line in enumerate(
        lines[:100]
    ):

        for label in TITLE_LABELS:

            match = re.search(
                label + r"\s*[:\-]?\s*",
                line,
                re.I,
            )

            if not match:
                continue

            title = collect_title(
                lines,
                i,
                line[
                    match.end():
                ],
            )

            if title_looks_valid(
                title
            ):
                return title

    for i, line in enumerate(
        lines[:35]
    ):

        match = re.search(
            r"(?:TENDER\s+DOCUMENT\s+FOR|"
            r"FOR\s+PROCUREMENT\s+OF|"
            r"E-TENDER\s+FOR)"
            r"\s+(.+)",
            line,
            re.I,
        )

        if match:

            title = collect_title(
                lines,
                i,
                match.group(1),
            )

            if title_looks_valid(
                title
            ):
                return title

    return ""


def extract_organization(text):
    upper = norm(
        text[:3500]
    ).upper()

    for name, aliases in ORGS.items():

        if any(
            alias in upper
            for alias in aliases
        ):
            return name

    return ""


def extract_duration(lines):
    labels = [
        r"completion\s+period",
        r"period\s+of\s+contract",
        r"time\s+allowed",
        r"contract\s+duration",
    ]

    for i, line in enumerate(
        lines
    ):

        for label in labels:

            match = re.search(
                label,
                line,
                re.I,
            )

            if not match:
                continue

            segment = " ".join(
                [line[
                    match.end():
                ]]
                + lines[
                    i + 1:
                    i + 2
                ]
            )

            duration = DURATION_RE.search(
                segment
            )

            if duration:
                return norm(
                    duration.group()
                )

    for line in lines:

        match = re.search(
            r"(?:completed|completion)"
            r"\s+within\s+"
            r"(\d+\s+"
            r"(?:days?|months?|years?))",
            line,
            re.I,
        )

        if match:
            return norm(
                match.group(1)
            )

    return ""


def resolve_financial(
    field,
    candidates,
):
    relevant = [
        item
        for item in candidates
        if (
            item["field"] == field
            and item["classification"]
            == "tender_financial_candidate"
        )
    ]

    if not relevant:
        return (
            "",
            "",
            "No accepted candidate",
        )

    grouped = {}

    for item in relevant:
        grouped.setdefault(
            item["value_inr"],
            [],
        ).append(item)

    if (
        field == "estimated_value_inr"
        and len(grouped) > 1
    ):

        two_year = {
            value: items
            for value, items
            in grouped.items()
            if any(
                item["period"]
                == "two_years"
                for item in items
            )
        }

        one_year_exists = any(
            item["period"]
            == "one_year"
            for item in relevant
        )

        if (
            len(two_year) == 1
            and one_year_exists
        ):
            grouped = two_year

    if len(grouped) != 1:

        return (
            "",
            "",
            "Conflicting amounts: "
            + ", ".join(
                sorted(grouped)
            ),
        )

    value, items = next(
        iter(grouped.items())
    )

    explicit_gst = {
        item["gst_basis"]
        for item in items
        if item["gst_basis"]
        != "unspecified"
    }

    explicit_period = {
        item["period"]
        for item in items
        if item["period"]
        != "unspecified"
    }

    if len(explicit_gst) > 1:
        return (
            "",
            "",
            "Conflicting GST basis",
        )

    if len(explicit_period) > 1:
        return (
            "",
            "",
            "Conflicting contract periods",
        )

    gst = next(
        iter(explicit_gst),
        "unspecified",
    )

    period = next(
        iter(explicit_period),
        "unspecified",
    )

    return (
        value,
        f"{gst};{period}",
        "",
    )


def process_zip(
    name,
    mappings,
):
    row = {
        field: ""
        for field in FIELDS
    }

    row["zip_filename"] = name

    warnings = []
    candidates = []
    evidence = []
    pdf_ids = set()

    blank_pages = 0
    readable_pages = 0

    mapping = mappings.get(
        name,
        {
            "ids": set(),
            "tnids": set(),
        },
    )

    if len(
        mapping["tnids"]
    ) == 1:

        row["portal_tnid"] = next(
            iter(
                mapping["tnids"]
            )
        )

    if len(
        mapping["ids"]
    ) == 1:

        row["tender_id"] = next(
            iter(
                mapping["ids"]
            )
        )

        row["tender_id_source"] = (
            "mapping"
        )

        row["tender_id_status"] = (
            "mapped"
        )

    elif len(
        mapping["ids"]
    ) > 1:

        row["tender_id_status"] = (
            "mapping_conflict"
        )

        warnings.append(
            "Conflicting mapped tender IDs"
        )

    path = ZIP_DIR / name

    if not path.exists():
        warnings.append(
            "ZIP not found"
        )

    else:
        try:

            with ZipFile(
                path
            ) as archive:

                members = sorted(
                    [
                        member
                        for member
                        in archive.namelist()
                        if member.lower()
                        .endswith(".pdf")
                    ],
                    key=lambda x: (
                        0
                        if re.search(
                            r"tender|nit|qualif",
                            x,
                            re.I,
                        )
                        else 1,
                        x.lower(),
                    ),
                )

                if not members:
                    warnings.append(
                        "No PDF documents"
                    )

                for member in members:

                    filename = Path(
                        member
                    ).name

                    try:

                        data = archive.read(
                            member
                        )

                        with pymupdf.open(
                            stream=data,
                            filetype="pdf",
                        ) as pdf:

                            for page_no in range(
                                min(
                                    MAX_PAGES,
                                    len(pdf),
                                )
                            ):

                                text = pdf[
                                    page_no
                                ].get_text()

                                if not norm(
                                    text
                                ):
                                    blank_pages += 1
                                    continue

                                readable_pages += 1

                                lines = [
                                    norm(line)
                                    for line
                                    in text.splitlines()
                                    if norm(line)
                                ]

                                pdf_ids.update(
                                    ID_RE.findall(
                                        text
                                    )
                                )

                                source = (
                                    filename,
                                    page_no + 1,
                                )

                                if not row[
                                    "organization"
                                ]:

                                    row[
                                        "organization"
                                    ] = extract_organization(
                                        text
                                    )

                                if (
                                    not row[
                                        "tender_title"
                                    ]
                                    and page_no < 3
                                ):

                                    row[
                                        "tender_title"
                                    ] = extract_title(
                                        lines
                                    )

                                if not row[
                                    "contract_duration"
                                ]:

                                    row[
                                        "contract_duration"
                                    ] = extract_duration(
                                        lines
                                    )

                                candidates.extend(
                                    extract_money(
                                        lines,
                                        source,
                                    )
                                )

                                if (
                                    not row[
                                        "eligibility_text"
                                    ]
                                    and re.search(
                                        r"qualifying\s+requirements|"
                                        r"technical\s+criteria",
                                        text,
                                        re.I,
                                    )
                                ):

                                    row[
                                        "eligibility_text"
                                    ] = norm(
                                        text
                                    )[:3500]

                    except Exception as error:

                        warnings.append(
                            f"{filename}: "
                            f"{error}"
                        )

        except Exception as error:

            warnings.append(
                f"ZIP error: {error}"
            )

    if not row[
        "tender_id"
    ]:

        if len(
            pdf_ids
        ) == 1:

            row[
                "tender_id"
            ] = next(
                iter(
                    pdf_ids
                )
            )

            row[
                "tender_id_source"
            ] = "pdf_candidate"

            row[
                "tender_id_status"
            ] = "needs_review"

        elif not row[
            "tender_id_status"
        ]:

            row[
                "tender_id_status"
            ] = "missing_tender_id"

    if (
        row["tender_id"]
        and pdf_ids
        and row[
            "tender_id"
        ] not in pdf_ids
    ):

        warnings.append(
            "Mapped/PDF tender ID disagreement"
        )

    for field in LABELS:

        value, basis, warning = (
            resolve_financial(
                field,
                candidates,
            )
        )

        row[field] = value

        if field == (
            "estimated_value_inr"
        ):
            row[
                "estimated_value_basis"
            ] = basis

        if (
            warning
            and warning
            != "No accepted candidate"
        ):

            warnings.append(
                f"{field}: "
                f"{warning}"
            )

        if value:

            matching = [
                item
                for item in candidates
                if (
                    item["field"]
                    == field
                    and item[
                        "value_inr"
                    ] == value
                )
            ]

            if matching:

                first = matching[0]

                evidence.append(
                    f"{field}="
                    f"{first['document']}:"
                    f"page{first['page']}"
                )

    if blank_pages:

        warnings.append(
            f"{blank_pages} pages "
            f"without extractable text"
        )

    if not readable_pages:

        warnings.append(
            "No readable PDF text"
        )

    row[
        "financial_candidates"
    ] = json.dumps(
        candidates,
        ensure_ascii=False,
    )

    row[
        "evidence"
    ] = "; ".join(
        evidence
    )

    core = [
        "organization",
        "tender_title",
        "estimated_value_inr",
        "emd_inr",
        "tender_fee_inr",
        "contract_duration",
    ]

    row[
        "metadata_fields_found"
    ] = sum(
        bool(
            row[field]
        )
        for field in core
    )

    row[
        "metadata_fields_total"
    ] = 6

    row[
        "extraction_warnings"
    ] = " | ".join(
        warnings
    )

    return row


def main():
    mappings = load_mappings()

    rows = []

    print("=" * 65)
    print(
        "JAS METADATA EXTRACTION "
        "- VALIDATION V4"
    )
    print("=" * 65)

    for index, name in enumerate(
        BATCH,
        1,
    ):

        row = process_zip(
            name,
            mappings,
        )

        rows.append(
            row
        )

        print(
            f"\n[{index}/20] {name}"
        )

        print(
            "Tender ID:",
            row[
                "tender_id"
            ] or "MISSING",
        )

        print(
            "Organization:",
            row[
                "organization"
            ] or "MISSING",
        )

        print(
            "Title:",
            row[
                "tender_title"
            ] or "MISSING",
        )

        print(
            "Estimated value:",
            row[
                "estimated_value_inr"
            ] or "REVIEW/MISSING",
        )

        print(
            "Basis:",
            row[
                "estimated_value_basis"
            ] or "UNKNOWN",
        )

        print(
            "EMD:",
            row[
                "emd_inr"
            ] or "MISSING",
        )

        print(
            "Fields:",
            f"{row['metadata_fields_found']}/6",
        )

        if row[
            "extraction_warnings"
        ]:

            print(
                "Warnings:",
                row[
                    "extraction_warnings"
                ],
            )

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
        writer.writerows(
            rows
        )

    audit = {
        "version": "v4",
        "tenders": len(
            rows
        ),
        "field_coverage": {
            field: sum(
                bool(
                    row[field]
                )
                for row in rows
            )
            for field in [
                "tender_id",
                "organization",
                "tender_title",
                "estimated_value_inr",
                "emd_inr",
                "contract_duration",
            ]
        },
        "records": [
            {
                "zip_filename":
                    row[
                        "zip_filename"
                    ],
                "tender_id":
                    row[
                        "tender_id"
                    ],
                "estimated_value_inr":
                    row[
                        "estimated_value_inr"
                    ],
                "estimated_value_basis":
                    row[
                        "estimated_value_basis"
                    ],
                "warnings":
                    row[
                        "extraction_warnings"
                    ],
                "financial_candidates":
                    json.loads(
                        row[
                            "financial_candidates"
                        ]
                    ),
            }
            for row in rows
        ],
    }

    AUDIT.write_text(
        json.dumps(
            audit,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\n" + "=" * 65
    )

    print(
        "V4 VALIDATION SUMMARY"
    )

    print(
        "=" * 65
    )

    for field, count in audit[
        "field_coverage"
    ].items():

        print(
            f"{field}: {count}/20"
        )

    print(
        "\nID statuses:"
    )

    statuses = Counter(
        row[
            "tender_id_status"
        ]
        for row in rows
    )

    for status, count in sorted(
        statuses.items()
    ):

        print(
            f"  {status}: {count}"
        )

    print(
        "\nSaved:",
        OUTPUT.name,
    )

    print(
        "Saved:",
        AUDIT.name,
    )

    print(
        "Original metadata files unchanged."
    )


if __name__ == "__main__":
    main()
