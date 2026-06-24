"""
ZimLII scraper — Zimbabwe Legal Information Institute
Scrapes recent judgments and legislation from zimlii.org
Schedule: 04:00 UTC daily (~6am CAT)
"""
import re
from firecrawl_client import scrape_url, crawl_url
from state import is_seen, mark_seen, mark_run
from pusher import push_legal_update, push_zlr_entry

ZIMLII_BASE = "https://zimlii.org"
ZIMLII_JUDGMENTS = "https://zimlii.org/zw/judgment"
ZIMLII_LEGISLATION = "https://zimlii.org/zw/legislation"


def _extract_reference(url: str) -> str:
    """Extract a short reference from a ZimLII URL."""
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts else url


def scrape_recent_judgments(limit: int = 5) -> int:
    """Scrape recent judgments from ZimLII and push to MutemoOS."""
    pushed = 0
    try:
        pages = crawl_url(ZIMLII_JUDGMENTS, limit=limit)
        for page in pages:
            url = page.get("metadata"),