"""
firecrawl_client.py — Drop-in replacement using Crawl4AI instead of Firecrawl.

Preserves the exact same interface as the original Firecrawl wrapper:
    scrape_page()     → returns dict with markdown and metadata
    scrape_markdown() → returns markdown string
    download_pdf()    → downloads PDF to temp file, returns Path
    FirecrawlError    → same exception class name

No API key needed. No credit limits. Self-hosted via Crawl4AI + Playwright.

Crawl4AI install (added to requirements.txt):
    crawl4ai==0.6.3
    playwright (installed automatically by crawl4ai)

On first run, Playwright browsers must be installed:
    python -m playwright install chromium

Railway: Add a Dockerfile RUN step or startup command:
    RUN python -m playwright install chromium --with-deps

Proxy modes (mapped from Firecrawl terminology):
    "basic"   → standard Playwright headless browser (fast, no stealth)
    "stealth" → Playwright with stealth plugin (bypasses basic bot detection)
    "auto"    → same as "basic" (Crawl4AI handles JS rendering by default)
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SCRAPE_TIMEOUT   = 60    # seconds
DOWNLOAD_TIMEOUT = 120   # seconds

# Crawl4AI import — graceful fallback if not installed
try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    from crawl4ai.content_filter_strategy import PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False
    logger.warning("[crawl4ai] crawl4ai not installed — falling back to httpx-only mode")


class FirecrawlError(Exception):
    """Same exception class as original firecrawl_client.py for drop-in compatibility."""
    pass


def _get_browser_config(proxy: str) -> "BrowserConfig":
    """Map Firecrawl proxy modes to Crawl4AI BrowserConfig."""
    use_stealth = proxy == "stealth"
    return BrowserConfig(
        browser_type="chromium",
        headless=True,
        verbose=False,
        extra_args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
        # Stealth mode — randomises fingerprint to bypass bot detection
        use_managed_browser=use_stealth,
    )


def _get_run_config(only_main_content: bool, wait_ms: int) -> "CrawlerRunConfig":
    """Build Crawl4AI run config matching Firecrawl behaviour."""
    md_generator = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(
            threshold=0.4,
            threshold_type="fixed",
            min_word_threshold=5,
        ) if only_main_content else None,
    )
    return CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,  # bypasses Crawl4AI's own internal cache only —
        # does NOT affect any CDN/edge cache the target site itself sits
        # behind (e.g. Cloudflare). That's a separate layer entirely.
        # NOTE: this pinned crawl4ai version (0.6.3) does not support a
        # `headers=` kwarg on CrawlerRunConfig (added in later releases) —
        # tried it, got TypeError: unexpected keyword argument 'headers'.
        # Relying on the cache-busting query param in scrape_page() instead,
        # which works independent of crawl4ai's API surface/version since
        # it's just URL string manipulation, not a config option.
        markdown_generator=md_generator,
        wait_until="domcontentloaded",
        page_timeout=30000,  # ms
        delay_before_return_html=wait_ms / 1000 if wait_ms > 0 else 0,
        remove_overlay_elements=True,
        simulate_user=True,
    )


async def scrape_page(
    url: str,
    proxy: str = "auto",
    only_main_content: bool = True,
    wait_ms: int = 0,
) -> dict:
    """
    Scrape a URL and return a dict compatible with the Firecrawl response format:
        {
            "markdown": "...",
            "metadata": {
                "title": "...",
                "description": "...",
                "url": "...",
                "statusCode": 200,
            }
        }

    Raises FirecrawlError on failures.
    """
    if not CRAWL4AI_AVAILABLE:
        raise FirecrawlError(
            "crawl4ai is not installed. Run: pip install crawl4ai && python -m playwright install chromium"
        )

    browser_config = _get_browser_config(proxy)
    run_config     = _get_run_config(only_main_content, wait_ms)

    # Belt-and-suspenders against CDN/edge caching on the target site itself
    # (separate from Crawl4AI's own cache, and separate from whether the
    # target's CDN even honors Cache-Control/Pragma headers — some CDN page
    # rules ignore client cache-control hints entirely). Appending a unique
    # query param changes the actual cache key, which most CDNs key on by
    # full URL+query — this forces a cache miss regardless of header
    # handling. The clean, original url is preserved everywhere else
    # (metadata, dedup, storage) — only the navigation target gets busted.
    import time
    cache_bust_sep = "&" if "?" in url else "?"
    fetch_url = f"{url}{cache_bust_sep}_cb={int(time.time() * 1000)}"

    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=fetch_url, config=run_config)

        if not result.success:
            raise FirecrawlError(
                f"Crawl4AI failed for {url}: {result.error_message or 'unknown error'}"
            )

        markdown = result.markdown.fit_markdown if result.markdown else ""
        if not markdown and result.markdown:
            markdown = result.markdown.raw_markdown or ""

        # Build metadata dict matching Firecrawl's format
        metadata = {
            "title":       result.metadata.get("title", "") if result.metadata else "",
            "description": result.metadata.get("description", "") if result.metadata else "",
            "url":         url,
            "sourceURL":   url,
            "statusCode":  result.status_code or 200,
        }

        logger.info(f"[crawl4ai] ✓ Scraped {url} ({len(markdown)} chars)")
        return {"markdown": markdown, "metadata": metadata}

    except FirecrawlError:
        raise
    except asyncio.TimeoutError:
        raise FirecrawlError(f"Crawl4AI timed out scraping: {url}")
    except Exception as e:
        raise FirecrawlError(f"Crawl4AI error scraping {url}: {e}")


async def scrape_markdown(
    url: str,
    proxy: str = "auto",
    only_main_content: bool = True,
    wait_ms: int = 0,
) -> str:
    """Convenience wrapper — returns just the markdown string."""
    data = await scrape_page(url, proxy=proxy, only_main_content=only_main_content, wait_ms=wait_ms)
    return data.get("markdown", "")


async def download_pdf(
    url: str,
    dest_dir: Optional[Path] = None,
    use_stealth: bool = False,
) -> Path:
    """
    Download a PDF from a URL and save it to a temp file.
    Returns the Path to the downloaded file.
    The caller is responsible for deleting the file after use.

    For Cloudflare-protected PDFs, set use_stealth=True —
    this uses Crawl4AI's stealth browser to bypass protection.
    For unprotected PDFs, uses direct httpx download (faster).
    """
    if dest_dir is None:
        dest_dir = Path(tempfile.gettempdir())

    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = url.rstrip("/").split("/")[-1]
    if not safe_name.lower().endswith(".pdf"):
        safe_name = safe_name + ".pdf"
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in safe_name)
    dest_path = dest_dir / safe_name

    if use_stealth and CRAWL4AI_AVAILABLE:
        logger.info(f"[pdf] Using Crawl4AI stealth browser for: {url}")
        try:
            browser_config = _get_browser_config("stealth")
            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                wait_until="domcontentloaded",
                page_timeout=30000,
            )
            async with AsyncWebCrawler(config=browser_config) as crawler:
                result = await crawler.arun(url=url, config=run_config)

            if result.success and result.downloaded_files:
                # Crawl4AI may capture the PDF as a downloaded file
                for f in result.downloaded_files:
                    if f.endswith(".pdf"):
                        import shutil
                        shutil.copy(f, dest_path)
                        logger.info(f"[pdf] Stealth download via Crawl4AI → {dest_path.name}")
                        return dest_path

            # Fall back to direct download with browser-like headers
            logger.info(f"[pdf] Stealth browser didn't capture PDF, trying direct download")
        except Exception as e:
            logger.warning(f"[pdf] Stealth browser error: {e} — trying direct download")

    # Direct download via httpx with browser-like headers
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,*/*",
        "Referer": "https://zimlii.org/",
    }

    try:
        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code == 403:
                    raise FirecrawlError(
                        f"PDF download blocked (403): {url}. Set use_stealth=True."
                    )
                if resp.status_code != 200:
                    raise FirecrawlError(
                        f"PDF download failed {resp.status_code}: {url}"
                    )
                content_type = resp.headers.get("content-type", "")
                if "pdf" not in content_type and "octet-stream" not in content_type:
                    logger.warning(f"[pdf] Unexpected content-type '{content_type}' for {url}")
                with open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)

        size_kb = dest_path.stat().st_size // 1024
        logger.info(f"[pdf] Downloaded {size_kb}KB → {dest_path.name}")
        return dest_path

    except FirecrawlError:
        raise
    except Exception as e:
        raise FirecrawlError(f"PDF download error for {url}: {e}")
