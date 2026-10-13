
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PORTAL_URL = "https://etenders.gov.in/eprocure/app"
DOWNLOAD_FOLDER = Path("downloaded_tenders").resolve()
DOWNLOAD_FOLDER.mkdir(exist_ok=True)

MAX_TENDERS = 3

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    try:
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)

        print("In the browser:")
        print("1. Open Active Tenders.")
        print("2. Select Published Date.")
        print("3. Enter the search CAPTCHA and click Search.")

        input("\nOnce the tender list appears, press ENTER...")

        tender_selector = 'table#table a[title="View Tender Information"]'
        links = page.locator(tender_selector)
        print("Waiting for tender results...")
        links.first.wait_for(state="visible", timeout=60000)
        count = links.count()
        total = min(count, MAX_TENDERS)

        print(f"\nFound {count} tenders. Processing {total}.")

        for index in range(total):
            print(f"\n--- Tender {index + 1}/{total} ---")

            try:
                links = page.locator(tender_selector)
                links.nth(index).click()

                zip_link = page.get_by_text(
                    "Download as zip file", exact=False
                )
                zip_link.wait_for(timeout=20000)

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
                destination = DOWNLOAD_FOLDER / download.suggested_filename

                if destination.exists():
                    print(f"Already downloaded: {destination.name}")
                else:
                    download.save_as(destination)
                    print(f"SUCCESS: {destination.name}")

                page.go_back(wait_until="domcontentloaded")

                print("Returned to tender list.")

            except PlaywrightTimeoutError:
                print(f"Timeout on tender {index + 1}.")
                print("Please check the browser before continuing.")
                break

            except Exception as error:
                print(f"Error on tender {index + 1}: {error}")
                break

        print("\nBatch finished!")

    finally:
        input("\nPress ENTER to close browser...")
        browser.close()
