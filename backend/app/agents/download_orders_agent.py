"""
Download Orders Agent
=====================
A Playwright-based web automation agent that:
  1. Opens http://www.bengalremedies.com:9000
  2. Logs in with configured credentials
  3. Navigates: Left Panel → Procurement → Order
  4. Filters by status "Initiated"
  5. Scrapes ALL paginated table rows
  6. Saves the result as Excel (.xlsx) to a specified output directory

Usage (standalone):
    python -m backend.app.agents.download_orders_agent

Or via FastAPI endpoint:
    POST /procurement/download-initiated-orders
"""
import asyncio
import sys
import os
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

# Ensure Playwright finds browser cache regardless of any HOME environment override
for _candidate_dir in [
    Path("/Users/debz/Library/Caches/ms-playwright"),
    Path.home() / "Library/Caches/ms-playwright",
    Path(os.path.expanduser("~")) / "Library/Caches/ms-playwright",
]:
    if _candidate_dir.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_candidate_dir))
        break

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ── Configuration ────────────────────────────────────────────────────────────
TARGET_URL      = "http://www.bengalremedies.com:9000"
LOGIN_EMAIL     = "debnathch@gmail.com"
LOGIN_PASSWORD  = "Br@2023"
OUTPUT_DIR      = Path("/Users/debz/Documents/BR/Project/Ben_Rem/excel_marg/latest")
HEADLESS        = False          # Set True to run silently without visible browser
PAGE_LOAD_WAIT  = 3000           # ms to wait after clicks / navigation
STATUS_FILTER   = "initiated"    # The status to filter by (case-insensitive match)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DOWNLOAD-AGENT] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("download_orders_agent")

# ── Progress callback (used when run from UI) ─────────────────────────────────
_progress_log: list[str] = []

def _emit(msg: str):
    """Emit progress message to both logger and shared list."""
    log.info(msg)
    _progress_log.append(msg)


