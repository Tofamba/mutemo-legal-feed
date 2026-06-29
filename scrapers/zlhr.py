"""
scrapers/zlhr.py — Zimbabwe Lawyers for Human Rights scraper.

ZLHR (https://zlhr.org.zw) publishes press statements and case updates
on human rights litigation in Zimbabwe. Simple WordPress site, no
Cloudflare protection.

URL structure: https://www.zlhr.org.zw/article-slug/
Listing page: https://www.zlhr.org.zw/ (homepage shows recent posts)

Schedule: weekdays at 06:00 UTC (08:00 CAT)
Credit budget: 1 credit/listing + 1 credit/article × up to 10 = ~11 credits/run
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, FirecrawlError
import state

logger = logging.getLogger(__name__)

LISTING_URL = "https://www.zlhr.org.zw/"
BASE_URL    = "https://www.zlhr.org.zw"

# Only match article slugs with at least 10 chars — filters out short nav pages
# e.g. /vision/, /work/, /mission/ won't match but
# /high-court-ends-students-month-long-detention/ will
RE_ARTICLE_URL = re.compile(
    r"https://www\.zlhr\.org\.zw/[a-z0-9][a-z0-9\-]{10,}/?"
)

RE_DATE  = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_TITLE    = re.compile(r"^#\s+(.+)$", re.MULTILINE)
RE_CITATION = re.compile(
    r"\b(ZWSC|ZWCC|ZWHHC|ZWBHC|ZWLC|SC|HH|HC|HB)\s*\d+[-/]\d{2,4}\b",
    re.IGNORECASE,
)
RE_CASE_NAME = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+v\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b"
)

EXCLUDE_PATTERNS = [
    "/wp-content/", "/wp-admin/", "/wp-json/", "/feed/",
    "#", "mailto:", "tel:",
    "/vision/", "/mission/", "/work/", "/access-to-justice/",
    "/publications/", "/resources/", "/news/", "/events/",
    "/programmes/", "/projects/", "/support-us/", "/get-involved/",
    "/manicaland/", "/matabeleland/", "/masvingo/", "/midlands/",
    "/mashonaland/", "/bulawayo/", "/harare/", "/membership/",
    "/board/", "/staff/", "/category/", "/tag/", "/page/",
    "/about/", "/contact/", "/donate/", "/privacy/", "/terms/",
    "/strategic-litigation/", "/constitutional-litigation/",
    "/anti-impunity/", "/mobile-legal-clinics/",
    "/human-rights-defenders/", "/public-education/",
]


def _is_excluded(url: str) -> bool:
    return any(pat in url for pat in EXCLUDE_PATTERNS)


@dataclass
class ZLHRItem:
    url: str
    title: str
    case_name: Optional[str]
    citation: Optional[str]
    doc_date: Optional[str]
    pdf_url: Optional[str] = None
    pdf_path: Optional[Path] = None
    source: str = "ZLHR"
    source_type: str = "news"
    markdown_summary: str = ""


async def _get_listing_urls() -> list[str]:
    logger.info(f"[zlhr] Scraping listing: {LISTING_URL}")
    try:
        md = await scrape_markdown(LISTING_URL, proxy="basic", wait_ms=1000)
    except FirecrawlError as e:
        logger.error(f"[zlhr] Failed to scrape listing: {e}")
        return []

    if not md:
        logger.warning("[zlhr] Empty markdown from listing")
        return []

    found = RE_ARTICLE_URL.findall(md)
    seen: set[str] = set()
    unique: list[str] = []
    for url in found:
        if url not in seen and not _is_excluded(url):
            seen.add(url)
            unique.append(url)

    logger.info(f"[zlhr] Found {len(unique)} article URLs")
    return unique


async def _scrape_article(url: str) -> Optional[ZLHRItem]:
    logger.info(f"[zlhr] Scraping: {url}")
    try:
        md = await scrape_markdown(url, proxy="basic", wait_ms=500)
    except FirecrawlError as e:
        logger.error(f"[zlhr] Failed to scrape {url}: {e}")
        return None

    if not md or len(md) < 100:
        return None

    if any(p in md.lower() for p in ["page not found", "404"]):
        return None

    title_m = RE_TITLE.search(md)
    title = title_m.group(1).strip() if title_m else url.rstrip("/").split("/")[-1].replace("-", " ").title()
    # Clean ZimLII-style suffix
    title = re.sub(r"\s*\|\s*ZLHR.*$", "", title).strip()

    citation_m = RE_CITATION.search(md)
    citation = citation_m.group(0).strip() if citation_m else None

    case_name_m = RE_CASE_NAME.search(md)
    case_name = case_name_m.group(0).strip() if case_name_m else None

    date_m = RE_DATE.search(md)
    doc_date = date_m.group(1).strip() if date_m else None

    return ZLHRItem(
        url=url,
        title=title,
        case_name=case_name,
        citation=citation,
        doc_date=doc_date,
        markdown_summary=md[:800].strip(),
    )


async def run(dry_run: bool = False) -> list[ZLHRItem]:
    logger.info(f"[zlhr] Starting scrape (dry_run={dry_run})")
    urls = await _get_listing_urls()

    new_items: list[ZLHRItem] = []
    max_new = state.get_max_new_per_run()

    for url in urls:
        if len(new_items) >= max_new:
            logger.info(f"[zlhr] Reached MAX_NEW_PER_RUN={max_new}, stopping")
            break

        if state.is_seen(url):
            state.increment_skipped()
            continue

        item = await _scrape_article(url)
        if item is None:
            continue

        if dry_run:
            logger.info(f"[DRY RUN] Would push: {item.title} — {url}")
        else:
            state.mark_seen(url)

        new_items.append(item)

    state.set_last_scraped("zlhr")
    logger.info(f"[zlhr] Done. {len(new_items)} new items.")
    return new_items
