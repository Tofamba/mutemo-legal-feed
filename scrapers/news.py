"""
scrapers/news.py — Zimbabwe legal news scraper (Phase 3).

Monitors 6 Zimbabwean news sources for legal, court, and regulatory stories.
Schedule: 05:30 UTC (07:30 CAT) — runs after ZimLII, Veritas, LRF.

Sources:
  - NewsDay         (newsday.co.zw)
  - The Herald      (herald.co.zw)
  - Financial Gazette (fingaz.co.zw)
  - Zimbabwe Independent (theindependent.co.zw)
  - Chronicle       (chronicle.co.zw)
  - Business Weekly (businessweekly.co.zw)

Strategy:
1. Scrape the news listing/homepage of each source via Firecrawl basic proxy
2. Extract article URLs from the markdown
3. Filter by legal keywords — only articles relevant to legal practice
4. For each new URL, scrape the full article
5. Return NewsItem objects for pushing to MutemoOS as Legal Updates
   (source_type: "news")

Credit budget: 1 credit/listing × 6 sources + 1 credit/article × up to 10 = ~16 credits/run
"""

import asyncio
import hashlib
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, FirecrawlError
import state

logger = logging.getLogger(__name__)

# News sources — listing URLs that return the most recent articles
NEWS_SOURCES = [
    {
        "name": "NewsDay",
        "listing_url": "https://www.newsday.co.zw/local-news/",
        "base_url": "https://www.newsday.co.zw",
        "article_pattern": re.compile(r"https://www\.newsday\.co\.zw/[a-z0-9\-]+/article/\d+/[a-z0-9\-]+/?"),
    },
    {
        "name": "The Herald",
        "listing_url": "https://www.herald.co.zw/",
        "base_url": "https://www.herald.co.zw",
        "article_pattern": re.compile(r"https://www\.herald\.co\.zw/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/?"),
    },
    {
        "name": "Financial Gazette",
        "listing_url": "https://www.fingaz.co.zw/",
        "base_url": "https://www.fingaz.co.zw",
        "article_pattern": re.compile(r"https://www\.fingaz\.co\.zw/[a-z0-9\-/]+/?"),
    },
    {
        "name": "Zimbabwe Independent",
        "listing_url": "https://www.theindependent.co.zw/",
        "base_url": "https://www.theindependent.co.zw",
        "article_pattern": re.compile(r"https://www\.theindependent\.co\.zw/[a-z0-9\-/]+/?"),
    },
    {
        "name": "Chronicle",
        "listing_url": "https://www.chronicle.co.zw/",
        "base_url": "https://www.chronicle.co.zw",
        "article_pattern": re.compile(r"https://www\.chronicle\.co\.zw/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/?"),
    },
    {
        "name": "Business Weekly",
        "listing_url": "https://businessweekly.co.zw/",
        "base_url": "https://businessweekly.co.zw",
        "article_pattern": re.compile(r"https://businessweekly\.co\.zw/[a-z0-9\-/]+/?"),
    },
]

# Legal keywords — article must contain at least one to be included
LEGAL_KEYWORDS = [
    "court", "judge", "judgment", "magistrate", "high court", "supreme court",
    "constitutional court", "labour court", "appeal", "sentence", "convicted",
    "acquitted", "interdict", "injunction", "lawsuit", "litigation",
    "attorney", "advocate", "lawyer", "prosecution", "accused", "defendant",
    "plaintiff", "applicant", "respondent", "verdict",
    "zimra", "revenue authority",
    "reserve bank", "financial intelligence", "aml",
    "companies act", "companies registry", "liquidation", "winding up",
    "eviction", "ejectment", "spoliation", "rei vindicatio",
    "bill of rights", "fundamental rights",
    "statutory instrument", "law society", "legal practitioners",
]

RE_DATE  = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_TITLE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)

# Exclude navigation/utility links
EXCLUDE_PATTERNS = [
    "/category/", "/tag/", "/author/", "/page/", "/wp-content/",
    "/wp-admin/", "/feed/", "#", "mailto:", "tel:", "/about",
    "/contact", "/advertise", "/subscribe", "/privacy", "/terms",
    "/sport/", "/entertainment/", "/lifestyle/", "/technology/",
]