async def _wait_and_screenshot(page, label: str, wait_ms: int = PAGE_LOAD_WAIT):
    """Wait for network to settle and optionally log page state."""
    await page.wait_for_timeout(wait_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        _emit(f"⚠️  Network still active after wait at: {label}. Continuing anyway.")


async def _click_by_texts(page, *text_candidates: str, timeout: int = 8000) -> bool:
    """
    Try clicking elements by multiple text candidates (first match wins).
    Returns True if a click succeeded.
    """
    for text in text_candidates:
        try:
            # Try exact text first
            loc = page.get_by_text(text, exact=True).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            try:
                # Try partial/case-insensitive text match via XPath
                loc = page.locator(f"xpath=//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{text.lower()}')]").first
                await loc.wait_for(state="visible", timeout=timeout)
                await loc.click()
                return True
            except Exception:
                continue
    return False


async def _extract_table_data(page) -> list[dict]:
    """
    Extract all rows from whatever table/list is on the current page.
    Tries multiple strategies: HTML <table>, list cards, or JSON from network.
    """
    rows = []

    # Strategy 1: Standard HTML table
    try:
        table_loc = page.locator("table").first
        if await table_loc.is_visible(timeout=3000):
            headers = []
            header_cells = await page.locator("table thead th").all()
            if not header_cells:
                header_cells = await page.locator("table tr:first-child th, table tr:first-child td").all()
            for h in header_cells:
                headers.append((await h.inner_text()).strip())

            data_rows = await page.locator("table tbody tr").all()
            if not data_rows:
                # Some tables have no tbody
                data_rows = await page.locator("table tr:not(:first-child)").all()

            for row_el in data_rows:
                cells = await row_el.locator("td").all()
                cell_texts = [(await c.inner_text()).strip() for c in cells]
                if not any(cell_texts):
                    continue
                if headers and len(headers) == len(cell_texts):
                    rows.append(dict(zip(headers, cell_texts)))
                else:
                    rows.append({f"col_{i+1}": v for i, v in enumerate(cell_texts)})

            if rows:
                _emit(f"   📋 Table strategy: extracted {len(rows)} rows")
                return rows
    except Exception as e:
        _emit(f"   ⚠️ Table strategy failed: {e}")

    # Strategy 2: Attempt to grab the page's visible text as a fallback
    # (useful if data is rendered in cards/divs instead of a table)
    try:
        _emit("   🔄 Attempting card/div row strategy...")
        # Look for repeated similar divs — common in React / Vue portals
        # Grab all text from the main content area
        content = await page.locator("main, #main, .content, [class*='content'], [class*='table'], [class*='list']").first.inner_text(timeout=5000)
        if content.strip():
            lines = [l.strip() for l in content.split("\n") if l.strip()]
            rows = [{"row_data": line} for line in lines]
            _emit(f"   📋 Card/div strategy: extracted {len(rows)} rows (raw text)")
            return rows
    except Exception as e:
        _emit(f"   ⚠️ Card/div strategy failed: {e}")

    return rows


async def _get_next_page_button(page):
    """
    Find the 'Next Page' button/link. Returns the locator or None.
    Tries multiple selectors used by common frameworks.
    """
    next_candidates = [
        "button[aria-label*='next' i]",
        "a[aria-label*='next' i]",
        "[class*='next']:not([disabled])",
        "button:has-text('Next')",
        "button:has-text('>')",
        "a:has-text('Next')",
        "[class*='pagination'] >> text=Next",
        "li.next > a",
        "button[class*='next']",
        ".pagination button:last-child",
    ]
    for sel in next_candidates:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                # Make sure it's not disabled
                disabled = await loc.get_attribute("disabled")
                cls = await loc.get_attribute("class") or ""
                if disabled is None and "disabled" not in cls.lower():
                    return loc
        except Exception:
            continue
    return None


async def run_download_agent(
    output_dir: str | Path = OUTPUT_DIR,
    headless: bool = HEADLESS,
    status_filter: str = STATUS_FILTER,
) -> dict:
    """
    Main coroutine for the download agent.
    Returns a dict with keys: success, file_path, rows_downloaded, message, log.
    """
    global _progress_log
    _progress_log = []
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    _emit("🚀 Starting Download Orders Agent...")
    _emit(f"   Target: {TARGET_URL}")
    _emit(f"   Status Filter: {status_filter.upper()}")
    _emit(f"   Output Folder: {output_dir}")

    all_rows: list[dict] = []

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
            # ── Step 1: Open the website ────────────────────────────────────
            _emit("🌐 Opening website...")
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await _wait_and_screenshot(page, "initial_load", 2000)

            # ── Step 2: Login ────────────────────────────────────────────────
            _emit("🔐 Logging in...")

            # Fill email — try various common selectors
            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                'input[placeholder*="email" i]',
                'input[placeholder*="username" i]',
                'input[name="username"]',
                '#email', '#username',
            ]
            email_filled = False
            for sel in email_selectors:
                try:
                    await page.locator(sel).first.fill(LOGIN_EMAIL, timeout=4000)
                    email_filled = True
                    _emit(f"   ✅ Email field filled ({sel})")
                    break
                except Exception:
                    continue

            if not email_filled:
                raise RuntimeError("Could not find email/username input field on login page.")

            # Fill password
            password_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                'input[placeholder*="password" i]',
                '#password',
            ]
            password_filled = False
            for sel in password_selectors:
                try:
                    await page.locator(sel).first.fill(LOGIN_PASSWORD, timeout=4000)
                    password_filled = True
                    _emit(f"   ✅ Password field filled ({sel})")
                    break
                except Exception:
                    continue

            if not password_filled:
                raise RuntimeError("Could not find password input field on login page.")

            # Click login / submit button
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Login")',
                'button:has-text("Sign In")',
                'button:has-text("Log In")',
                'button:has-text("Submit")',
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    await page.locator(sel).first.click(timeout=4000)
                    submitted = True
                    _emit(f"   ✅ Login button clicked ({sel})")
                    break
                except Exception:
                    continue

            if not submitted:
                # Try pressing Enter on password field as fallback
                for sel in password_selectors:
                    try:
                        await page.locator(sel).first.press("Enter", timeout=4000)
                        submitted = True
                        _emit("   ✅ Login submitted via Enter key")
                        break
                    except Exception:
                        continue

            await _wait_and_screenshot(page, "after_login", 3000)
            _emit(f"   📍 URL after login: {page.url}")

            # ── Step 3: Navigate to Procurement ─────────────────────────────
            _emit("📂 Navigating to Procurement...")
            procurement_clicked = await _click_by_texts(
                page, "Procurement", "procurement", "PROCUREMENT"
            )
            if not procurement_clicked:
                # Try sidebar navigation links
                try:
                    await page.locator("nav a, .sidebar a, [class*='sidebar'] a, [class*='nav'] a").filter(has_text="Procurement").first.click(timeout=6000)
                    procurement_clicked = True
                    _emit("   ✅ Procurement clicked via nav link")
                except Exception:
                    raise RuntimeError("Could not find 'Procurement' in the left navigation panel.")

            await _wait_and_screenshot(page, "after_procurement_click", 2000)

            # ── Step 4: Navigate to Order ────────────────────────────────────
            _emit("📋 Clicking 'Order'...")
            order_clicked = await _click_by_texts(page, "Order", "Orders", "ORDERS")
            if not order_clicked:
                try:
                    await page.locator("[class*='sidebar'] a, [class*='menu'] a, nav a").filter(has_text="Order").first.click(timeout=6000)
                    order_clicked = True
                    _emit("   ✅ Order clicked via nav link")
                except Exception:
                    raise RuntimeError("Could not find 'Order' option under Procurement.")

            await _wait_and_screenshot(page, "after_order_click", 2500)

            # ── Step 5: Apply Filter by Status = "Initiated" ─────────────────
            _emit(f"🔍 Applying filter: Status = '{status_filter.capitalize()}'...")

            # Click the "Filter by Status" control
            filter_clicked = await _click_by_texts(
                page,
                "Filter by Status", "filter by status", "Status", "Filter"
            )
            if not filter_clicked:
                # Try dropdown / select elements
                try:
                    selects = await page.locator("select").all()
                    for select in selects:
                        label_el = await select.evaluate(
                            "el => el.closest('label') ? el.closest('label').innerText : ''"
                        )
                        if "status" in str(label_el).lower() or True:
                            await select.select_option(label=status_filter.capitalize(), timeout=3000)
                            filter_clicked = True
                            _emit("   ✅ Status selected via <select> dropdown")
                            break
                except Exception:
                    pass

            await page.wait_for_timeout(1500)

            # Select "Initiated" option
            initiated_selected = await _click_by_texts(
                page,
                "Initiated", "INITIATED", "initiated"
            )
            if not initiated_selected:
                # Try to select from a dropdown option
                try:
                    await page.locator(
                        "option, [role='option'], li[class*='option'], [class*='dropdown-item']"
                    ).filter(has_text=status_filter).first.click(timeout=4000)
                    initiated_selected = True
                    _emit("   ✅ 'Initiated' option selected from dropdown list")
                except Exception:
                    _emit("   ⚠️  Could not explicitly click 'Initiated'. The filter may already be active or auto-applied.")

            await _wait_and_screenshot(page, "after_filter", 3000)
            _emit(f"   📍 URL after filter: {page.url}")

            # ── Step 6: Scrape ALL pages ──────────────────────────────────────
            _emit("📥 Starting data extraction (all pages)...")
            current_page_num = 1

            while True:
                _emit(f"   📄 Scraping page {current_page_num}...")
                page_rows = await _extract_table_data(page)

                if page_rows:
                    all_rows.extend(page_rows)
                    _emit(f"   ✅ Page {current_page_num}: {len(page_rows)} rows (total so far: {len(all_rows)})")
                else:
                    _emit(f"   ⚠️  Page {current_page_num}: No rows found. Stopping pagination.")
                    break

                # Check for next page
                next_btn = await _get_next_page_button(page)
                if next_btn is None:
                    _emit("   🏁 No more pages found. Extraction complete.")
                    break

                _emit("   ➡️  Moving to next page...")
                await next_btn.click()
                await _wait_and_screenshot(page, f"page_{current_page_num + 1}", 2500)
                current_page_num += 1

        except Exception as e:
            _emit(f"❌ Agent error: {e}")
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

    # ── Step 7: Save to Excel ────────────────────────────────────────────────
    if not all_rows:
        _emit("⚠️  No data was extracted. Excel file not created.")
        return {
            "success": False,
            "file_path": None,
            "rows_downloaded": 0,
            "message": "No data rows were found after applying the 'Initiated' filter. The page may have no records or the site layout requires manual selector tuning.",
            "log": _progress_log,
        }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"initiated_orders_{timestamp}.xlsx"
    out_path = output_dir / filename

    _emit(f"💾 Saving {len(all_rows)} rows → {out_path}")
    df = pd.DataFrame(all_rows)

    # Clean up column names
    df.columns = [str(c).strip() for c in df.columns]

    with pd.ExcelWriter(str(out_path), engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Initiated Orders", index=False)

        # Auto-size columns
        ws = writer.sheets["Initiated Orders"]
        for col_cells in ws.columns:
            max_len = max(
                (len(str(cell.value)) for cell in col_cells if cell.value), default=10
            )
            ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 60)

    _emit(f"✅ Excel saved: {out_path}")
    _emit(f"🎉 Download complete! Total rows: {len(all_rows)}")

    return {
        "success": True,
        "file_path": str(out_path),
        "rows_downloaded": len(all_rows),
        "message": f"Successfully downloaded {len(all_rows)} rows from {current_page_num} page(s).",
        "log": _progress_log,
    }


# ── Standalone entry point ────────────────────────────────────────────────────
if __name__ == "__main__":
    result = asyncio.run(run_download_agent())
    print("\n" + "=" * 60)
    print(json.dumps({k: v for k, v in result.items() if k != "log"}, indent=2))
    sys.exit(0 if result["success"] else 1)
