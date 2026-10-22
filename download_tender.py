
import csv
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

# ============================================================
# CONFIGURATION
# ============================================================

PORTAL_URL = "https://etenders.gov.in/eprocure/app"

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_FOLDER = BASE_DIR / "downloaded_tenders"
MAPPING_FILE = BASE_DIR / "tender_zip_mapping.csv"
STATUS_FILE = BASE_DIR / "tender_download_status.csv"

DOWNLOAD_FOLDER.mkdir(exist_ok=True)

MAX_PAGES = 2
MAX_TENDERS = 20

TENDER_SELECTOR = 'table#table a[title="View Tender Information"]'
NEXT_SELECTOR = "#loadNext"

MAPPING_COLUMNS = [
    "portal",
    "portal_tnid",
    "tender_id",
    "tender_title",
    "zip_filename",
]

STATUS_COLUMNS = [
    "tender_id",
    "tender_title",
    "download_status",
    "download_start_date",
    "download_end_date",
    "zip_filename",
    "last_checked",
    "error_details",
]

TENDER_ID_PATTERN = re.compile(
    r"\b\d{4}_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_\d+_\d+\b"
)

ZIP_ID_PATTERN = re.compile(r"^work_(\d+)\.zip$", re.I)


# ============================================================
# CSV FUNCTIONS
# ============================================================

def read_records(path, key_column):
    records = {}

    if not path.exists():
        return records

    with path.open(
        "r", newline="", encoding="utf-8-sig"
    ) as file:
        for row in csv.DictReader(file):
            key = row.get(key_column, "").strip()

            if key:
                records[key] = row

    return records


def save_records(path, columns, records):
    with path.open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(records.values())


def read_statuses():
    statuses = {}

    if not STATUS_FILE.exists():
        return statuses

    with STATUS_FILE.open(
        "r", newline="", encoding="utf-8-sig"
    ) as file:
        for row in csv.DictReader(file):
            key = (
                row.get("tender_id", "").strip()
                or row.get("tender_title", "").strip()
            )

            if key:
                statuses[key] = row

    return statuses


# ============================================================
# TENDER UTILITIES
# ============================================================

def wait_for_tender_list(page):
    page.locator(TENDER_SELECTOR).first.wait_for(
        state="visible",
        timeout=20000,
    )


def extract_tender_id(text):
    match = TENDER_ID_PATTERN.search(text)
    return match.group(0) if match else ""


def capture_tender_info(link):
    full_text = link.locator("xpath=..").inner_text()

    return {
        "tender_id": extract_tender_id(full_text),
        "tender_title": link.inner_text().strip(),
    }


def tender_key(info):
    return info["tender_id"] or info["tender_title"]


def first_tender_id(page):
    wait_for_tender_list(page)

    return capture_tender_info(
        page.locator(TENDER_SELECTOR).first
    )["tender_id"]


# ============================================================
# PAGINATION
# ============================================================

def go_to_next_page(page):
    print("\nMoving to next results page...")

    old_first_id = first_tender_id(page)

    next_link = page.locator(NEXT_SELECTOR)

    if next_link.count() == 0:
        print("No Next link found.")
        return False

    try:
        next_link.click(timeout=15000)

        wait_for_tender_list(page)

        page.wait_for_function(
            """({selector, previousId}) => {
                const link = document.querySelector(selector);
                if (!link) return false;

                const cell = link.parentElement;
                const text = cell ? cell.innerText : "";

                const match = text.match(
                    /\\b\\d{4}_[A-Za-z0-9_]+_\\d+_\\d+\\b/
                );

                return match && match[0] !== previousId;
            }""",
            arg={
                "selector": TENDER_SELECTOR,
                "previousId": old_first_id,
            },
            timeout=20000,
        )

        new_first_id = first_tender_id(page)

        print("Previous first tender:", old_first_id)
        print("New first tender:", new_first_id)
        print("Next page loaded successfully!")

        return True

    except Exception as error:
        print("Pagination failed:", error)
        return False


# ============================================================
# RETURN TO RESULTS
# ============================================================

def restore_tender_list(page, expected_first_id):
    try:
        print("Returning to tender list automatically...")

        back_link = page.locator('a[title="Back"]').last

        back_link.wait_for(
            state="visible",
            timeout=10000,
        )

        back_link.click(timeout=15000)

        wait_for_tender_list(page)

        current_first_id = first_tender_id(page)

        if current_first_id != expected_first_id:
            print("WARNING: Returned to a different results page.")
            print("Expected:", expected_first_id)
            print("Found:", current_first_id)
            return False

        print("Returned to correct tender list!")
        return True

    except Exception as error:
        print("Automatic navigation failed:", error)
        return False


