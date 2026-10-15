
import csv
import re
from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

PORTAL_URL = "https://etenders.gov.in/eprocure/app"
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_FOLDER = BASE_DIR / "downloaded_tenders"
MAPPING_FILE = BASE_DIR / "tender_zip_mapping.csv"

DOWNLOAD_FOLDER.mkdir(exist_ok=True)

MAX_TENDERS = 3
TENDER_SELECTOR = 'table#table a[title="View Tender Information"]'

MAPPING_COLUMNS = [
    "portal",
    "portal_tnid",
    "tender_id",
    "tender_title",
    "zip_filename",
]

TENDER_ID_PATTERN = re.compile(
    r"\b\d{4}_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_\d+_\d+\b"
)

ZIP_ID_PATTERN = re.compile(r"^work_(\d+)\.zip$", re.I)


def wait_for_tender_list(page):
    page.locator(TENDER_SELECTOR).first.wait_for(
        state="visible",
        timeout=20000,
    )


def extract_tender_id(text):
    match = TENDER_ID_PATTERN.search(text)
    return match.group(0) if match else ""


def read_existing_mappings():
    mappings = {}

    if not MAPPING_FILE.exists():
        return mappings

    with MAPPING_FILE.open(
        "r", newline="", encoding="utf-8-sig"
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            zip_filename = row.get("zip_filename", "")

            if zip_filename:
                mappings[zip_filename] = {
                    column: row.get(column, "")
                    for column in MAPPING_COLUMNS
                }

    return mappings


def save_mappings(mappings):
    with MAPPING_FILE.open(
        "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=MAPPING_COLUMNS,
        )

        writer.writeheader()
        writer.writerows(mappings.values())


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

        input("Once the results appear, press ENTER...")

        try:
            wait_for_tender_list(page)
            return True

        except PlaywrightTimeoutError:
            return False


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

    with page.expect_download(timeout=5000) as download_info:
        zip_link.click()

    return download_info.value


def download_with_captcha(page):
    try:
        return download_tender_zip(page)

    except PlaywrightTimeoutError:
        print("No immediate download detected.")

        captcha_input = page.locator('input[type="text"]')

        if captcha_input.count() > 0:
            print("CAPTCHA verification may be required.")
            print("Complete CAPTCHA and click Submit manually.")

            input(
                "Once back on tender details, press ENTER..."
            )

            zip_link = page.get_by_text(
                "Download as zip file",
                exact=False,
            )

            with page.expect_download(
                timeout=60000
            ) as download_info:
                zip_link.click()

            return download_info.value

        raise


def capture_tender_info(link):
    full_text = link.locator("xpath=..").inner_text()

    # The full tender ID is normally displayed
    # in the same table cell as the tender title.
    tender_id = extract_tender_id(full_text)

    title = link.inner_text().strip()

    return {
        "tender_id": tender_id,
        "tender_title": title,
    }


def record_tender_mapping(mappings, info, zip_filename):
    match = ZIP_ID_PATTERN.match(zip_filename)

    portal_tnid = match.group(1) if match else ""

    mappings[zip_filename] = {
        "portal": "etenders.gov.in",
        "portal_tnid": portal_tnid,
        "tender_id": info["tender_id"],
        "tender_title": info["tender_title"],
        "zip_filename": zip_filename,
    }

    save_mappings(mappings)

    print(
        f"Mapped: {info['tender_id'] or 'ID not found'} "
        f"-> {zip_filename}"
    )


def main():
    mappings = read_existing_mappings()

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
            
            page.locator('input[type="radio"][id="published"]').check(timeout=15000)

            print(
                "Complete the search CAPTCHA "
                "and click Search."
            )

            input(
                "\nOnce the tender list appears, "
                "press ENTER..."
            )

            wait_for_tender_list(page)

            count = page.locator(TENDER_SELECTOR).count()
            total = min(count, MAX_TENDERS)

            print(
                f"\nFound {count} tenders. "
                f"Processing {total}."
            )

            successful = 0

            for index in range(total):
                print(
                    f"\n--- Tender {index + 1}/{total} ---"
                )

                try:
                    wait_for_tender_list(page)

                    links = page.locator(TENDER_SELECTOR)
                    link = links.nth(index)

                    info = capture_tender_info(link)

                    print("Opening:", info["tender_title"])
                    print(
                        "Tender ID:",
                        info["tender_id"] or "Not found",
                    )

                    link.click()

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

                    successful += 1

                except Exception as error:
                    print(
                        f"Error on tender {index + 1}: "
                        f"{error}"
                    )

                if index < total - 1:
                    if not restore_tender_list(page):
                        print(
                            "Could not restore tender list."
                        )
                        break

            print(
                f"\nBatch finished: "
                f"{successful}/{total}"
            )
            print(
                f"Mappings saved to: {MAPPING_FILE.name}"
            )

        finally:
            input(
                "\nPress ENTER to close browser..."
            )
            browser.close()


if __name__ == "__main__":
    main()
