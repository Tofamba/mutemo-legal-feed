"""
Mutemo Legal Feed — entry point.
Runs scrapers on a schedule and pushes new legal content to MutemoOS V2.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Validate required env vars before starting
required = ["FIRECRAWL_API_KEY", "MUTEMO_API_URL", "MUTEMO_ADMIN_TOKEN"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"[error] Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from scheduler import build_scheduler

if __name__ == "__main__":
    # Allow running a single scraper manually:
    # python main.py zimlii
    # python main.py veritas
    # python main.py lrf
    if len(sys.argv) > 1:
        target = sys.argv[1].lower()
        if target == "zimlii":
            from scrapers.zimlii import run
        elif target == "veritas":
            from scrapers.veritas import run
        elif target == "lrf":
            from scrapers.lrf import run
        else:
            print(f"Unknown scraper: {target}. Use: zimlii, veritas, lrf")
            sys.exit(1)
        print(f"[manual] Running {target} scraper now...")
        pushed = run()
        print(f"[manual] Done — {pushed} items pushed")
        sys.exit(0)

    # Normal mode — run scheduler
    print("[feed] Starting Mutemo Legal Feed scheduler...")
    print("[feed] Schedule: ZimLII 04:00 UTC · Veritas 04:30 UTC · LRF 05:00 UTC")
    scheduler = build_scheduler()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("[feed] Scheduler stopped.")