# ============================================================
# DOCUMENT AVAILABILITY
# ============================================================

def get_download_dates(page):
    dates = {
        "download_start_date": "",
        "download_end_date": "",
    }

    fields = {
        "download_start_date":
            "Document Download / Sale Start Date",
        "download_end_date":
            "Document Download / Sale End Date",
    }

    for key, label in fields.items():
        try:
            cell = page.locator("td").filter(
                has_text=re.compile(
                    r"^\s*" + re.escape(label) + r"\s*$"
                )
            ).first

            if cell.count() > 0:
                dates[key] = cell.locator(
                    "xpath=following-sibling::td[1]"
                ).inner_text(timeout=3000).strip()

        except Exception:
            pass

    return dates


def check_download_availability(page):
    try:
        page.get_by_text(
            "Tender Details",
            exact=True,
        ).first.wait_for(
            state="visible",
            timeout=20000,
        )

        page.wait_for_load_state("domcontentloaded")

        if page.get_by_text(
            "Document download date is not begun yet",
            exact=False,
        ).count() > 0:
            return "not_yet_available"

        if page.get_by_text(
            "Document download date is over",
            exact=False,
        ).count() > 0:
            return "download_closed"

        if page.get_by_text(
            "Download as zip file",
            exact=False,
        ).count() > 0:
            return "available"

        return "download_unavailable"

    except PlaywrightTimeoutError:
        return "page_load_failed"


# ============================================================
# DOWNLOAD WITH RETRY
# ============================================================

def download_with_captcha(page):
    zip_link = page.get_by_text(
        "Download as zip file",
        exact=False,
    )

    for attempt in range(1, 3):
        try:
            print(f"Download attempt {attempt}/2...")

            with page.expect_download(
                timeout=15000
            ) as download_info:
                zip_link.click()

            return download_info.value

        except PlaywrightTimeoutError:
            print("No download detected within 15 seconds.")

            if (
                zip_link.count() == 0
                or not zip_link.is_visible()
            ):
                print(
                    "Verification or portal navigation detected."
                )

                input(
                    "Complete any CAPTCHA manually and "
                    "return to Tender Details, "
                    "then press ENTER..."
                )

                zip_link = page.get_by_text(
                    "Download as zip file",
                    exact=False,
                )

            elif attempt == 1:
                print("Download timed out. Retrying once...")
                page.wait_for_timeout(2000)

    raise RuntimeError(
        "download_failed: No ZIP download after two attempts."
    )


# ============================================================
# MAPPING AND STATUS
# ============================================================

def record_tender_mapping(mappings, info, zip_filename):
    match = ZIP_ID_PATTERN.match(zip_filename)

    mappings[zip_filename] = {
        "portal": "etenders.gov.in",
        "portal_tnid": match.group(1) if match else "",
        "tender_id": info["tender_id"],
        "tender_title": info["tender_title"],
        "zip_filename": zip_filename,
    }

    save_records(
        MAPPING_FILE,
        MAPPING_COLUMNS,
        mappings,
    )

    print(
        f"Mapped: {info['tender_id'] or 'ID not found'} "
        f"-> {zip_filename}"
    )


def record_status(
    statuses,
    info,
    status,
    dates,
    zip_filename="",
    error_details="",
):
    key = tender_key(info)

    previous = statuses.get(key, {})

    statuses[key] = {
        "tender_id": info["tender_id"],
        "tender_title": info["tender_title"],
        "download_status": status,
        "download_start_date": (
            dates.get("download_start_date")
            or previous.get("download_start_date", "")
        ),
        "download_end_date": (
            dates.get("download_end_date")
            or previous.get("download_end_date", "")
        ),
        "zip_filename": (
            zip_filename
            or previous.get("zip_filename", "")
        ),
        "last_checked": (
            datetime.now()
            .astimezone()
            .isoformat(timespec="seconds")
        ),
        "error_details": error_details,
    }

    save_records(
        STATUS_FILE,
        STATUS_COLUMNS,
        statuses,
    )

    print(f"Status recorded: {status}")


def is_already_downloaded(info, statuses):
    existing = statuses.get(tender_key(info), {})

    if existing.get("download_status") != "downloaded":
        return False

    zip_filename = existing.get("zip_filename", "")

    if not zip_filename:
        return False

    return (DOWNLOAD_FOLDER / zip_filename).is_file()


# ============================================================
# PROCESS A SINGLE TENDER
# ============================================================

