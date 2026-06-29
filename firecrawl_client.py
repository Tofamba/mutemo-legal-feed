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


async def download_pdf(url: str, dest_dir: Optional[Path] = None, use_stealth: bool = False) -> Path:
    """
    Download a PDF from a URL and save it to a temp file.

    For Cloudflare-protected URLs (e.g. ZimLII source.pdf), set use_stealth=True.
    This routes the download through Firecrawl's stealth proxy (5 credits) instead
    of a direct httpx GET, bypassing the 403 that Cloudflare returns to bots.

    For unprotected URLs (e.g. LRF WordPress), use_stealth=False (default, 0 credits).

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

    if use_stealth:
        # Route through Firecrawl stealth proxy to bypass Cloudflare.
        # Costs 5 credits per PDF. Firecrawl scrapes the PDF page and returns
        # the raw PDF bytes via its /scrape endpoint with formats=["rawHtml"].
        # We then follow the canonical PDF URL returned in metadata.
        logger.info(f"[pdf] Using Firecrawl stealth proxy for: {url}")

        if not FIRECRAWL_API_KEY:
            raise FirecrawlError("FIRECRAWL_API_KEY not set — cannot use stealth PDF download.")

        payload = {
            "url": url,
            "formats": ["rawHtml"],
            "proxy": "stealth",
            "parsePDF": False,
        }
        headers_fc = {
            "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT) as client:
            resp = await client.post(
                f"{FIRECRAWL_BASE}/scrape",
                json=payload,
                headers=headers_fc,
            )

        if resp.status_code == 402:
            raise FirecrawlError("Firecrawl credit limit reached (402). Check your plan.")
        if resp.status_code == 429:
            raise FirecrawlError("Firecrawl rate limit hit (429). Slow down requests.")
        if resp.status_code != 200:
            raise FirecrawlError(
                f"Firecrawl stealth PDF error {resp.status_code}: {resp.text[:300]}"
            )

        data = resp.json()
        if not data.get("success"):
            raise FirecrawlError(f"Firecrawl stealth PDF returned success=false: {data}")

        # Firecrawl returns the raw HTML/bytes of the PDF page.
        # The actual PDF binary is in data["data"]["rawHtml"] as bytes or
        # accessible via the sourceURL in metadata. Try to get the direct
        # download URL from metadata first, then fall back to rawHtml content.
        fc_data = data.get("data", {})
        source_url = fc_data.get("metadata", {}).get("sourceURL") or url

        # Now do a direct download using the resolved URL with a Firecrawl-style
        # browser User-Agent — the stealth scrape will have set cookies that
        # allow a subsequent direct download to succeed.
        raw_html = fc_data.get("rawHtml", b"")
        if isinstance(raw_html, str):
            raw_html = raw_html.encode("utf-8", errors="replace")

        if raw_html and len(raw_html) > 1024:
            # Firecrawl returned the PDF content directly
            with open(dest_path, "wb") as f:
                f.write(raw_html)
            size_kb = dest_path.stat().st_size // 1024
            logger.info(f"[pdf] Stealth download via rawHtml {size_kb}KB → {dest_path.name}")
            return dest_path
        else:
            # Fall back: try direct download with browser headers after stealth scrape
            logger.info(f"[pdf] rawHtml empty, falling back to direct download: {source_url}")
            # Fall through to direct download below with source_url
            url = source_url
            use_stealth = False  # prevent infinite recursion

    # Direct download path (no Cloudflare protection, or fallback)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
        "Referer": "https://zimlii.org/",
    }

    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code == 403:
                raise FirecrawlError(
                    f"PDF download blocked by Cloudflare (403): {url}. "
                    f"Set use_stealth=True to route through Firecrawl."
                )
            if resp.status_code != 200:
                raise FirecrawlError(
                    f"PDF download failed {resp.status_code}: {url}"
                )
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type and "octet-stream" not in content_type:
                logger.warning(
                    f"[pdf] Unexpected content-type '{content_type}' for {url}"
                )
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    size_kb = dest_path.stat().st_size // 1024
    logger.info(f"[pdf] Downloaded {size_kb}KB → {dest_path.name}")
    return dest_path
