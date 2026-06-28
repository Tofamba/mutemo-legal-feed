"""
Zimbabwe legal news scraper
Monitors 6 news sources for legal, court, and regulatory stories.
Schedule: runs after ZimLII, Veritas, LRF — add to scheduler at 05:30 UTC (~7:30am CAT)

Sources:
  - NewsDay (newsday.co.zw)
  - The Herald (herald.co.zw)
  - Financial Gazette (fingaz.co.zw)
  - Zimbabwe Independent (theindependent.co.zw)
  - Chronicle (chronicle.co.zw)
  - Business Weekly (businessweekly.co.zw)
"""
import re
from firecrawl_client import crawl_url, scrape_url
from state import is_seen, mark_seen, mark_run
from pusher import push_legal_update

# Legal keywords to filter relevant articles
LEGAL_KEYWORDS = [
    "court", "judge", "judgment", "magistrate", "high court", "supreme court",
    "constitutional court", "labour court", "appeal", "sentence", "convicted",
    "acquitted", "interdict", "injunction", "lawsuit", "litigation", "legal",
    "attorney", "advocate", "lawyer", "prosecution", "accused", "defendant",
    "plaintiff", "applicant", "respondent", "verdict", "ruling", "order",
    "zimra", "revenue authority", "tax", "vat", "customs",
    "rbn", "reserve bank", "financial intelligence", "aml",
    "secz", "securities", "stock exchange", "zse",
    "companies act", "companies registry", "liquidation", "winding up",
    "eviction", "ejectment", "spoliation", "rei vindicatio",
    "divorce", "matrimonial", "custody", "maintenance",
    "mining", "mining commissioner", "mining claims",
    "parliament", "bill", "act", "statutory instrument", "gazette",
    "constitution", "constitutional", "bill of rights",
    "criminal", "murder", "theft", "fraud", "corruption",
    "arrest", "bail", "remand", "plea",
]

NEWS_SOURCES = [
    {
        "name": "NewsDay",
        "source_name": "NewsDay Zimbabwe",
        "url": "https://www.newsday.co.zw/local-news/",
        "limit": 5,
    },
    {
        "name": "Herald",
        "source_name": "The Herald Zimbabwe",
        "url": "https://www.herald.co.zw/category/local/",
        "limit": 5,
    },
    {
        "name": "Financial Gazette",
        "source_name": "Financial Gazette Zimbabwe",
        "url": "https://www.fingaz.co.zw/category/news/",
        "limit": 4,
    },
    {
        "name": "Zimbabwe Independent",
        "source_name": "Zimbabwe Independent",
        "url": "https://www.theindependent.co.zw/category/local/",
        "limit": 4,
    },
    {
        "name": "Chronicle",
        "source_name": "The Chronicle Zimbabwe",
        "url": "https://www.chronicle.co.zw/category/local/",
        "limit": 3,
    },
    {
        "name": "Business Weekly",
        "source_name": "Business Weekly Zimbabwe",
        "url": "https://businessweekly.co.zw/",
        "limit": 3,
    },
]


def _is_legal_article(content: str, title: str = "") -> bool:
    """Return True if the article contains legal keywords."""
    text = (title + " " + content[:3000]).lower()
    matches = sum(1 for kw in LEGAL_KEYWORDS if kw in text)
    return matches >= 2


def _is_valid_content(content: str, url: str) -> bool:
    if not content or len(content) < 300:
        return False
    lower = content[:300].lower()
    if "404" in lower or "page not found" in lower or "not found" in lower:
        print(f"[news] skipping 404: {url}")
        return False
    return True


def scrape_news_source(source: dict) -> int:
    pushed = 0
    name = source["name"]
    try:
        pages = crawl_url(source["url"], limit=source["limit"])
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not _is_valid_content(content, url):
                continue

            title = page.get("metadata", {}).get("title", "") or url

            # Only push articles relevant to law/courts/regulation
            if not _is_legal_article(content, title):
                print(f"[news] skipping non-legal article: {title[:60]}")
                continue

            push_legal_update(
                content=f"SOURCE: {source['source_name']}\nURL: {url}\nTITLE: {title}\n\n{content}",
                filename=f"{name.lower()}_{url.rstrip('/').split('/')[-1][:60]}.txt",
                source_type="news",
                source_name=source["source_name"],
                reference=title[:200],
            )

            mark_seen(url)
            pushed += 1
            print(f"[news] pushed from {name}: {title[:80]}")

    except Exception as e:
        print(f"[news] {name} scrape failed: {e}")

    mark_run(f"news_{name.lower()}")
    return pushed


def run():
    print("[news] starting legal news scrape...")
    total = 0
    for source in NEWS_SOURCES:
        count = scrape_news_source(source)
        total += count
        print(f"[news] {source['name']}: {count} articles pushed")
    print(f"[news] done — {total} total articles pushed")
    return total
