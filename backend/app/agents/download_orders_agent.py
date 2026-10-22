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
import tempfile
from datetime import datetime
from pathlib import Path

# Ensure Playwright finds browser cache regardless of any HOME environment override
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    for _candidate_dir in [
        Path("/Users/debz/Library/Caches/ms-playwright"),
        Path.home() / "Library/Caches/ms-playwright",
        Path.home() / ".cache/ms-playwright",
        Path("/root/.cache/ms-playwright"),
        Path(os.path.expanduser("~")) / "Library/Caches/ms-playwright",
        Path(os.path.expanduser("~")) / ".cache/ms-playwright",
    ]:
        if _candidate_dir.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_candidate_dir)
            break

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ── Cloud Environment & Configuration Defaults ───────────────────────────────
def is_cloud_environment() -> bool:
    """Detects whether running on Render, in Docker, or on a headless Linux environment."""
    return bool(
        os.environ.get("RENDER")
        or os.environ.get("DOCKER_RUNTIME")
        or (sys.platform.startswith("linux") and not os.environ.get("DISPLAY"))
    )

def get_default_output_dir() -> Path:
    """Returns local user output directory if valid locally, or a temporary directory."""
    local_default = Path("/Users/debz/Documents/BR/Project/Ben_Rem/excel_marg/latest")
    if not is_cloud_environment() and local_default.parent.exists():
        return local_default
    tmp = Path(tempfile.gettempdir()) / "procurement_orders"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp

TARGET_URL      = os.environ.get("PORTAL_URL", "http://www.bengalremedies.com:9000").rstrip("/")
LOGIN_EMAIL     = "debnathch@gmail.com"
LOGIN_PASSWORD  = "Br@2023"
OUTPUT_DIR      = get_default_output_dir()
HEADLESS        = is_cloud_environment()

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
        if len(td_texts) == 1 and any(keyword in td_texts[0].lower() for keyword in ("no data", "no records", "loading")):
            continue

        row_dict = {}
        for idx, col_name in enumerate(data_headers):
            row_dict[col_name] = td_texts[idx] if idx < len(td_texts) else ""
        rows.append(row_dict)

    return data_headers, rows


