"""
Download Orders Agent
=====================
A Playwright-based web automation agent that:
  1. Opens http://www.bengalremedies.com:9000
  2. Logs in with configured credentials (emailId / password)
  3. Navigates: Procurement → Orders
  4. Selects exact filter option: Status = "Initiated" (skipping "To Be Initiated")
  5. Screen-captures all populated product tables directly from the webpage
  6. Navigates through ALL pagination pages until the last page
  7. Compiles all accumulated product records into an Excel (.xlsx) file
  8. Saves the file to: /Users/debz/Documents/BR/Project/Ben_Rem/excel_marg/latest
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


async def _extract_screen_table_rows(page) -> tuple[list[str], list[dict]]:
    """
    Extracts headers and data rows directly from the visible screen table.
    Filters out checkbox column ('Select Order') and action buttons ('Actions').
    """
    # 1. Extract table headers
    th_elements = await page.locator("table thead th").all()
    headers = [" ".join((await th.inner_text()).split()) for th in th_elements]
    data_headers = [h for h in headers if h and h not in ("Select Order", "Actions")]

    if not data_headers:
        data_headers = [
            "Product Name", "Composition", "Manufacture Name", "Order Date",
            "Manufacturing For", "Purchase Net Rate", "Order Qty", "Receive Qty",
            "Difrence Qty", "M.R.P.", "Sale Rate", "Bil Date", "Bill No.",
            "NoOfCase", "Due Days", "Status", "Initiated By", "Comment"
        ]

    # 2. Extract table body cells
    rows = []
    tr_elements = await page.locator("table tbody tr").all()
    for tr in tr_elements:
        td_elements = await tr.locator("td").all()
        td_texts = [" ".join((await td.inner_text()).split()) for td in td_elements]
        if not td_texts or not any(td_texts):
            continue

        row_dict = {}
        for idx, col_name in enumerate(data_headers):
            row_dict[col_name] = td_texts[idx] if idx < len(td_texts) else ""
        rows.append(row_dict)

    return data_headers, rows


async def run_download_agent(
    output_dir: str | Path = OUTPUT_DIR,
    headless: bool = HEADLESS,
    status_filter: str = "Initiated",
) -> dict:
    """
    Executes screen table scraping across all pagination pages with 'Initiated' filter.
    Does NOT use the download button; captures all data directly from the webpage.
    """
    global _progress_log
    _progress_log = []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    _emit("🚀 Starting Download Orders Agent (Screen Capture Mode)...")
    _emit(f"   Target URL: {TARGET_URL}")
    _emit(f"   Target Status: '{status_filter}' (Exact Match)")
    _emit(f"   Save Directory: {output_dir}")
    _emit(f"   Browser View: {'Headless (Silent)' if headless else 'Visible (Interactive)'}")

    all_scraped_rows: list[dict] = []
    final_headers: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
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
            email_field = page.locator('input[name="emailId"], input[type="email"], #email').first
            await email_field.wait_for(state="visible", timeout=10000)
            await email_field.fill(LOGIN_EMAIL)
            _emit("   ✅ Email entered: debnathch@gmail.com")

            password_field = page.locator('input[name="password"], input[type="password"], #password').first
            await password_field.fill(LOGIN_PASSWORD)
            _emit("   ✅ Password entered.")

            submit_btn = page.locator('button[type="submit"], button:has-text("Login")').first
            await submit_btn.click()
            _emit("   ✅ Clicked Login button. Waiting for dashboard...")
            await page.wait_for_timeout(3500)
            _emit(f"   📍 Dashboard loaded: {page.url}")

            # ── 3. Navigate to Procurement → Orders ───────────────────────────
            _emit("📂 Navigating to Procurement Orders...")
            proc_nav = page.get_by_text("Procurement", exact=False).first
            if await proc_nav.is_visible(timeout=5000):
                await proc_nav.click()
                await page.wait_for_timeout(2000)

            orders_link = page.get_by_text("Orders", exact=True).first
            if await orders_link.is_visible(timeout=3000):
                await orders_link.click()
                await page.wait_for_timeout(3000)
            else:
                await page.goto(f"{TARGET_URL}/procurement/procurement-order", timeout=15000)
                await page.wait_for_timeout(3000)

            _emit(f"   📍 Orders page reached: {page.url}")

            # ── 4. Filter by Status = EXACT "Initiated" ───────────────────────
            _emit(f"🔍 Applying exact filter for Status = '{status_filter}'...")
            status_input = page.locator('input[placeholder="Filter By Status"]').first
            await status_input.wait_for(state="visible", timeout=8000)
            await status_input.click()
            await page.wait_for_timeout(1000)

            # Look through autocomplete options to find the EXACT match 'Initiated'
            # (Ensures 'To Be Initiated' is never selected)
            all_options = await page.locator('[role="option"], li.MuiAutocomplete-option, li').all()
            matched_option = None
            for opt in all_options:
                txt = (await opt.inner_text()).strip()
                if txt.lower() == status_filter.lower():
                    matched_option = opt
                    break

            if matched_option:
                await matched_option.click()
                _emit(f"   ✅ Successfully selected exact option: '{status_filter}'")
            else:
                # Fallback: type 'Initiated' and press enter
                await status_input.fill(status_filter)
                await page.wait_for_timeout(500)
                await page.keyboard.press("ArrowDown")
                await page.keyboard.press("Enter")
                _emit(f"   ✅ Filtered via keyboard input for: '{status_filter}'")

            await page.wait_for_timeout(3000)

            # ── 5. Screen Capture All Tables Across All Pages ──────────────────
            _emit("📊 Starting screen table capture across all pagination pages...")
            page_index = 1

            while True:
                _emit(f"   📄 Scraping Page {page_index} table...")
                headers, page_rows = await _extract_screen_table_rows(page)
                if not final_headers and headers:
                    final_headers = headers

                if page_rows:
                    all_scraped_rows.extend(page_rows)
                    _emit(f"   ✅ Page {page_index}: Captured {len(page_rows)} rows (Total accumulated: {len(all_scraped_rows)})")
                else:
                    _emit(f"   ⚠️ Page {page_index}: No rows found.")
                    break

                # Pagination: check 'Go to next page' button
                next_btn = page.locator('button[aria-label="Go to next page"]').first
                is_visible = await next_btn.is_visible(timeout=2000)
                is_disabled = await next_btn.is_disabled() if is_visible else True

                if is_visible and not is_disabled:
                    await next_btn.click()
                    await page.wait_for_timeout(1500)
                    page_index += 1
                else:
                    _emit(f"   🏁 Reached final page (Page {page_index}). Pagination completed.")
                    break

        except Exception as e:
            _emit(f"❌ Error during screen capture automation: {e}")
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

    # ── 6. Save Screen-Captured Data to Excel (.xlsx) ─────────────────────────
    if not all_scraped_rows:
        _emit("⚠️ No records were captured from the screen tables.")
        return {
            "success": False,
            "file_path": None,
            "rows_downloaded": 0,
            "message": "No product records were captured from the screen tables for Status = Initiated.",
            "log": _progress_log,
        }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_filename = f"initiated_orders_{timestamp}.xlsx"
    target_path = output_dir / target_filename

    _emit(f"💾 Compiling all {len(all_scraped_rows)} captured product records into Excel (.xlsx)...")
    df = pd.DataFrame(all_scraped_rows)

    # Reorder columns cleanly if headers are available
    if final_headers:
        ordered_cols = [c for c in final_headers if c in df.columns] + [c for c in df.columns if c not in final_headers]
        df = df[ordered_cols]

    with pd.ExcelWriter(str(target_path), engine="openpyxl") as writer:
        sheet_name = "Initiated Orders"
        df.to_excel(writer, sheet_name=sheet_name, index=False)

        # Auto-size columns for read-friendly Excel view
        ws = writer.sheets[sheet_name]
        for col_cells in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col_cells), default=10)
            col_letter = col_cells[0].column_letter
            ws.column_dimensions[col_letter].width = min(max_len + 4, 60)

    _emit(f"🎉 Complete! All {len(all_scraped_rows)} records successfully saved to: {target_path}")

    return {
        "success": True,
        "file_path": str(target_path),
        "rows_downloaded": len(all_scraped_rows),
        "message": f"Successfully screen-captured all {len(all_scraped_rows)} product rows across {page_index} page(s) and saved to Excel.",
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
