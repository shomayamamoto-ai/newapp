"""HTML -> PDF export.

Chromium is the primary renderer because the reports are styled with modern
CSS (grid, custom properties) that lighter engines render incorrectly.
WeasyPrint is accepted as a fallback when it is installed.
"""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path


class PdfError(RuntimeError):
    pass


def chromium_executable() -> str | None:
    """Locate a Chromium build Playwright can drive.

    Playwright pins an exact build number, so a preinstalled browser under
    PLAYWRIGHT_BROWSERS_PATH often does not match the version the Python
    package expects. Globbing for any installed build makes an existing
    browser usable instead of demanding `playwright install`.
    """
    explicit = os.environ.get("CHROMIUM_EXECUTABLE")
    if explicit and Path(explicit).exists():
        return explicit

    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    patterns = [
        f"{root}/chromium-*/chrome-linux/chrome",
        f"{root}/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        f"{root}/chromium_headless_shell-*/chrome-linux/headless_shell",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]

    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def html_to_pdf(
    html: str,
    out_path: str | Path,
    *,
    base_url: str | Path | None = None,
    print_background: bool = True,
    margin: str = "14mm",
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _weasyprint_fallback(html, out_path, base_url)

    executable = chromium_executable()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=executable,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page()
            # set_content with a file:// base lets relative <img> paths resolve.
            page.set_content(html, wait_until="networkidle")
            if base_url:
                page.evaluate(
                    "u => { const b = document.createElement('base'); b.href = u; "
                    "document.head.prepend(b); }",
                    Path(base_url).resolve().as_uri() + "/",
                )
                page.set_content(html, wait_until="networkidle")
            page.emulate_media(media="print")
            page.pdf(
                path=str(out_path),
                format="A4",
                print_background=print_background,
                margin={"top": margin, "bottom": margin, "left": margin, "right": margin},
            )
            browser.close()
        return out_path
    except Exception as exc:
        try:
            return _weasyprint_fallback(html, out_path, base_url)
        except PdfError:
            raise PdfError(
                f"Chromium PDF export failed ({exc}). Install a browser with "
                "`playwright install chromium`, set CHROMIUM_EXECUTABLE, or "
                "`pip install weasyprint`."
            ) from exc


def _weasyprint_fallback(html: str, out_path: Path, base_url=None) -> Path:
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise PdfError(
            "No PDF renderer available. Install `playwright` (recommended) "
            "or `weasyprint`."
        ) from exc
    HTML(string=html, base_url=str(base_url) if base_url else None).write_pdf(str(out_path))
    return out_path
