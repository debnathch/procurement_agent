# Windows Run Guide: MARG Pharmaceutical Procurement Agent

This document explains how to set up and run the MARG Pharmaceutical Procurement Agent on Microsoft Windows using the automated one-click launcher [`run_windows.bat`](run_windows.bat).

---

## 1. Zero-Touch Setup (Automatic Bootstrapping)

[`run_windows.bat`](run_windows.bat) is completely automated. In most cases, **no manual installation is required**:
* If neither Python nor `uv` is detected on your system, the batch script will **automatically download and configure a lightweight, self-contained Python runtime (`uv`) via built-in Windows PowerShell** (no administrator privileges needed).
* If you already have **Python 3.10+** or **Astral `uv`** installed, the script automatically detects and utilizes your existing installation.

> **Optional Manual Python Installation:**  
> If you prefer to install Python manually:
> 1. Download the installer from: **[python.org/downloads](https://www.python.org/downloads/)**
> 2. **CRITICAL STEP:** During setup, check the box: **`[x] Add python.exe to PATH`**.

---

## 2. How to Launch the Application

### Method A: One-Click Launch (Easiest)
1. Open **Windows File Explorer** and navigate to the project directory.
2. Double-click **`run_windows.bat`**.
3. A Command Prompt window will open and perform all setup tasks automatically.

### Method B: From Command Prompt or PowerShell
1. Open **Command Prompt** (`cmd`) or **PowerShell**.
2. Change directory into the project folder:
   ```cmd
   cd C:\path\to\procurement_agent
   ```
3. Run the batch script:
   ```cmd
   run_windows.bat
   ```

---

## 3. What the Automated Launcher Does

When executed, [`run_windows.bat`](run_windows.bat) performs the following tasks without requiring manual input:

1. **Environment Config (`.env`):**
   * Checks if `.env` exists. If not, it copies `.env.example` to `.env` automatically.
2. **Environment & Runtime Detection:**
   * Checks for Astral `uv` (fastest runtime).
   * If `uv` is not present, it checks for standard `python` (verifying Python 3.10+).
   * If `python` is not in PATH, it falls back to the official Windows Python Launcher (`py -3` or `py`).
3. **Isolated Virtual Environment (`.venv`):**
   * Creates a local virtual environment in the `.venv\` directory if one does not already exist.
4. **Dependency Installation:**
   * Upgrades `pip` and installs all packages listed in [`requirements.txt`](requirements.txt) (FastAPI, Streamlit, SQLAlchemy, Pandas, openpyxl, xlrd, etc.).
5. **Unified Launcher Execution:**
   * Starts both background services in parallel:
     * **FastAPI Backend Server** on `http://127.0.0.1:8000`
     * **Streamlit Web Dashboard** on `http://localhost:8501`
6. **Automatic Browser Launch:**
   * Opens your default web browser directly to the Streamlit UI once the backend is healthy.

---

## 4. Application URLs

Once the services are active, you can access:

| Service | URL | Description |
| :--- | :--- | :--- |
| **Streamlit Dashboard** | [http://localhost:8501](http://localhost:8501) | Main user interface for inventory review and purchase order generation |
| **FastAPI REST API** | [http://localhost:8000](http://localhost:8000) | Backend REST API server |
| **Interactive API Docs** | [http://localhost:8000/docs](http://localhost:8000/docs) | Swagger UI for exploring and testing API endpoints |

---

## 5. How to Stop the Application

To shut down both the backend and frontend cleanly:
1. Bring the Command Prompt window running `run_windows.bat` to the foreground.
2. Press **`Ctrl + C`**.
3. Both services will terminate cleanly.
4. The window will display `"Application stopped."` and pause so you can inspect any logs before closing.

---

## 6. Troubleshooting Common Issues

### 1. `[!] Error: Python 3.10+ was not detected on your Windows system`
* **Cause:** Python is either not installed or was installed without adding it to the system PATH environment variable.
* **Fix:** Re-run the Python installer, choose **Modify**, check **"Add python.exe to PATH"**, and complete setup. Then reopen `run_windows.bat`.

### 2. Windows Defender / SmartScreen: `"Windows protected your PC"`
* **Cause:** Windows SmartScreen often flags newly downloaded `.bat` files.
* **Fix:** Click **"More info"** on the popup and select **"Run anyway"**.

### 3. Port Conflict (Port 8000 or 8501 already in use)
* **Cause:** A previously running instance of the application or another service is already using port 8000 or 8501.
* **Fix:** Check Task Manager for any hanging `python.exe` processes and end them, or run the following in Command Prompt to find and terminate the process:
  ```cmd
  netstat -ano | findstr :8000
  taskkill /PID <PID_NUMBER> /F
  ```

### 4. Antivirus or Firewall Popups
* If Windows Firewall asks for permission to allow Python network access, select **"Allow access"** on Private networks so the frontend can communicate with the backend on localhost.
