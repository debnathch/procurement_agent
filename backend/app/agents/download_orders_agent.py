"""
Download Orders Agent
=====================
A Playwright-based web automation agent that:
  1. Opens http://www.bengalremedies.com:9000
  2. Logs in with configured credentials (emailId / password)
  3. Navigates: Procurement → Orders
  4. Applies filter: Status = "Initiated"
  5. Clicks "Download(Excel)" to download the official Excel report
  6. Falls back to table pagination scraping if download button is unavailable
  7. Saves the output to the user's chosen folder (/Users/debz/Documents/BR/Project/Ben_Rem/excel_marg/latest)
"""
import asyncio
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

# Ensure Playwright finds browser cache regardless of any HOME environment override
for _candidate_dir in [
    Path("/Users/debz/Library/Caches/ms-playwright"),
    Path.home() / "Library/Caches/ms-playwright",
    Path(os.path.expanduser("~")) / "Library/Caches/ms-playwright",
]:
    if _candidate_dir.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_candidate_dir))
        break

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ── Configuration Defaults ───────────────────────────────────────────────────
TARGET_URL      = "http://www.bengalremedies.com:9000"
LOGIN_EMAIL     = "debnathch@gmail.com"
LOGIN_PASSWORD  = "Br@2023"
OUTPUT_DIR      = Path("/Users/debz/Documents/BR/Project/Ben_Rem/excel_marg/latest")
HEADLESS        = False
PAGE_LOAD_WAIT  = 2500

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DOWNLOAD-AGENT] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("download_orders_agent")

_progress_log: list[str] = []

def _emit(msg: str):
    log.info(msg)
    _progress_log.append(msg)


async def _extract_table_rows(page) -> list[dict]:
    """Scrape visible table rows from the orders table."""
    rows = []
    try:
        header_cells = await page.locator("table thead th, table tr:first-child th").all()
        headers = [(await h.inner_text()).strip() for h in header_cells]
        headers = [h for h in headers if h]

        data_rows = await page.locator("table tbody tr").all()
        for row_el in data_rows:
            cells = await row_el.locator("td").all()
            cell_texts = [(await c.inner_text()).strip() for c in cells]
            if not any(cell_texts):
                continue
            if headers and len(headers) == len(cell_texts):
                rows.append(dict(zip(headers, cell_texts)))
            else:
                rows.append({f"col_{i+1}": v for i, v in enumerate(cell_texts)})
    except Exception as e:
        _emit(f"   ⚠️ Table extraction note: {e}")
    return rows