def _is_excluded(url: str) -> bool:
    return any(pat in url for pat in EXCLUDE_PATTERNS)


def _is_legal_content(text: str) -> bool:
    """Return True if the text contains at least 2 legal keywords."""
    text_lower = text.lower()
    matches = sum(1 for kw in LEGAL_KEYWORDS if kw in text_lower)
    return matches >= 2


@dataclass
class NewsItem:
    url: str
    title: str
    source: str                  # e.g. "NewsDay", "The Herald"
    doc_date: Optional[str]
    markdown_summary: str
    pdf_url: Optional[str] = None
    pdf_path: Optional[Path] = None
    source_type: str = "news"    # for MutemoOS legal updates
    scraped_at: Optional[object] = None  # datetime set at scrape time


async def _scrape_source(source: dict, dry_run: bool = False, max_new: Optional[int] = None) -> list[NewsItem]:
    """
    Scrape a single news source and return relevant NewsItems.

    `max_new` is the remaining shared budget across all sources for this run
    — passed in by run() so a single high-volume source (NewsDay, Herald)
    can't consume the whole per-run cap on its own. Falls back to the full
    per-run cap if not given (e.g. when called standalone/manually).
    """
    name = source["name"]
    listing_url = source["listing_url"]
    article_pattern = source["article_pattern"]

    if max_new is None:
        max_new = state.get_max_new_per_run()
    if max_new <= 0:
        return []

    logger.info(f"[news/{name}] Scraping listing: {listing_url}")
    try:
        md = await scrape_markdown(listing_url, proxy="basic", wait_ms=1000)
    except FirecrawlError as e:
        logger.error(f"[news/{name}] Failed to scrape listing: {e}")
        return []

    if not md:
        logger.warning(f"[news/{name}] Empty markdown from listing")
        return []

    # Extract article URLs
    article_urls = article_pattern.findall(md)
    article_urls = [u for u in article_urls if not _is_excluded(u)]

    # Deduplicate
    seen: set[str] = set()
    unique_urls: list[str] = []
    for u in article_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    logger.info(f"[news/{name}] Found {len(unique_urls)} article URLs")

    items: list[NewsItem] = []
    # Guards against a specific failure mode we found in production: a CDN/
    # edge cache in front of the news site (independent of Crawl4AI's own
    # cache_mode setting, which only controls Crawl4AI's local cache) can
    # serve the same cached page for every URL requested, regardless of
    # path. That silently produces N "different" articles that are actually
    # all identical content from one specific page. Two different URLs
    # scraped in the same run should never legitimately produce byte-
    # identical content — if they do, something upstream is stale.
    seen_content_hashes: set = set()
    is_first_request = True

    for url in unique_urls:
        if len(items) >= max_new:
            break

        if state.is_seen(url):
            state.increment_skipped()
            continue

        # Rapid-fire requests (no delay at all previously) are a classic
        # bot-detection trigger. We saw exactly this pattern in production:
        # the first request or two would succeed with real content, then
        # every subsequent request — even with a cache-busting URL that
        # guarantees a unique request — returned identical content back.
        # That rules out simple CDN URL-keyed caching; a soft rate-limit
        # or anti-bot measure silently falling back to a cached "last good"
        # response fits the evidence much better. Pacing requests with a
        # randomized delay (not a fixed interval, which is itself a bot
        # signature) gives the site less reason to treat this as abuse.
        if not is_first_request:
            await asyncio.sleep(random.uniform(3.0, 6.0))
        is_first_request = False

        # Scrape the article
        # only_main_content=False (raw markdown, no PruningContentFilter):
        # we found in production that the content filter was scoring the
        # site's shared nav/boilerplate as higher-value than the actual
        # article body, stripping the real unique text out entirely and
        # leaving nearly-identical leftover content (nav + a "Latest News"
        # widget) across every article page — which looked exactly like a
        # caching bug (byte-identical "content" across different URLs) but
        # was actually a content-extraction setting the whole time. Raw
        # markdown is noisier (includes nav/footer text) but guarantees
        # genuinely unique, real per-page content.
        try:
            article_md = await scrape_markdown(url, proxy="basic", wait_ms=500, only_main_content=False)
        except FirecrawlError as e:
            logger.warning(f"[news/{name}] Failed to scrape {url}: {e}")
            continue

        if not article_md or len(article_md) < 100:
            continue

        content_hash = hashlib.sha256(article_md.encode("utf-8")).hexdigest()
        if content_hash in seen_content_hashes:
            logger.warning(
                f"[news/{name}] Skipping {url} — content is byte-identical to another "
                f"article already scraped this run. This usually means a CDN/edge cache "
                f"in front of the site served a stale/wrong page for this URL, not that "
                f"the article is a real duplicate — not pushing to avoid storing wrong "
                f"content under the wrong headline."
            )
            continue
        seen_content_hashes.add(content_hash)

        # Filter by legal keywords
        if not _is_legal_content(article_md):
            logger.debug(f"[news/{name}] Skipping non-legal article: {url}")
            if not dry_run:
                state.mark_seen(url)  # mark as seen so we don't check again
            continue

        title_m = RE_TITLE.search(article_md)
        # Fallback when no markdown heading matches: use the LAST url segment
        # (the readable slug), not the second-to-last — for URLs like
        # .../article/200058187/high-court-blocks-development-.../ the
        # second-to-last segment is just the numeric article ID, not
        # anything resembling a headline. rstrip("/") first in case the
        # URL has a trailing slash, which would otherwise make the last
        # segment an empty string.
        title = title_m.group(1).strip() if title_m else url.rstrip("/").split("/")[-1].replace("-", " ").title()

        date_m = RE_DATE.search(article_md)
        doc_date = date_m.group(1).strip() if date_m else None

        # Summary should start at the real article heading, not the raw
        # start of the page — with only_main_content=False (raw markdown),
        # every page starts with the same nav/masthead boilerplate before
        # ever reaching the actual article, same as it did in fit_markdown,
        # just now we keep the real content that comes after it too.
        summary_start = title_m.start() if title_m else 0
        summary = article_md[summary_start:summary_start + 800].strip()

        item = NewsItem(
            url=url,
            title=title,
            source=name,
            doc_date=doc_date,
            markdown_summary=summary,
            scraped_at=datetime.now(timezone.utc),
        )

        if dry_run:
            logger.info(f"[DRY RUN] Would push news: {title} — {url}")
        # NOTE: mark_seen() is no longer called here. It used to run right
        # after a successful scrape, before the item was ever pushed to
        # MutemoOS — so a failed push (e.g. a backend rejecting the request)
        # permanently blacklisted the URL with nothing ever delivered.
        # mark_seen() now happens in pusher.py, only after a push actually
        # succeeds.

        items.append(item)

    return items


async def run(dry_run: bool = False) -> list[NewsItem]:
    """
    Main entry point. Scrapes all news sources and returns new legal NewsItems.
    Each source is scraped sequentially to avoid rate-limiting.

    MAX_NEW_PER_RUN is a shared budget across all 6 sources, not a per-source
    cap — otherwise a high-volume source (NewsDay, Herald) could consume the
    full cap on its own before lower-volume sources ever get checked, and the
    real per-run total could run as high as 6x the configured max.
    """
    logger.info(f"[news] Starting scrape (dry_run={dry_run})")
    all_items: list[NewsItem] = []
    remaining = state.get_max_new_per_run()

    for source in NEWS_SOURCES:
        if remaining <= 0:
            logger.info("[news] Per-run budget exhausted — skipping remaining sources")
            break
        items = await _scrape_source(source, dry_run=dry_run, max_new=remaining)
        all_items.extend(items)
        remaining -= len(items)

    state.set_last_scraped("news")
    logger.info(f"[news] Done. {len(all_items)} new legal news items.")
    return all_items
