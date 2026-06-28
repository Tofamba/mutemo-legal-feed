"""
firecrawl_client.py — Thin async wrapper around the Firecrawl v1 scrape API.

Firecrawl API reference: https://docs.firecrawl.dev/api-reference/endpoint/scrape

Credit costs (free tier: 500/month):
  - basic proxy:   1 credit per page
  - stealth proxy: 5 credits per page (used for Cloudflare-protected sites)
  - PDF (parsePDF=false): 1 credit flat (we download the raw file ourselves)
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
FIRECRAWL_BASE    = "https://api.firecrawl.dev/v1"
SCRAPE_TIMEOUT    = 60   # seconds — Firecrawl can be slow on stealth mode
DOWNLOAD_TIMEOUT  = 120  # seconds — PDF downloads can be large


class FirecrawlError(Exception):
    pass


async def scrape_page(
    url: str,
    proxy: str = "auto",
    only_main_content: bool = True,
    wait_ms: int = 0,
) -> dict:
    """
    Scrape a URL and return the Firecrawl response dict.

    Returns a dict with at minimum:
        {
            "markdown": "...",
            "metadata": {
                "title": "...",
                "description": "...",
                "url": "...",
                "statusCode": 200,
                ...
            }
        }

    Raises FirecrawlError on API errors or non-200 responses.
    """
    if not FIRECRAWL_API_KEY:
        raise FirecrawlError("FIRECRAWL_API_KEY environment variable not set.")

    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": only_main_content,
        "proxy": proxy,
    }
    if wait_ms > 0:
        payload["waitFor"] = wait_ms

    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT) as client:
        resp = await client.post(
            f"{FIRECRAWL_BASE}/scrape",
            json=payload,
            headers=headers,
        )

    if resp.status_code == 402:
        raise FirecrawlError("Firecrawl credit limit reached (402). Check your plan.")
    if resp.status_code == 429:
        raise FirecrawlError("Firecrawl rate limit hit (429). Slow down requests.")
    if resp.status_code != 200:
        raise FirecrawlError(
            f"Firecrawl API error {resp.status_code}: {resp.text[:300]}"
        )

    data = resp.json()
    if not data.get("success"):
        raise FirecrawlError(f"Firecrawl returned success=false: {data}")

    return data.get("data", {})


async def scrape_markdown(
    url: str,
    proxy: str = "auto",
    only_main_content: bool = True,
    wait_ms: int = 0,
) -> str:
    """Convenience wrapper — returns just the markdown string."""
    data = await scrape_page(url, proxy=proxy, only_main_content=only_main_content, wait_ms=wait_ms)
    return data.get("markdown", "")


async def download_pdf(url: str, dest_dir: Optional[Path] = None) -> Path:
    """
    Download a PDF from a URL and save it to a temp file.

    Returns the Path to the downloaded file.
    The caller is responsible for deleting the file after use.
    """
    if dest_dir is None:
        dest_dir = Path(tempfile.gettempdir())

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Derive a safe filename from the URL
    safe_name = url.rstrip("/").split("/")[-1]
    if not safe_name.lower().endswith(".pdf"):
        safe_name = safe_name + ".pdf"
    # Sanitise
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in safe_name)
    dest_path = dest_dir / safe_name

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
    }

    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise FirecrawlError(
                    f"PDF download failed {resp.status_code}: {url}"
                )
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type and "octet-stream" not in content_type:
                # Some servers redirect to a login page — catch this
                logger.warning(
                    f"[pdf] Unexpected content-type '{content_type}' for {url}"
                )
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    size_kb = dest_path.stat().st_size // 1024
    logger.info(f"[pdf] Downloaded {size_kb}KB → {dest_path.name}")
    return dest_path
