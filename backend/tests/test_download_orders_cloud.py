"""
Unit tests for the cloud-compatible Download Orders Agent and API endpoints.
Tests call the FastAPI endpoint handlers directly with asyncio.run:
1. is_cloud_environment() correctly detects RENDER, DOCKER_RUNTIME, and local macOS.
2. get_default_output_dir() returns a valid Path.
3. health() returns is_cloud flag.
4. download_initiated_orders() returns file_b64, filename, rows_downloaded, and log when agent succeeds.
5. download_initiated_orders_file() returns a Response with appropriate Excel MIME type and attachment header.
"""
import os
import asyncio
import base64
from unittest.mock import patch, AsyncMock
from pathlib import Path

from backend.app.main import (
    health,
    download_initiated_orders,
    download_initiated_orders_file,
)
from backend.app.agents.download_orders_agent import (
    is_cloud_environment,
    get_default_output_dir,
)


def test_is_cloud_environment_detection():
    # Test local non-cloud
    with patch.dict(os.environ, {"RENDER": "", "DOCKER_RUNTIME": ""}, clear=False):
        if os.sys.platform == "darwin":
            assert is_cloud_environment() is False

    # Test Render cloud flag
    with patch.dict(os.environ, {"RENDER": "true"}, clear=False):
        assert is_cloud_environment() is True

    # Test Docker runtime flag
    with patch.dict(os.environ, {"RENDER": "", "DOCKER_RUNTIME": "1"}, clear=False):
        assert is_cloud_environment() is True


def test_get_default_output_dir_cloud():
    # Under cloud environment, it must return a temp directory
    with patch.dict(os.environ, {"RENDER": "true"}, clear=False):
        out_dir = get_default_output_dir()
        assert isinstance(out_dir, Path)
        assert out_dir.exists()
        test_file = out_dir / "_write_check.tmp"
        test_file.write_text("test")
        assert test_file.read_text() == "test"
        test_file.unlink()


def test_health_endpoint_reports_cloud_flag():
    data = health()
    assert data["status"] == "ok"
    assert "is_cloud" in data
    assert isinstance(data["is_cloud"], bool)


def test_download_initiated_orders_api_mocked():
    sample_bytes = b"PK\x03\x04synthetic_excel_content"
    mock_result = {
        "success": True,
        "file_path": "/tmp/procurement_orders/initiated_orders_20261022.xlsx",
        "filename": "initiated_orders_20261022.xlsx",
        "file_bytes": sample_bytes,
        "rows_downloaded": 42,
        "message": "Successfully screen-captured 42 rows.",
        "log": ["Started", "Connected", "Scraped 42 rows", "Saved"],
    }

    with patch("backend.app.agents.download_orders_agent.run_download_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = mock_result
        data = asyncio.run(download_initiated_orders(headless=True))
        assert data["status"] == "success"
        assert data["rows_downloaded"] == 42
        assert data["filename"] == "initiated_orders_20261022.xlsx"
        assert data["file_b64"] == base64.b64encode(sample_bytes).decode("utf-8")
        assert len(data["log"]) == 4


def test_download_initiated_orders_file_streaming_mocked():
    sample_bytes = b"PK\x03\x04mock_excel_bytes_for_streaming"
    mock_result = {
        "success": True,
        "file_path": "/tmp/procurement_orders/initiated_orders_stream.xlsx",
        "filename": "initiated_orders_stream.xlsx",
        "file_bytes": sample_bytes,
        "rows_downloaded": 10,
        "message": "Success",
        "log": [],
    }

    with patch("backend.app.agents.download_orders_agent.run_download_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = mock_result
        resp = asyncio.run(download_initiated_orders_file(headless=True))
        assert resp.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert 'attachment; filename="initiated_orders_stream.xlsx"' in resp.headers["Content-Disposition"]
        assert resp.body == sample_bytes


def test_ensure_playwright_browser_installed():
    from backend.app.agents.download_orders_agent import ensure_playwright_browser_installed
    from unittest.mock import MagicMock

    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="Browser installed", stderr="")
        assert ensure_playwright_browser_installed() is True

    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="Failed")
        assert ensure_playwright_browser_installed() is False