def process_tender(page, info, mappings, statuses):
    dates = {
        "download_start_date": "",
        "download_end_date": "",
    }

    try:
        if is_already_downloaded(info, statuses):
            print("SKIPPED: already downloaded")
            return "already_downloaded", False

        page.locator(TENDER_SELECTOR).filter(
            has_text=info["tender_title"]
        ).first.click(timeout=15000)

        status = check_download_availability(page)
        dates = get_download_dates(page)

        if status != "available":
            print(f"SKIPPED: {status}")

            record_status(
                statuses,
                info,
                status,
                dates,
            )

            return status, True

        download = download_with_captcha(page)

        destination = (
            DOWNLOAD_FOLDER / download.suggested_filename
        )

        if destination.exists():
            print("Already downloaded:", destination.name)
        else:
            download.save_as(destination)
            print("SUCCESS:", destination.name)

        record_tender_mapping(
            mappings,
            info,
            destination.name,
        )

        record_status(
            statuses,
            info,
            "downloaded",
            dates,
            zip_filename=destination.name,
        )

        return "downloaded", True

    except Exception as error:
        print("Tender processing error:", error)

        record_status(
            statuses,
            info,
            "download_failed",
            dates,
            error_details=str(error),
        )

        return "failed", True


# ============================================================
# MAIN PIPELINE
# ============================================================

def main():
    mappings = read_records(
        MAPPING_FILE,
        "zip_filename",
    )

    statuses = read_statuses()

    stats = {
        "downloaded": 0,
        "already_downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "processed": 0,
    }

    seen_tenders = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            page.goto(
                PORTAL_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.get_by_role(
                "link",
                name="Active Tenders",
            ).click(timeout=20000)

            page.locator(
                'input[type="radio"][id="published"]'
            ).check(timeout=15000)

            print(
                "Complete the search CAPTCHA "
                "and click Search."
            )

            input(
                "\nOnce the tender list appears, "
                "press ENTER..."
            )

            wait_for_tender_list(page)

            for page_number in range(1, MAX_PAGES + 1):
                if stats["processed"] >= MAX_TENDERS:
                    break

                wait_for_tender_list(page)

                page_first_id = first_tender_id(page)

                count = page.locator(TENDER_SELECTOR).count()

                print("\n" + "=" * 50)
                print(f"PROCESSING RESULTS PAGE {page_number}")
                print(f"Tenders found: {count}")
                print("=" * 50)

                # Snapshot the listing before opening details.
                # This avoids relying on changing DOM positions.
                page_tenders = []

                for index in range(count):
                    link = page.locator(
                        TENDER_SELECTOR
                    ).nth(index)

                    info = capture_tender_info(link)
                    page_tenders.append(info)

                page_navigation_ok = True

                for index, info in enumerate(page_tenders):
                    if stats["processed"] >= MAX_TENDERS:
                        break

                    key = tender_key(info)

                    if key in seen_tenders:
                        print("Duplicate tender skipped:", key)
                        continue

                    seen_tenders.add(key)

                    stats["processed"] += 1

                    print(
                        f"\n--- Tender {stats['processed']}/"
                        f"{MAX_TENDERS} ---"
                    )

                    print("Opening:", info["tender_title"])
                    print(
                        "Tender ID:",
                        info["tender_id"] or "Not found",
                    )

                    result, opened_details = process_tender(
                        page,
                        info,
                        mappings,
                        statuses,
                    )

                    if result == "downloaded":
                        stats["downloaded"] += 1

                    elif result == "already_downloaded":
                        stats["already_downloaded"] += 1

                    elif result == "failed":
                        stats["failed"] += 1

                    else:
                        stats["skipped"] += 1

                    if opened_details:
                        if not restore_tender_list(
                            page,
                            page_first_id,
                        ):
                            print(
                                "Stopping to avoid processing "
                                "the wrong results page."
                            )
                            page_navigation_ok = False
                            break

                if not page_navigation_ok:
                    break

                if page_number >= MAX_PAGES:
                    break

                if stats["processed"] >= MAX_TENDERS:
                    break

                if not go_to_next_page(page):
                    print("No further page available.")
                    break

            print("\n" + "=" * 50)
            print("FINAL BATCH SUMMARY")
            print("=" * 50)

            print("Tenders examined:", stats["processed"])
            print("Newly processed:", stats["downloaded"])
            print(
                "Already downloaded:",
                stats["already_downloaded"],
            )
            print("Other skipped:", stats["skipped"])
            print("Failed:", stats["failed"])

            print("\nMappings:", MAPPING_FILE.name)
            print("Statuses:", STATUS_FILE.name)

        finally:
            try:
                input("\nPress ENTER to close browser...")
            except (EOFError, KeyboardInterrupt):
                pass

            browser.close()


if __name__ == "__main__":
    main()
