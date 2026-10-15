
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PORTAL_URL = "https://etenders.gov.in/eprocure/app"
DOWNLOAD_FOLDER = Path("downloaded_tenders").resolve()
DOWNLOAD_FOLDER.mkdir(exist_ok=True)

MAX_TENDERS = 3
TENDER_SELECTOR = 'table#table a[title="View Tender Information"]'


def wait_for_tender_list(page):
    page.locator(TENDER_SELECTOR).first.wait_for(
        state="visible",
        timeout=20000
    )



def restore_tender_list(page):
    try:
        print("Returning to tender list automatically...")

        back_link = page.locator('a[title="Back"]').last

        back_link.wait_for(state="visible", timeout=10000)
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
        "Download as zip file", exact=False
    )

    zip_link.wait_for(state="visible", timeout=20000)

    print("Requesting ZIP download...")

    # A download can begin immediately or after CAPTCHA verification.
    with page.expect_event("download", timeout=5000) as download_wait:
        zip_link.click()

    return download_wait.value


def download_with_captcha(page):
    try:
        return download_tender_zip(page)

    except PlaywrightTimeoutError:
        print("No immediate download detected.")

        captcha_input = page.locator(
            'input[type="text"]'
        )

        if captcha_input.count() > 0:
            print("CAPTCHA verification may be required.")
            print("Complete CAPTCHA and click Submit manually.")
            input("Once back on tender details, press ENTER...")

            zip_link = page.get_by_text(
                "Download as zip file", exact=False
            )

            with page.expect_download(timeout=60000) as download_info:
                zip_link.click()

            return download_info.value

        raise


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    try:
        page.goto(
            PORTAL_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )
        page.get_by_role("link", name="Active Tenders").click(timeout=20000)
        page.get_by_text("Published Date", exact=True).click(timeout=10000)

        print("Open Active Tenders, select Published Date,")
        print("complete the search CAPTCHA and click Search.")

        input("\nOnce the tender list appears, press ENTER...")

        wait_for_tender_list(page)

        count = page.locator(TENDER_SELECTOR).count()
        total = min(count, MAX_TENDERS)

        print(f"\nFound {count} tenders. Processing {total}.")

        successful = 0

        for index in range(total):
            print(f"\n--- Tender {index + 1}/{total} ---")

            try:
                wait_for_tender_list(page)

                links = page.locator(TENDER_SELECTOR)
                print("Opening:", links.nth(index).inner_text())

                links.nth(index).click()

                download = download_with_captcha(page)

                destination = (
                    DOWNLOAD_FOLDER / download.suggested_filename
                )

                if destination.exists():
                    print(f"Already downloaded: {destination.name}")
                else:
                    download.save_as(destination)
                    print(f"SUCCESS: {destination.name}")

                successful += 1

            except Exception as error:
                print(f"Error on tender {index + 1}: {error}")

            if index < total - 1:
                if not restore_tender_list(page):
                    print("Could not restore tender list.")
                    break

        print(f"\nBatch finished: {successful}/{total}")

    finally:
        input("\nPress ENTER to close browser...")
        browser.close()
