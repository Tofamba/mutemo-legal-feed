"""
Veritas Zimbabwe scraper — legislation and statutory instruments
veritas.org.zw
Schedule: 04:30 UTC daily (~6:30am CAT)
"""
from firecrawl_client import crawl_url
from state import is_seen, mark_seen, mark_run
from pusher import push_legal_update

VERITAS_BASE = "https://www.veritas.org.zw"
VERITAS_ACTS = "https://www.veritas.org.zw/acts"
VERITAS_SI = "https://www.veritas.org.zw/statutory-instruments"


def _extract_reference(url: str, title: str = "") -> str:
    if title:
        return title[:200]
    parts = url.rstrip("/").split("/")
    return parts[-1].replace("-", " ").title() if parts else url


def scrape_acts(limit: int = 3) -> int:
    pushed = 0
    try:
        pages = crawl_url(VERITAS_ACTS, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not content or len(content) < 200:
                continue

            title = page.get("metadata", {}).get("title", "") or ""
            reference = _extract_reference(url, title)

            push_legal_update(
                content=content,
                filename=f"veritas_{reference[:80].replace(' ', '_')}.txt",
                source_type="legislation",
                source_name="Veritas Zimbabwe",
                reference=reference,
            )

            mark_seen(url)
            pushed += 1
            print(f"[veritas] pushed act: {reference[:80]}")

    except Exception as e:
        print(f"[veritas] acts scrape failed: {e}")

    mark_run("veritas_acts")
    return pushed


def scrape_statutory_instruments(limit: int = 5) -> int:
    pushed = 0
    try:
        pages = crawl_url(VERITAS_SI, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not content or len(content) < 200:
                continue

            title = page.get("metadata", {}).get("title", "") or ""
            reference = _extract_reference(url, title)

            push_legal_update(
                content=content,
                filename=f"veritas_si_{reference[:80].replace(' ', '_')}.txt",
                source_type="legislation",
                source_name="Veritas Zimbabwe",
                reference=reference,
            )

            mark_seen(url)
            pushed += 1
            print(f"[veritas] pushed SI: {reference[:80]}")

    except Exception as e:
        print(f"[veritas] SI scrape failed: {e}")

    mark_run("veritas_si")
    return pushed


def run():
    print("[veritas] starting scrape...")
    a = scrape_acts(limit=3)
    s = scrape_statutory_instruments(limit=5)
    print(f"[veritas] done — {a} acts, {s} SIs pushed")
    return a + s