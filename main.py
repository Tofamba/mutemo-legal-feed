"""
main.py — MutemoOS Legal Intelligence Feed Service

A standalone FastAPI service that:
  1. Scrapes ZimLII, Veritas Zimbabwe, and LRF daily using Firecrawl
  2. Downloads PDFs for each new judgment/legislation/digest
  3. Pushes new content to MutemoOS via webhook with retry logic
  4. Exposes a health endpoint and manual trigger endpoints

Environment variables (all required unless marked optional):
  FIRECRAWL_API_KEY     — Firecrawl API key (get one at firecrawl.dev)
  MUTEMOS_BASE_URL      — MutemoOS base URL (e.g. https://mutemoos-production.up.railway.app)
  MUTEMOS_ADMIN_TOKEN   — MutemoOS admin token (X-Admin-Token header)
  MUTEMOS_FIRM_ID       — Firm UUID in MutemoOS (optional, for multi-tenant future)
  FEED_ADMIN_TOKEN      — Token to protect this service's own trigger endpoints
  DATA_DIR              — Path to persistent volume for state (default: ./data)
  DRY_RUN               — Set to "true" to scrape without pushing (optional, default: false)
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Query
from fastapi.responses import JSONResponse

import state
import scheduler

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

FEED_ADMIN_TOKEN = os.environ.get("FEED_ADMIN_TOKEN", "")
DRY_RUN          = os.environ.get("DRY_RUN", "false").lower() == "true"

if DRY_RUN:
    logger.warning("⚠️  DRY_RUN mode enabled — no content will be pushed to MutemoOS")


# ── Auth helper ───────────────────────────────────────────────────────────────

def _require_admin(x_feed_admin_token: str = Header(default="")) -> None:
    if not FEED_ADMIN_TOKEN:
        # No token configured — allow all (useful for local dev)
        return
    if x_feed_admin_token != FEED_ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Feed-Admin-Token header")


# ── Lifespan ──────────────────────────────────────────────────────────────────

_scheduler_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task
    logger.info("🚀 Legal Intelligence Feed starting up...")
    logger.info(f"   DRY_RUN: {DRY_RUN}")
    logger.info(f"   MutemoOS: {os.environ.get('MUTEMOS_BASE_URL', 'not set')}")
    logger.info(f"   Data dir: {os.environ.get('DATA_DIR', './data')}")

    # Start the background scheduler
    _scheduler_task = asyncio.create_task(
        scheduler.scheduler_loop(dry_run=DRY_RUN),
        name="legal_feed_scheduler",
    )
    logger.info("📅 Scheduler started (ZimLII 06:00 CAT | Veritas 06:30 CAT | LRF 07:00 CAT)")

    yield

    # Shutdown
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("Legal Intelligence Feed shut down cleanly.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="MutemoOS Legal Intelligence Feed",
    description=(
        "Automated legal content scraper for Zimbabwe law firms. "
        "Monitors ZimLII, Veritas Zimbabwe, and LRF for new judgments, "
        "legislation, and case digests, then pushes them to MutemoOS."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint. Returns service status and scrape stats."""
    stats = state.get_stats()
    scheduler_running = (
        _scheduler_task is not None
        and not _scheduler_task.done()
    )
    return {
        "status": "ok",
        "dry_run": DRY_RUN,
        "scheduler_running": scheduler_running,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **stats,
    }


@app.get("/health/alerts")
async def health_alerts():
    """AlertEngine-compatible health endpoint."""
    import time
    stats = state.get_stats()

    scheduler_running = (
        _scheduler_task is not None
        and not _scheduler_task.done()
    )

    total = stats.get("total_pushed", 0) + stats.get("total_failed", 0)
    error_rate = (stats.get("total_failed", 0) / total) if total > 0 else 0.0

    if not scheduler_running:
        score = 0.0
    elif error_rate > 0.5:
        score = 20.0
    elif error_rate > 0.2:
        score = 60.0
    elif error_rate > 0.1:
        score = 80.0
    else:
        score = 100.0

    return {
        "status": "ok" if scheduler_running else "critical",
        "score": score,
        "p95_latency": 0.0,
        "error_rate": round(error_rate, 4),
        "timestamp": time.time(),
        "scheduler_running": scheduler_running,
    }


@app.get("/")
async def root():
    return {"service": "MutemoOS Legal Intelligence Feed", "status": "running", "docs": "/docs"}


# ── Manual trigger endpoints ──────────────────────────────────────────────────

@app.post("/trigger/all")
async def trigger_all(
    background_tasks: BackgroundTasks,
    dry_run: bool = Query(default=None, description="Override DRY_RUN env var for this run"),
    x_feed_admin_token: str = Header(default=""),
):
    """
    Manually trigger all three scrapers.
    Runs in the background — returns immediately with a job ID.
    Protected by X-Feed-Admin-Token header.
    """
    _require_admin(x_feed_admin_token)
    effective_dry_run = dry_run if dry_run is not None else DRY_RUN

    async def _run():
        logger.info(f"[trigger] Manual run_all triggered (dry_run={effective_dry_run})")
        results = await scheduler.run_all(dry_run=effective_dry_run)
        logger.info(f"[trigger] Manual run_all complete: {results}")

    background_tasks.add_task(_run)
    return {
        "status": "triggered",
        "sources": ["zimlii", "veritas", "lrf", "news"],
        "dry_run": effective_dry_run,
        "message": "All scrapers running in background. Check /health for stats.",
    }


@app.post("/trigger/{source}")
async def trigger_source(
    source: str,
    background_tasks: BackgroundTasks,
    dry_run: bool = Query(default=None, description="Override DRY_RUN env var for this run"),
    x_feed_admin_token: str = Header(default=""),
):
    """
    Manually trigger a single scraper by name.
    valid_sources = ["zimlii", "veritas", "lrf", "news", "zlhr", "lawsafrica", "zelj"]
    Protected by X-Feed-Admin-Token header.
    """
    _require_admin(x_feed_admin_token)
    valid_sources = ["zimlii", "veritas", "lrf", "news"]
    if source not in valid_sources:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{source}'. Valid: {valid_sources}"
        )

    effective_dry_run = dry_run if dry_run is not None else DRY_RUN

    async def _run():
        logger.info(f"[trigger] Manual {source} triggered (dry_run={effective_dry_run})")
        result = await scheduler.run_single(source, dry_run=effective_dry_run)
        logger.info(f"[trigger] Manual {source} complete: {result}")

    background_tasks.add_task(_run)
    return {
        "status": "triggered",
        "source": source,
        "dry_run": effective_dry_run,
        "message": f"{source} scraper running in background. Check /health for stats.",
    }


# ── Audit endpoints ───────────────────────────────────────────────────────────

@app.get("/failures")
async def get_failures(x_feed_admin_token: str = Header(default="")):
    """Return the list of push failures for audit."""
    _require_admin(x_feed_admin_token)
    return {"failures": state.get_failures()}


@app.delete("/failures")
async def clear_failures(x_feed_admin_token: str = Header(default="")):
    """Clear the push failure log after manual review."""
    _require_admin(x_feed_admin_token)
    state.clear_failures()
    return {"status": "cleared"}


@app.get("/stats")
async def get_stats(x_feed_admin_token: str = Header(default="")):
    """Return detailed stats and last-scraped timestamps."""
    _require_admin(x_feed_admin_token)
    return state.get_stats()
