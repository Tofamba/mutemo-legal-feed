"""
Pushes scraped legal content to MutemoOS V2 API.
"""
import os
import httpx
import tempfile

MUTEMO_API_URL = os.environ.get("MUTEMO_API_URL", "https://mutemo.tofamba.com")
MUTEMO_ADMIN_TOKEN = os.environ.get("MUTEMO_ADMIN_TOKEN", "")

HEADERS = {"X-Admin-Token": MUTEMO_ADMIN_TOKEN}


def push_legal_update(
    content: str,
    filename: str,
    source_type: str,
    source_name: str,
    reference: str = "",
) -> dict:
    """
    Push a legal update (legislation or case law) to MutemoOS.
    source_type: 'legislation' or 'case_law'
    source_name: e.g. 'ZimLII', 'Veritas', 'LRF'
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            response = httpx.post(
                f"{MUTEMO_API_URL}/api/legal-updates/upload",
                headers=HEADERS,
                data={
                    "source_type": source_type,
                    "source_name": source_name,
                    "reference": reference,
                },
                files={"file": (filename, f, "text/plain")},
                timeout=60,
            )
        response.raise_for_status()
        return response.json()
    finally:
        os.unlink(tmp_path)


def push_zlr_entry(
    content: str,
    filename: str,
    source: str = "ZimLII",
    volume_year: str = None,
    zimlii_url: str = None,
) -> dict:
    """Push a ZLR case entry to MutemoOS."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            data = {"source": source}
            if volume_year:
                data["volume_year"] = volume_year
            if zimlii_url:
                data["zimlii_url"] = zimlii_url
            response = httpx.post(
                f"{MUTEMO_API_URL}/api/zlr/upload",
                headers=HEADERS,
                data=data,
                files={"file": (filename, f, "text/plain")},
                timeout=60,
            )
        response.raise_for_status()
        return response.json()
    finally:
        os.unlink(tmp_path)