async def run_download_agent(
    output_dir: str | Path = OUTPUT_DIR,
    headless: bool = HEADLESS,
    status_filter: str = "Initiated",
) -> dict:
    """
    Executes the browser automation flow to download Initiated orders.
    """
    global _progress_log
    _progress_log = []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    _emit("🚀 Starting Download Orders Agent...")
    _emit(f"   Target URL: {TARGET_URL}")
    _emit(f"   Status Filter: {status_filter}")
    _emit(f"   Output Directory: {output_dir}")
    _emit(f"   Browser Mode: {'Headless (Silent)' if headless else 'Visible (Interactive)'}")

    downloaded_file: str | None = None
    row_count = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
            ignore_https_errors=True,
        )
        page = await context.new_page()

        try:
            # ── 1. Navigate to Portal ─────────────────────────────────────────
            _emit("🌐 Connecting to portal: http://www.bengalremedies.com:9000 ...")
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # ── 2. Login ──────────────────────────────────────────────────────
            _emit("🔐 Entering credentials...")

            # Email field (MUI text input named emailId)
            email_field = page.locator('input[name="emailId"], input[type="email"], #email').first
            await email_field.wait_for(state="visible", timeout=10000)
            await email_field.fill(LOGIN_EMAIL)
            _emit("   ✅ Email entered: debnathch@gmail.com")

            # Password field
            password_field = page.locator('input[name="password"], input[type="password"], #password').first
            await password_field.fill(LOGIN_PASSWORD)
            _emit("   ✅ Password entered.")

            # Submit
            submit_btn = page.locator('button[type="submit"], button:has-text("Login")').first
            await submit_btn.click()
            _emit("   ✅ Clicked Login button. Waiting for dashboard...")
            await page.wait_for_timeout(3500)

            _emit(f"   📍 Current location: {page.url}")

            # ── 3. Navigate to Procurement → Orders ───────────────────────────
            _emit("📂 Navigating to Procurement Orders...")
            
            # Click left navigation 'Procurement'
            proc_nav = page.get_by_text("Procurement", exact=False).first
            if await proc_nav.is_visible(timeout=5000):
                await proc_nav.click()
                await page.wait_for_timeout(2000)

            # Click 'Orders'
            orders_link = page.get_by_text("Orders", exact=True).first
            if await orders_link.is_visible(timeout=3000):
                await orders_link.click()
                await page.wait_for_timeout(3000)
            else:
                # Direct navigation fallback
                await page.goto(f"{TARGET_URL}/procurement/procurement-order", timeout=15000)
                await page.wait_for_timeout(3000)

            _emit(f"   📍 Orders page reached: {page.url}")

            # ── 4. Filter by Status = "Initiated" ─────────────────────────────
            _emit(f"🔍 Applying filter: Status = '{status_filter}'...")
            status_input = page.locator('input[placeholder="Filter By Status"]').first
            await status_input.wait_for(state="visible", timeout=8000)
            await status_input.click()
            await page.wait_for_timeout(1000)

            # Select 'Initiated' from the autocomplete dropdown
            opt_locator = page.locator('[role="option"], li.MuiAutocomplete-option, li').filter(has_text=status_filter).first
            await opt_locator.wait_for(state="visible", timeout=6000)
            await opt_locator.click()
            _emit("   ✅ 'Initiated' status selected! Waiting for filtered records to load...")
            await page.wait_for_timeout(3000)

            # ── 5. Download via "Download(Excel)" button ───────────────────────
            dl_button = page.get_by_text("Download(Excel)").first
            is_dl_available = await dl_button.is_visible(timeout=5000)

            if is_dl_available:
                _emit("📥 Found portal 'Download(Excel)' button! Triggering native Excel export...")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                target_filename = f"initiated_orders_{timestamp}.xlsx"
                target_path = output_dir / target_filename

                async with page.expect_download(timeout=20000) as download_info:
                    await dl_button.click()

                download = await download_info.value
                _emit(f"   📦 Portal generated export: {download.suggested_filename}")
                await download.save_as(str(target_path))
                _emit(f"   💾 Saved Excel file to: {target_path}")

                downloaded_file = str(target_path)

                # Inspect row count from the downloaded Excel
                try:
                    df = pd.read_excel(target_path)
                    row_count = len(df)
                    _emit(f"   📊 Verified Excel contents: {row_count} orders found.")
                except Exception as ex:
                    _emit(f"   ⚠️ Could not read row count from downloaded Excel: {ex}")
                    row_count = 1
            else:
                # ── Fallback: Table scraping across pages ──────────────────────
                _emit("⚠️ Native download button not found. Fallback: Scraping visible table pages...")
                all_rows = []
                page_idx = 1
                while True:
                    _emit(f"   📄 Scraping page {page_idx}...")
                    p_rows = await _extract_table_rows(page)
                    if p_rows:
                        all_rows.extend(p_rows)
                        _emit(f"   ✅ Collected {len(p_rows)} rows (Total: {len(all_rows)})")
                    else:
                        break

                    # Look for next pagination button
                    next_btn = page.locator("button[aria-label*='next' i], [class*='pagination'] button:last-child").first
                    if await next_btn.is_visible(timeout=1500) and not await next_btn.is_disabled():
                        await next_btn.click()
                        await page.wait_for_timeout(2000)
                        page_idx += 1
                    else:
                        break

                if all_rows:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    target_path = output_dir / f"initiated_orders_{timestamp}.xlsx"
                    df = pd.DataFrame(all_rows)
                    df.to_excel(str(target_path), index=False)
                    downloaded_file = str(target_path)
                    row_count = len(all_rows)
                    _emit(f"   💾 Scraped data saved to: {target_path}")

        except Exception as e:
            _emit(f"❌ Error during automation: {e}")
            await browser.close()
            return {
                "success": False,
                "file_path": None,
                "rows_downloaded": 0,
                "message": str(e),
                "log": _progress_log,
            }
        finally:
            await browser.close()

    if downloaded_file and os.path.exists(downloaded_file):
        _emit(f"🎉 Complete! Initiated orders saved at: {downloaded_file}")
        return {
            "success": True,
            "file_path": downloaded_file,
            "rows_downloaded": row_count,
            "message": f"Successfully downloaded initiated orders ({row_count} records) to Excel.",
            "log": _progress_log,
        }
    else:
        return {
            "success": False,
            "file_path": None,
            "rows_downloaded": 0,
            "message": "No file was generated or downloaded.",
            "log": _progress_log,
        }


# ── Standalone CLI Entrypoint ─────────────────────────────────────────────────
if __name__ == "__main__":
    res = asyncio.run(run_download_agent(headless=False))
    print("\n" + "=" * 60)
    print("SUCCESS:", res["success"])
    print("FILE:", res.get("file_path"))
    print("ROWS:", res.get("rows_downloaded"))
    sys.exit(0 if res["success"] else 1)
