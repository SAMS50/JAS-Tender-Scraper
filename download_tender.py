
import csv
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

PORTAL_URL = "https://etenders.gov.in/eprocure/app"

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_FOLDER = BASE_DIR / "downloaded_tenders"
MAPPING_FILE = BASE_DIR / "tender_zip_mapping.csv"
STATUS_FILE = BASE_DIR / "tender_download_status.csv"

DOWNLOAD_FOLDER.mkdir(exist_ok=True)

MAX_TENDERS = 10
TENDER_SELECTOR = 'table#table a[title="View Tender Information"]'

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


def read_csv_records(path, key_column):
    records = {}

    if not path.exists():
        return records

    with path.open("r", newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            key = row.get(key_column, "").strip()

            if key:
                records[key] = row

    return records


def save_csv_records(path, columns, records):
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(records.values())


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


def restore_tender_list(page):
    try:
        print("Returning to tender list automatically...")

        back_link = page.locator('a[title="Back"]').last

        back_link.wait_for(
            state="visible",
            timeout=10000,
        )

        back_link.click(timeout=15000)
        wait_for_tender_list(page)

        print("Returned to tender list successfully!")
        return True

    except Exception as error:
        print(f"Automatic navigation failed: {error}")

        print("\nPlease restore the tender list manually:")
        print("1. Open Active Tenders.")
        print("2. Select Published Date.")
        print("3. Complete the search CAPTCHA and click Search.")

        input("Once results appear, press ENTER...")

        try:
            wait_for_tender_list(page)
            return True
        except PlaywrightTimeoutError:
            return False


def get_download_dates(page):
    dates = {
        "download_start_date": "",
        "download_end_date": "",
    }

    fields = {
        "download_start_date": "Document Download / Sale Start Date",
        "download_end_date": "Document Download / Sale End Date",
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

        zip_link = page.get_by_text(
            "Download as zip file",
            exact=False,
        )

        if zip_link.count() > 0:
            return "available"

        return "download_unavailable"

    except PlaywrightTimeoutError:
        return "page_load_failed"


def download_tender_zip(page):
    zip_link = page.get_by_text(
        "Download as zip file",
        exact=False,
    )

    zip_link.wait_for(
        state="visible",
        timeout=20000,
    )

    print("Requesting ZIP download...")

    with page.expect_download(timeout=15000) as download_info:
        zip_link.click()

    return download_info.value



def download_with_captcha(page):
    zip_link = page.get_by_text(
        "Download as zip file",
        exact=False,
    )

    for attempt in range(1, 3):
        try:
            print(f"Download attempt {attempt}/2...")

            with page.expect_download(timeout=15000) as download_info:
                zip_link.click()

            return download_info.value

        except PlaywrightTimeoutError:
            print("No download detected within 15 seconds.")

            # Check whether the download link is still available.
            # If not, the portal may have opened a CAPTCHA
            # verification page.
            if zip_link.count() == 0 or not zip_link.is_visible():
                print("Verification or portal navigation detected.")

                input(
                    "Complete any CAPTCHA manually and return "
                    "to Tender Details, then press ENTER..."
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

def record_tender_mapping(mappings, info, zip_filename):
    match = ZIP_ID_PATTERN.match(zip_filename)

    mappings[zip_filename] = {
        "portal": "etenders.gov.in",
        "portal_tnid": match.group(1) if match else "",
        "tender_id": info["tender_id"],
        "tender_title": info["tender_title"],
        "zip_filename": zip_filename,
    }

    save_csv_records(
        MAPPING_FILE,
        MAPPING_COLUMNS,
        mappings,
    )

    print(
        f"Mapped: {info['tender_id'] or 'ID not found'} "
        f"-> {zip_filename}"
    )


def record_status(statuses, info, status, dates,
                  zip_filename="", error_details=""):

    tender_id = info["tender_id"]

    # Keep records separate even if a tender ID was not found.
    key = tender_id or info["tender_title"]

    statuses[key] = {
        "tender_id": tender_id,
        "tender_title": info["tender_title"],
        "download_status": status,
        "download_start_date": dates.get(
            "download_start_date", ""
        ),
        "download_end_date": dates.get(
            "download_end_date", ""
        ),
        "zip_filename": zip_filename,
        "last_checked": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "error_details": error_details,
    }

    save_csv_records(
        STATUS_FILE,
        STATUS_COLUMNS,
        statuses,
    )

    print(f"Status recorded: {status}")


def main():
    mappings = read_csv_records(
        MAPPING_FILE, "zip_filename"
    )

    statuses = {}

    if STATUS_FILE.exists():
        with STATUS_FILE.open(
            "r", newline="", encoding="utf-8-sig"
        ) as file:
            for row in csv.DictReader(file):
                key = row.get("tender_id") or row.get(
                    "tender_title", ""
                )

                if key:
                    statuses[key] = row

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
                "Complete the search CAPTCHA and click Search."
            )

            input(
                "\nOnce the tender list appears, press ENTER..."
            )

            wait_for_tender_list(page)

            count = page.locator(TENDER_SELECTOR).count()
            total = min(count, MAX_TENDERS)

            print(
                f"\nFound {count} tenders. Processing {total}."
            )

            successful = 0
            skipped = 0
            failed = 0

            for index in range(total):
                print(
                    f"\n--- Tender {index + 1}/{total} ---"
                )

                info = None
                dates = {
                    "download_start_date": "",
                    "download_end_date": "",
                }

                try:
                    wait_for_tender_list(page)

                    link = page.locator(
                        TENDER_SELECTOR
                    ).nth(index)

                    info = capture_tender_info(link)

                    print("Opening:", info["tender_title"])
                    print(
                        "Tender ID:",
                        info["tender_id"] or "Not found",
                    )

                    link.click()

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

                        skipped += 1

                    else:
                        download = download_with_captcha(page)

                        destination = (
                            DOWNLOAD_FOLDER
                            / download.suggested_filename
                        )

                        if destination.exists():
                            print(
                                f"Already downloaded: "
                                f"{destination.name}"
                            )
                        else:
                            download.save_as(destination)
                            print(
                                f"SUCCESS: {destination.name}"
                            )

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

                        successful += 1

                except Exception as error:
                    failed += 1

                    print(
                        f"Error on tender {index + 1}: "
                        f"{error}"
                    )

                    if info:
                        record_status(
                            statuses,
                            info,
                            "error",
                            dates,
                            error_details=str(error),
                        )

                if index < total - 1:
                    if not restore_tender_list(page):
                        print(
                            "Could not restore tender list."
                        )
                        break

            print("\n" + "=" * 45)
            print("BATCH SUMMARY")
            print("=" * 45)

            print(f"Downloaded: {successful}")
            print(f"Skipped: {skipped}")
            print(f"Failed: {failed}")

            print(f"\nZIP mappings: {MAPPING_FILE.name}")
            print(f"Download statuses: {STATUS_FILE.name}")

        finally:
            input("\nPress ENTER to close browser...")
            browser.close()


if __name__ == "__main__":
    main()