def ensure_playwright_browser_installed() -> bool:
    """
    Checks if Playwright Chromium browser is installed.
    If missing, runs `python -m playwright install chromium` via subprocess.
    """
    import subprocess
    _emit("📦 Installing Playwright Chromium browser binary (auto-install)...")
    try:
        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        _emit(f"   Executing: {' '.join(cmd)}")
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if res.returncode == 0:
            _emit("   ✅ Playwright Chromium browser installed successfully.")
            return True
        else:
            _emit(f"   ⚠️ Playwright install returned code {res.returncode}: {res.stderr or res.stdout}")
            return False
    except Exception as e:
        _emit(f"   ❌ Error running playwright install: {e}")
        return False


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

    # On cloud / headless Linux, always enforce headless
    if is_cloud_environment():
        headless = True

    try:
        output_dir = Path(output_dir) if output_dir else get_default_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        output_dir = get_default_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)

    _emit("🚀 Starting Download Orders Agent (Screen Capture Mode)...")
    _emit(f"   Target URL: {TARGET_URL}")
    _emit(f"   Target Status: '{status_filter}' (Exact Match)")
    _emit(f"   Save Directory: {output_dir}")
    _emit(f"   Browser View: {'Headless (Silent / Cloud)' if headless else 'Visible (Interactive)'}")

    all_scraped_rows: list[dict] = []
    final_headers: list[str] = []

    launch_args = ["--start-maximized"]
    if is_cloud_environment() or sys.platform.startswith("linux"):
        launch_args.extend([
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ])

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=headless,
                args=launch_args,
            )
        except Exception as launch_err:
            err_msg = str(launch_err)
            if "Executable doesn't exist" in err_msg or "playwright install" in err_msg:
                _emit("⚠️ Playwright Chromium browser binary not found in cache. Starting automatic installation...")
                installed = ensure_playwright_browser_installed()
                if not installed:
                    raise RuntimeError(f"Playwright Chromium browser binary is missing and auto-install failed: {err_msg}")
                _emit("🔄 Retrying browser launch now that Chromium is installed...")
                browser = await pw.chromium.launch(
                    headless=headless,
                    args=launch_args,
                )
            else:
                raise

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            },
        )
        page = await context.new_page()

        try:
            # ── 1. Navigate directly to Portal Login ──────────────────────────
            # Direct navigation to /login avoids redirect loops and in-flight navigation races
            clean_target = TARGET_URL.rstrip('/')
            candidate_bases = [clean_target]
            if "www." in clean_target:
                candidate_bases.append(clean_target.replace("www.", ""))
            else:
                parts = clean_target.split("://", 1)
                if len(parts) == 2:
                    candidate_bases.append(f"{parts[0]}://www.{parts[1]}")
            candidate_bases.append("http://13.127.16.211:9000")
            candidate_bases = list(dict.fromkeys(candidate_bases))

            connected_base = None

            for c_base in candidate_bases:
                login_url = f"{c_base}/login" if not c_base.endswith("/login") else c_base
                _emit(f"🌐 Connecting directly to login: {login_url} ...")
                try:
                    # Cancel any prior hanging navigation
                    try:
                        await page.evaluate("() => window.stop()")
                    except Exception:
                        pass

                    await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
                    _emit("   ⏳ Page loaded. Waiting for login form fields...")

                    email_field = page.locator('input[name="emailId"], input[type="email"], #email').first
                    await email_field.wait_for(state="visible", timeout=25000)

                    connected_base = c_base
                    _emit(f"   ✅ Login form visible at: {login_url}")
                    break
                except Exception as nav_ex:
                    _emit(f"   ⚠️ Could not reach {login_url} ({nav_ex}). Trying next candidate...")

            if not connected_base:
                err_msg = (
                    f"Unable to reach Bengal Remedies portal login on port 9000 from cloud (attempted: {', '.join(candidate_bases)}).\n"
                    f"If the server is on AWS EC2, please ensure your EC2 Security Group has an Inbound Rule for "
                    f"Custom TCP port 9000 from 0.0.0.0/0 (Anywhere IPv4)."
                )
                _emit(f"❌ {err_msg}")
                raise RuntimeError(err_msg)

            await page.wait_for_timeout(1000)

            # ── 2. Login ──────────────────────────────────────────────────────
            _emit("🔐 Entering credentials...")
            email_field = page.locator('input[name="emailId"], input[type="email"], #email').first
            await email_field.wait_for(state="visible", timeout=20000)
            await email_field.fill(LOGIN_EMAIL)
            _emit(f"   ✅ Email entered: {LOGIN_EMAIL}")

            password_field = page.locator('input[name="password"], input[type="password"], #password').first
            await password_field.fill(LOGIN_PASSWORD)
            _emit("   ✅ Password entered.")

            submit_btn = page.locator('button[type="submit"], button:has-text("Login")').first
            _emit("   🔑 Submitting login credentials and waiting for API authentication...")

            try:
                async with page.expect_response(
                    lambda r: ("login" in r.url or "auth" in r.url or "user" in r.url) and r.request.method == "POST",
                    timeout=30000,
                ) as resp_info:
                    await submit_btn.click()
                resp = await resp_info.value
                _emit(f"   ✅ Received login API authorization (HTTP {resp.status}).")
            except Exception as resp_err:
                _emit(f"   ℹ️ Login API response note ({resp_err}); verifying session...")
                try:
                    await submit_btn.click()
                except Exception:
                    pass

            # Ensure we land on authenticated dashboard or businessType view
            _emit("   ⏳ Establishing authenticated dashboard session...")
            try:
                await page.wait_for_url(
                    lambda u: any(k in u.lower() for k in ("businesstype", "pcd", "procurement", "dashboard")),
                    timeout=25000,
                )
            except Exception:
                proc_sidebar = page.get_by_text("Procurement", exact=False).first
                await proc_sidebar.wait_for(state="visible", timeout=25000)

            _emit(f"   📍 Post-login dashboard reached: {page.url}")
            await page.wait_for_timeout(1500)

            # ── 3. Navigate to Procurement Orders ─────────────────────────────
            _emit("📂 Navigating to Procurement Orders...")

            menu_nav_success = False
            try:
                proc_nav = page.get_by_text("Procurement", exact=False).first
                await proc_nav.wait_for(state="visible", timeout=15000)
                await proc_nav.click()
                _emit("   ✅ Clicked 'Procurement' menu.")
                await page.wait_for_timeout(1000)

                orders_link = page.get_by_text("Orders", exact=True).first
                await orders_link.wait_for(state="visible", timeout=15000)
                await orders_link.click()
                _emit("   ✅ Clicked 'Orders' submenu.")
                menu_nav_success = True
            except Exception as menu_err:
                _emit(f"   ⚠️ Menu navigation notice: {menu_err}. Trying direct page navigation...")

            status_input = page.locator('input[placeholder="Filter By Status"]').first
            try:
                await status_input.wait_for(state="visible", timeout=12000)
            except Exception:
                orders_url = f"{connected_base}/procurement/procurement-order"
                _emit(f"   🔄 Navigating directly to orders page URL: {orders_url} ...")
                await page.goto(orders_url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await status_input.wait_for(state="visible", timeout=25000)
                except Exception as wait_err:
                    _emit(f"   ❌ Filter input not visible on page. Current URL: {page.url}, Title: '{await page.title()}'")
                    raise RuntimeError(f"Orders page failed to load filter. URL: {page.url}. Error: {wait_err}")

            _emit(f"   📍 Orders page reached and ready: {page.url}")

            # ── 4. Filter by Status = EXACT "Initiated" ───────────────────────
            _emit(f"🔍 Applying exact filter for Status = '{status_filter}'...")
            status_input = page.locator('input[placeholder="Filter By Status"]').first
            await status_input.wait_for(state="visible", timeout=15000)
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

            await page.wait_for_timeout(2500)
            # Wait for table rows to appear after filter application
            try:
                await page.locator("table tbody tr").first.wait_for(state="visible", timeout=12000)
            except Exception:
                pass

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

    with open(target_path, "rb") as f:
        file_bytes = f.read()

    return {
        "success": True,
        "file_path": str(target_path),
        "filename": target_filename,
        "file_bytes": file_bytes,
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
