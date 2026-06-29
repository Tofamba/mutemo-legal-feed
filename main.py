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
  LAWS_AFRICA_TOKEN     — Laws.Africa API token (free tier at platform.laws.africa)
"""

import asyncio
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Query

import state
import scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

FEED_ADMIN_TOKEN = os.environ.get("FEED_ADMIN_TOKEN", "")
DRY_RUN          = os.environ.get("DRY_RUN", "false").lower() == "true"

if DRY_RUN:
    logger.warning("⚠️  DRY_RUN mode enabled — no content will be pushed to MutemoOS")

VALID_SOURCES = ["zimlii", "veritas", "lrf", "news", "zlhr", "lawsafrica", "zelj"]


def _require_admin(x_feed_admin_token: str = Header(default="")) -> None:
    if not FEED_ADMIN_TOKEN:
        return
    if x_feed_admin_token != FEED_ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Feed-Admin-Token header")


_scheduler_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task
    logger.info("🚀 Legal Intelligence Feed starting up...")
    logger.info(f"   DRY_RUN: {DRY_RUN}")
    logger.info(f"   MutemoOS: {os.environ.get('MUTEMOS_BASE_URL', 'not set')}")
    logger.info(f"   Data dir: {os.environ.get('DATA_DIR', './data')}")

    _scheduler_task = asyncio.create_task(
        scheduler.scheduler_loop(dry_run=DRY_RUN),
        name="legal_feed_scheduler",
    )
    logger.info("📅 Scheduler started (Mon-Fri: ZimLII/Veritas/News/ZLHR | Mondays: LRF | Sundays: Laws.Africa/ZELJ)")

    yield

    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("Legal Intelligence Feed shut down cleanly.")


app = FastAPI(
    title="MutemoOS Legal Intelligence Feed",
    description=(
        "Automated legal content scraper for Zimbabwe law firms. "
        "Monitors ZimLII, Veritas, LRF, ZLHR, Laws.Africa and news sources, "
        "then pushes content to MutemoOS."
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
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
    stats = state.get_stats()
    scheduler_running = (
        _scheduler_task is not None
        and not _scheduler_task.done()
    )
    total_pushed = stats.get("stats", {}).get("total_pushed", 0)
    total_failed = stats.get("stats", {}).get("total_failed", 0)
    total = total_pushed + total_failed
    error_rate = round(total_failed / total, 3) if total > 0 else 0.0
    score = 100.0
    if not scheduler_running:
        score -= 50.0
    if error_rate > 0.1:
        score -= min(40.0, error_rate * 100)
    return {
        "status": "ok" if scheduler_running else "degraded",
        "score": round(max(0.0, score), 1),
        "p95_latency": 0.0,
        "error_rate": error_rate,
        "timestamp": _time.time(),
    }


@app.get("/")
async def root():
    return {"service": "MutemoOS Legal Intelligence Feed", "status": "running", "docs": "/docs"}


@app.post("/trigger/all")
async def trigger_all(
    background_tasks: BackgroundTasks,
    dry_run: bool = Query(default=None),
    x_feed_admin_token: str = Header(default=""),
):
    _require_admin(x_feed_admin_token)
    effective_dry_run = dry_run if dry_run is not None else DRY_RUN

    async def _run():
        logger.info(f"[trigger] Manual run_all triggered (dry_run={effective_dry_run})")
        results = await scheduler.run_all(dry_run=effective_dry_run)
        logger.info(f"[trigger] Manual run_all complete: {results}")

    background_tasks.add_task(_run)
    return {
        "status": "triggered",
        "sources": VALID_SOURCES,
        "dry_run": effective_dry_run,
        "message": "All scrapers running in background. Check /health for stats.",
    }


@app.post("/trigger/{source}")
async def trigger_source(
    source: str,
    background_tasks: BackgroundTasks,
    dry_run: bool = Query(default=None),
    x_feed_admin_token: str = Header(default=""),
):
    _require_admin(x_feed_admin_token)
    if source not in VALID_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{source}'. Valid: {VALID_SOURCES}"
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


@app.get("/failures")
async def get_failures(x_feed_admin_token: str = Header(default="")):
    _require_admin(x_feed_admin_token)
    return {"failures": state.get_failures()}


@app.delete("/failures")
async def clear_failures(x_feed_admin_token: str = Header(default="")):
    _require_admin(x_feed_admin_token)
    state.clear_failures()
    return {"status": "cleared"}


@app.get("/stats")
async def get_stats(x_feed_admin_token: str = Header(default="")):
    _require_admin(x_feed_admin_token)
    return state.get_stats()
