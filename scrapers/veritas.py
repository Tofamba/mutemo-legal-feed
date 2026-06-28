"""
scrapers/veritas.py — Zimbabwe legislation/SI scraper.

CORRECT URL STRUCTURE (verified 2026-06-28):
  Veritas (veritaszim.net) returns HTTP 403 Forbidden on ALL paths from
  non-browser clients, including /legislation, /statutory-instruments, and
  /node/{id}. The Apache server blocks the request before Firecrawl stealth
  can help — this is a server-side IP/UA block, not a JS challenge.

  ZimLII (zimlii.org/legislation/) returns HTTP 200 and contains AKN links
  to all Zimbabwe Acts and SIs in the format:
    /akn/zw/act/{year}/{number}/eng@{date}
    /akn/zw/act/si/{year}/{number}/eng@{date}

  Strategy: Use ZimLII /legislation/ as the primary source for Acts and SIs.
  ZimLII is the authoritative mirror of Zimbabwe legislation and is the same
  data Veritas publishes. Firecrawl stealth is used for the individual
  legislation pages (which are Cloudflare-protected).

  Fallback: If ZimLII legislation listing is also blocked, the scraper logs
  a warning and returns an empty list rather than crashing.

Credit budget: 1 credit for listing (no stealth needed) + 5 credits/item × up to 10 = ~51 credits/run
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, download_pdf, FirecrawlError
import state

logger = logging.getLogger(__name__)

# ZimLII legislation listing — returns 200 OK without stealth
LISTING_URL = "https://zimlii.org/legislation/"
BASE_URL    = "https://zimlii.org"

# AKN act/SI URL patterns
RE_AKN_ACT = re.compile(r"/akn/zw/act/(?:si/)?(?:\w+/)*\d{4}/\d+/eng@\d{4}-\d{2}-\d{2}")
RE_AKN_ACT_SHORT = re.compile(r"/akn/zw/act/(?:si/)?(?:\w+/)*\d{4}/\d+")

# Metadata extraction
RE_TITLE      = re.compile(r"^#\s+(.+)$", re.MULTILINE)
RE_SI_REF     = re.compile(r"S\.?I\.?\s*\d+\s*(?:of|/)\s*\d{4}", re.IGNORECASE)
RE_ACT_REF    = re.compile(r"(?:Chapter|Cap\.?)\s*\d+[:.]?\d*", re.IGNORECASE)
RE_DATE       = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_PDF_LINK   = re.compile(r"\[.*?\]\((https?://[^\)]+\.pdf[^\)]*)\)", re.IGNORECASE)
RE_PDF_PATH   = re.compile(r"/akn/zw/act/[^\"'\s]+\.pdf")


@dataclass
class LegislationItem:
    url: str
    title: str
    reference: Optional[str]        # e.g. "S.I. 93 of 2019" or "Chapter 28:01"
    doc_date: Optional[str]
    source_type: str                 # "legislation" or "statutory_instrument"
    pdf_url: Optional[str]
    pdf_path: Optional[Path]
    source: str = "ZimLII Legislation"
    markdown_summary: str = ""


async def _get_listing_urls() -> list[str]:
    """
    Scrape ZimLII /legislation/ and return all Act/SI AKN URLs.
    This page returns 200 OK without stealth proxy.
    """
    logger.info(f"[veritas] Scraping legislation listing: {LISTING_URL}")
    try:
        # Use basic proxy — ZimLII /legislation/ is accessible without stealth
        md = await scrape_markdown(LISTING_URL, proxy="basic", wait_ms=1000)
    except FirecrawlError as e:
        logger.error(f"[veritas] Failed to scrape legislation listing: {e}")
        return []

    if not md:
        logger.error("[veritas] Empty markdown from legislation listing")
        return []

    all_urls: list[str] = []
    seen: set[str] = set()

    # Extract full AKN paths with date suffix
    for path in RE_AKN_ACT.findall(md):
        url = BASE_URL + path
        if url not in seen:
            seen.add(url)
            all_urls.append(url)

    # Extract short AKN paths without date suffix
    for path in RE_AKN_ACT_SHORT.findall(md):
        url = BASE_URL + path
        if url not in seen:
            seen.add(url)
            all_urls.append(url)

    logger.info(f"[veritas] Found {len(all_urls)} legislation URLs")
    return all_urls


async def _scrape_document(url: str, dry_run: bool = False) -> Optional[LegislationItem]:
    """Scrape a single ZimLII legislation page and return a LegislationItem."""
    logger.info(f"[veritas] Scraping legislation: {url}")
    try:
        # Individual legislation pages are Cloudflare-protected — use stealth
        md = await scrape_markdown(url, proxy="stealth", wait_ms=2000)
    except FirecrawlError as e:
        logger.error(f"[veritas] Failed to scrape {url}: {e}")
        return None

    if not md or len(md) < 50:
        logger.warning(f"[veritas] Empty markdown for {url}")
        return None

    # Extract title
    title_m = RE_TITLE.search(md)
    title = title_m.group(1).strip() if title_m else "Unknown Legislation"
    title = re.sub(r"\s*\|\s*ZimLII.*$", "", title).strip()

    # Determine reference and source type
    si_m = RE_SI_REF.search(md)
    act_m = RE_ACT_REF.search(md)
    is_si = "/act/si/" in url or bool(si_m)

    if si_m:
        reference = si_m.group(0).strip()
        source_type = "statutory_instrument"
    elif act_m:
        reference = act_m.group(0).strip()
        source_type = "legislation"
    else:
        reference = None
        source_type = "statutory_instrument" if is_si else "legislation"

    date_m = RE_DATE.search(md)
    doc_date = date_m.group(1).strip() if date_m else None

    # Find PDF URL
    pdf_m = RE_PDF_LINK.search(md)
    pdf_url = pdf_m.group(1).strip() if pdf_m else None

    if not pdf_url:
        pdf_path_m = RE_PDF_PATH.search(md)
        if pdf_path_m:
            pdf_url = BASE_URL + pdf_path_m.group(0)

    if not pdf_url:
        base_akn = url.split("?")[0].rstrip("/")
        if "/eng@" in base_akn:
            pdf_url = base_akn + "/source.pdf"

    summary = md[:800].strip()

    item = LegislationItem(
        url=url,
        title=title,
        reference=reference,
        doc_date=doc_date,
        source_type=source_type,
        pdf_url=pdf_url,
        pdf_path=None,
        markdown_summary=summary,
    )

    if not dry_run and pdf_url:
        try:
            item.pdf_path = await download_pdf(pdf_url)
        except Exception as e:
            logger.warning(f"[veritas] PDF download failed for {url}: {e}")
            item.pdf_path = None

    return item


async def run(dry_run: bool = False) -> list[LegislationItem]:
    """Main entry point. Returns new LegislationItems not previously seen."""
    logger.info(f"[veritas] Starting scrape (dry_run={dry_run})")
    urls = await _get_listing_urls()

    new_items: list[LegislationItem] = []
    max_new = state.get_max_new_per_run()

    for url in urls:
        if len(new_items) >= max_new:
            logger.info(f"[veritas] Reached MAX_NEW_PER_RUN={max_new}, stopping")
            break

        if state.is_seen(url):
            state.increment_skipped()
            logger.debug(f"[veritas] Already seen: {url}")
            continue

        item = await _scrape_document(url, dry_run=dry_run)
        if item is None:
            continue

        if dry_run:
            logger.info(
                f"[DRY RUN] Would push: {item.title} ({item.reference}) — {url}"
            )
        else:
            state.mark_seen(url)

        new_items.append(item)

    state.set_last_scraped("veritas")
    logger.info(f"[veritas] Done. {len(new_items)} new items.")
    return new_items
