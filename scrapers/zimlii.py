"""
ZimLII scraper — Zimbabwe Legal Information Institute
Scrapes recent judgments directly by URL pattern.
Schedule: 04:00 UTC daily (~6am CAT)

ZimLII URL pattern for individual judgments:
  https://zimlii.org/akn/zw/judgment/{court}/{year}/{number}/eng@{date}

Since we don't know exact dates, we use the listing page to find recent judgment numbers,
then fetch each judgment individually.
"""
import re
from datetime import datetime
from firecrawl_client import scrape_url, crawl_url
from state import is_seen, mark_seen, mark_run
from pusher import push_legal_update, push_zlr_entry

YEAR = datetime.utcnow().year

# Direct listing pages — these exist and return judgment lists
ZIMLII_HH_LIST   = f"https://zimlii.org/judgments/ZWHHC/{YEAR}/"
ZIMLII_SC_LIST   = f"https://zimlii.org/judgments/ZWSC/{YEAR}/"
ZIMLII_CCZ_LIST  = f"https://zimlii.org/judgments/ZWCC/{YEAR}/"
ZIMLII_LC_LIST   = f"https://zimlii.org/judgments/ZWLC/{YEAR}/"
ZIMLII_LEGISLATION = "https://zimlii.org/legislation/"

COURT_MAP = {
    "ZWHHC": "zwhhc",
    "ZWSC":  "zwsc",
    "ZWCC":  "zwcc",
    "ZWLC":  "zwlc",
}


def _extract_reference(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts else url


def _is_valid_content(content: str, url: str) -> bool:
    if not content or len(content) < 300:
        return False
    lower = content[:500].lower()
    if "not found" in lower or "error 404" in lower or "page not found" in lower:
        print(f"[zimlii] skipping 404: {url}")
        return False
    return True


def _extract_judgment_urls_from_listing(listing_content: str, court_code: str) -> list:
    """
    Parse the listing page markdown to find individual judgment URLs.
    ZimLII listing pages contain links like:
    /akn/zw/judgment/zwhhc/2026/132/eng@2026-03-25
    """
    court_lower = COURT_MAP.get(court_code, court_code.lower())
    # Match the AKN URL pattern
    pattern = rf'/akn/zw/judgment/{court_lower}/{YEAR}/(\d+)/eng@(\d{{4}}-\d{{2}}-\d{{2}})'
    matches = re.findall(pattern, listing_content)
    urls = []
    seen_nums = set()
    for num, date in matches:
        if num not in seen_nums:
            seen_nums.add(num)
            url = f"https://zimlii.org/akn/zw/judgment/{court_lower}/{YEAR}/{num}/eng@{date}"
            urls.append(url)
    return urls


def scrape_court(court_code: str, list_url: str, limit: int = 5) -> int:
    """Scrape a court's recent judgments by fetching the listing then individual pages."""
    pushed = 0
    court_lower = COURT_MAP.get(court_code, court_code.lower())

    try:
        # Step 1: Get the listing page to find recent judgment URLs
        listing = scrape_url(list_url, formats=["markdown"])
        listing_content = listing.get("markdown", "") or listing.get("content", "")

        if not listing_content or len(listing_content) < 100:
            print(f"[zimlii] listing page returned no content: {list_url}")
            return 0

        # Step 2: Extract individual judgment URLs
        judgment_urls = _extract_judgment_urls_from_listing(listing_content, court_code)

        if not judgment_urls:
            # Fallback: try constructing URLs for recent judgment numbers
            print(f"[zimlii] no URLs found in listing, trying number-based fallback for {court_code}")
            # Find the highest judgment number mentioned in the listing
            nums = re.findall(r'\b(\d{3,4})\b', listing_content)
            if nums:
                latest = max(int(n) for n in nums if int(n) < 2000)
                for num in range(latest, max(latest - limit, 0), -1):
                    url = f"https://zimlii.org/judgments/{court_code}/{YEAR}/{num}/"
                    if not is_seen(url):
                        judgment_urls.append(url)

        print(f"[zimlii] found {len(judgment_urls)} judgment URLs for {court_code}")

        # Step 3: Fetch each judgment (up to limit, skip already seen)
        fetched = 0
        for url in judgment_urls:
            if fetched >= limit:
                break
            if is_seen(url):
                continue

            try:
                result = scrape_url(url, formats=["markdown"])
                content = result.get("markdown", "") or result.get("content", "")

                if not _is_valid_content(content, url):
                    continue

                title = result.get("metadata", {}).get("title", "") or f"{court_code} Judgment"

                # These are proper judgment pages — push as ZLR entry
                is_headnote = bool(re.search(
                    r'\b(HH|SC|CCZ|LC|HB|HM|HMT)-?\d+[-/]\d+|\[' + str(YEAR) + r'\]\s+ZW',
                    content
                ))

                if is_headnote:
                    push_zlr_entry(
                        content=content,
                        filename=f"{court_code}_{_extract_reference(url)}.txt",
                        source="ZimLII",
                        zimlii_url=url,
                    )
                else:
                    push_legal_update(
                        content=content,
                        filename=f"{court_code}_{_extract_reference(url)}.txt",
                        source_type="case_law",
                        source_name="ZimLII",
                        reference=title[:200],
                    )

                mark_seen(url)
                pushed += 1
                fetched += 1
                print(f"[zimlii] pushed {court_code}: {title[:80]}")

            except Exception as e:
                print(f"[zimlii] failed to fetch {url}: {e}")
                continue

    except Exception as e:
        print(f"[zimlii] {court_code} scrape failed: {e}")

    mark_run(f"zimlii_{court_code.lower()}")
    return pushed


def scrape_recent_legislation(limit: int = 3) -> int:
    pushed = 0
    try:
        pages = crawl_url(ZIMLII_LEGISLATION, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue
            content = page.get("markdown", "") or page.get("content", "")
            if not _is_valid_content(content, url):
                continue
            title = page.get("metadata", {}).get("title", "") or _extract_reference(url)
            push_legal_update(
                content=content,
                filename=f"leg_{_extract_reference(url)}.txt",
                source_type="legislation",
                source_name="ZimLII",
                reference=title[:200],
            )
            mark_seen(url)
            pushed += 1
            print(f"[zimlii] pushed legislation: {title[:80]}")
    except Exception as e:
        print(f"[zimlii] legislation scrape failed: {e}")
    mark_run("zimlii_legislation")
    return pushed


def run():
    print(f"[zimlii] starting scrape for {YEAR}...")
    hh  = scrape_court("ZWHHC", ZIMLII_HH_LIST,  limit=5)
    sc  = scrape_court("ZWSC",  ZIMLII_SC_LIST,   limit=3)
    ccz = scrape_court("ZWCC",  ZIMLII_CCZ_LIST,  limit=2)
    lc  = scrape_court("ZWLC",  ZIMLII_LC_LIST,   limit=2)
    leg = scrape_recent_legislation(limit=3)
    total = hh + sc + ccz + lc + leg
    print(f"[zimlii] done — HH:{hh} SC:{sc} CCZ:{ccz} LC:{lc} Leg:{leg} Total:{total}")
    return total
