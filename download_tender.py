
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
        timeout=15000
    )


def restore_tender_list(page):
    try:
        page.get_by_role("button", name="Back", exact=True).last.click(
            timeout=5000
        )
        wait_for_tender_list(page)
        print("Returned to tender list automatically.")
        return True

    except Exception:
        print("\nCould not restore the tender list automatically.")
        print("Please navigate back to Active Tenders.")
        print("Select Published Date and complete the search CAPTCHA.")
        input("Once the tender list appears, press ENTER...")

        try:
            wait_for_tender_list(page)
            print("Tender list restored.")
            return True
        except PlaywrightTimeoutError:
            print("Tender list still not detected.")
            return False


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

        print("In the browser:")
        print("1. Open Active Tenders.")
        print("2. Select Published Date.")
        print("3. Complete CAPTCHA and click Search.")

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
                print("Opening tender link:", links.nth(index).inner_text())
                links.nth(index).click()
                print("Current page URL:", page.url)

                zip_link = page.get_by_text(
                    "Download as zip file", exact=False
                )

                zip_link.wait_for(
                    state="visible",
                    timeout=20000
                )

                print("Opening download CAPTCHA...")
                zip_link.click()

                print("Complete CAPTCHA and click Submit.")
                input("Once back on tender details, press ENTER...")

                zip_link = page.get_by_text(
                    "Download as zip file", exact=False
                )

                with page.expect_download(timeout=60000) as download_info:
                    zip_link.click()

                download = download_info.value

                destination = (
                    DOWNLOAD_FOLDER / download.suggested_filename
                )

                if destination.exists():
                    print(f"Already downloaded: {destination.name}")
                else:
                    download.save_as(destination)
                    print(f"SUCCESS: {destination.name}")

                successful += 1

            except PlaywrightTimeoutError:
                print(f"Timeout on tender {index + 1}.")

            except Exception as error:
                print(f"Error on tender {index + 1}: {error}")

            if index < total - 1:
                if not restore_tender_list(page):
                    print("Stopping because tender list is unavailable.")
                    break

        print("\nBatch finished!")
        print(f"Successfully processed: {successful}/{total}")

    finally:
        input("\nPress ENTER to close browser...")
        browser.close()
