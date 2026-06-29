"""
scrapers/zimlii.py — ZimLII judgment scraper.

ZimLII (https://zimlii.org) is Cloudflare-protected. We use Firecrawl
stealth proxy (5 credits/request) to bypass it.

CORRECT URL STRUCTURE (verified 2026-06-28):
  Home:      https://zimlii.org/                         → 200 OK (Cloudflare JS challenge)
  Judgments: https://zimlii.org/judgments/               → 403 (Cloudflare bot challenge, needs stealth)
  Legislation: https://zimlii.org/legislation/           → 200 OK
  Individual judgment: https://zimlii.org/akn/zw/judgment/{court}/{year}/{number}/eng@{date}
  Individual legislation: https://zimlii.org/akn/zw/act/{year}/{number}/eng@{date}

  OLD (broken) URL patterns — DO NOT USE:
    https://zimlii.org/zw/judgment/...  → 404
    https://zimlii.org/judgments/       → 403 without stealth

Strategy:
1. Scrape https://zimlii.org/judgments/ via Firecrawl stealth to get recent judgment links
2. Parse AKN judgment URLs from the markdown: /akn/zw/judgment/{court}/{year}/{number}/...
3. For each new URL, scrape the individual judgment page (stealth)
4. Extract metadata: case_name, citation, court, judge, date, PDF URL
5. Download the PDF (direct HTTP, no Firecrawl credit needed)
6. Return JudgmentItem objects for pushing to MutemoOS /api/zlr/upload

Credit budget: ~5 credits/listing + 5 credits/judgment × up to 10 new/day = ~55 credits/day max
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, download_pdf, FirecrawlError
import state

logger = logging.getLogger(__name__)

LISTING_URL = "https://zimlii.org/judgments/"
BASE_URL    = "https://zimlii.org"

# AKN judgment URL pattern — the correct ZimLII URL structure
# e.g. /akn/zw/judgment/zwhhc/2026/179/eng@2026-11-20
RE_AKN_JUDGMENT = re.compile(
    r"/akn/zw/judgment/[a-z]+/\d{4}/\d+/eng@\d{4}-\d{2}-\d{2}"
)

# Metadata extraction patterns for judgment page markdown
RE_CASE_NAME  = re.compile(r"^#\s+(.+)$", re.MULTILINE)
RE_CITATION   = re.compile(
    r"\b(ZWSC|ZWCC|ZWHHC|ZWBHC|ZWHHC|ZWMHC|ZWCHC|ZWLC|ZWAC|SC|HH|HC|HB|HG|HMA|HMT)\s*\d+[-/]\d{2,4}\b",
    re.IGNORECASE,
)
RE_COURT      = re.compile(
    r"(Supreme Court|Constitutional Court|High Court|Labour Court|Administrative Court|"
    r"Magistrate|Harare High Court|Bulawayo High Court|Chinhoyi High Court|"
    r"Masvingo High Court|Mutare High Court)",
    re.IGNORECASE,
)
RE_JUDGE      = re.compile(
    r"(?:JUDGE[S]?|J\b|JA\b|JP\b|DCJ\b|CJ\b)[:\s]+([A-Z][A-Z\s,]+?)(?:\n|$)",
    re.MULTILINE,
)
RE_DATE       = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_PDF_LINK   = re.compile(
    r"\[.*?(?:PDF|Download|Full judgment).*?\]\((https?://[^\)]+\.pdf[^\)]*)\)",
    re.IGNORECASE,
)
# ZimLII PDF URL pattern: /akn/zw/judgment/{court}/{year}/{number}/eng@{date}/source.pdf
RE_PDF_PATH   = re.compile(r"/akn/zw/judgment/[^\"'\s]+\.pdf")


@dataclass
class JudgmentItem:
    url: str
    case_name: str
    citation: Optional[str]
    court: Optional[str]
    judge: Optional[str]
    judgment_date: Optional[str]
    pdf_url: Optional[str]
    pdf_path: Optional[Path]
    source: str = "ZimLII"
    markdown_summary: str = ""


async def _get_listing_urls(dry_run: bool = False) -> list[str]:
    """
    Scrape the ZimLII judgment listing and return all judgment AKN URLs found.
    Uses Firecrawl stealth proxy to bypass Cloudflare.
    """
    logger.info("[zimlii] Scraping judgment listing via Firecrawl stealth...")
    try:
        md = await scrape_markdown(LISTING_URL, proxy="stealth", wait_ms=3000)
    except FirecrawlError as e:
        logger.error(f"[zimlii] Failed to scrape listing: {e}")
        return []

    if not md:
        logger.error("[zimlii] Empty markdown returned from listing page")
        return []

    # Extract AKN judgment paths from markdown links
    akn_paths = RE_AKN_JUDGMENT.findall(md)

    # Also catch relative links without the date suffix
    # e.g. /akn/zw/judgment/zwhhc/2026/179 (without /eng@...)
    relative_paths = re.findall(r"/akn/zw/judgment/[a-z]+/\d{4}/\d+", md)

    all_urls = []
    seen = set()

    for path in akn_paths:
        url = BASE_URL + path
        if url not in seen:
            seen.add(url)
            all_urls.append(url)

    for path in relative_paths:
        url = BASE_URL + path
        if url not in seen:
            seen.add(url)
            all_urls.append(url)

    logger.info(f"[zimlii] Found {len(all_urls)} judgment URLs in listing")
    return all_urls


async def _scrape_judgment(url: str, dry_run: bool = False) -> Optional[JudgmentItem]:
    """Scrape a single ZimLII judgment page and return a JudgmentItem."""
    logger.info(f"[zimlii] Scraping judgment: {url}")
    try:
        md = await scrape_markdown(url, proxy="stealth", wait_ms=2000)
    except FirecrawlError as e:
        logger.error(f"[zimlii] Failed to scrape {url}: {e}")
        return None

    if not md or len(md) < 100:
        logger.warning(f"[zimlii] Empty or very short markdown for {url}")
        return None

    # Extract metadata
    case_name_m = RE_CASE_NAME.search(md)
    case_name = case_name_m.group(1).strip() if case_name_m else "Unknown v Unknown"
    # Clean up common artifacts like " | ZimLII" in the title
    case_name = re.sub(r"\s*\|\s*ZimLII.*$", "", case_name).strip()

    citation_m = RE_CITATION.search(md)
    citation = citation_m.group(0).strip() if citation_m else None

    court_m = RE_COURT.search(md)
    court = court_m.group(0).strip() if court_m else "High Court of Zimbabwe"

    judge_m = RE_JUDGE.search(md)
    judge = judge_m.group(1).strip().rstrip(",") if judge_m else None

    date_m = RE_DATE.search(md)
    judgment_date = date_m.group(1).strip() if date_m else None

    # Try to find PDF URL — first from explicit markdown links
    pdf_m = RE_PDF_LINK.search(md)
    pdf_url = pdf_m.group(1).strip() if pdf_m else None

    # If not found, look for AKN PDF paths in the markdown
    if not pdf_url:
        pdf_path_m = RE_PDF_PATH.search(md)
        if pdf_path_m:
            pdf_url = BASE_URL + pdf_path_m.group(0)

    # Last resort: construct the standard ZimLII source PDF URL from the AKN path
    if not pdf_url:
        # Strip any query string from the URL and append /source.pdf
        base_akn = url.split("?")[0].rstrip("/")
        if "/eng@" in base_akn:
            pdf_url = base_akn + "/source.pdf"
        else:
            # Try appending the PDF suffix directly
            pdf_url = base_akn + ".pdf"
        logger.debug(f"[zimlii] Constructed PDF URL: {pdf_url}")

    # Take first 800 chars of markdown as summary
    summary = md[:800].strip()

    item = JudgmentItem(
        url=url,
        case_name=case_name,
        citation=citation,
        court=court,
        judge=judge,
        judgment_date=judgment_date,
        pdf_url=pdf_url,
        pdf_path=None,
        markdown_summary=summary,
    )

    # Download PDF (skip in dry-run)
    if not dry_run and pdf_url:
        try:
            item.pdf_path = await download_pdf(pdf_url, use_stealth=True)
        except Exception as e:
            logger.warning(f"[zimlii] PDF download failed for {url}: {e}")
            item.pdf_path = None

    return item


async def run(dry_run: bool = False) -> list[JudgmentItem]:
    """
    Main entry point. Returns a list of new JudgmentItems not previously seen.
    Respects MAX_NEW_PER_RUN from state to cap Firecrawl credit usage.
    """
    logger.info(f"[zimlii] Starting scrape (dry_run={dry_run})")
    urls = await _get_listing_urls(dry_run=dry_run)

    new_items: list[JudgmentItem] = []
    max_new = state.get_max_new_per_run()

    for url in urls:
        if len(new_items) >= max_new:
            logger.info(f"[zimlii] Reached MAX_NEW_PER_RUN={max_new}, stopping")
            break

        if state.is_seen(url):
            state.increment_skipped()
            logger.debug(f"[zimlii] Already seen: {url}")
            continue

        item = await _scrape_judgment(url, dry_run=dry_run)
        if item is None:
            continue

        if dry_run:
            logger.info(
                f"[DRY RUN] Would push: {item.case_name} ({item.citation}) — {url}"
            )
        else:
            state.mark_seen(url)

        new_items.append(item)

    state.set_last_scraped("zimlii")
    logger.info(f"[zimlii] Done. {len(new_items)} new items.")
    return new_items
