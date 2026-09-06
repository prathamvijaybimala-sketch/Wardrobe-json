"""
FastAPI application — Railway-hosted wardrobe pipeline service.

Endpoints:
- GET  /health → health check (returns {"status": "ok"})
- POST /sync   → trigger a full pipeline run

Start with:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.config import load_settings
from app.pipeline import run_pipeline

# ─── Logging Configuration ───────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler — log startup/shutdown."""
    logger.info("🚀 Wardrobe Pipeline starting up...")

    # Validate config at startup — fail fast if env vars are missing
    try:
        settings = load_settings()
        logger.info(f"✅ Config loaded: repo={settings.github_repo}, model={settings.gemini_model}")
        # Store settings in app state for reuse
        app.state.settings = settings
    except SystemExit as e:
        logger.critical(f"❌ Configuration error: {e}")
        raise

    yield

    logger.info("🛑 Wardrobe Pipeline shutting down...")


# ─── FastAPI App ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Wardrobe Pipeline",
    description="AI-powered wardrobe cataloging pipeline — "
                "watches Google Drive for clothing photos, tags with Gemini, "
                "commits to GitHub.",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Response Models ─────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    service: str = "wardrobe-pipeline"


class SyncItemDetail(BaseModel):
    id: str | None = None
    drive_file_id: str
    drive_filename: str
    category: str | None = None
    color: str | None = None
    status: str = "unknown"
    review_reason: str | None = None


class SyncResponse(BaseModel):
    success: bool
    new_images_found: int = 0
    items_processed: int = 0
    items_failed: int = 0
    commit_sha: str | None = None
    item_details: list[SyncItemDetail] = []
    duration_seconds: float = 0
    message: str = ""


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """
    Health check endpoint.

    Returns OK if the service is running and config is valid.
    Used by Railway for health checks and by cron job monitors.
    """
    return HealthResponse(status="ok")


@app.post("/sync", response_model=SyncResponse, tags=["Pipeline"])
async def trigger_sync():
    """
    Trigger a full wardrobe pipeline sync.

    This endpoint:
    1. Fetches new clothing photos from Google Drive
    2. Tags each with Gemini for structured attributes
    3. Assigns deterministic IDs
    4. Commits images + updated catalog to GitHub

    Returns a summary of items processed.
    """
    logger.info("📥 /sync triggered")

    start_time = time.time()

    try:
        # Get settings (already validated at startup, but reload in case)
        settings = getattr(app.state, "settings", None)
        if settings is None:
            settings = load_settings()

        # Run the pipeline
        result = run_pipeline(settings)

        duration = time.time() - start_time

        # Build response
        item_details = [
            SyncItemDetail(**detail)
            for detail in result.get("item_details", [])
        ]

        response = SyncResponse(
            success=True,
            new_images_found=result["new_images_found"],
            items_processed=result["items_processed"],
            items_failed=result["items_failed"],
            commit_sha=result.get("commit_sha"),
            item_details=item_details,
            duration_seconds=round(duration, 2),
            message=(
                f"Processed {result['items_processed']} items from "
                f"{result['new_images_found']} new photos in {duration:.1f}s"
            ),
        )

        logger.info(f"✅ /sync complete: {response.message}")
        return response

    except SystemExit as e:
        # Config error
        logger.critical(f"/sync failed — config error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Configuration error: {e}",
        )

    except Exception as e:
        duration = time.time() - start_time
        logger.exception(f"/sync failed after {duration:.1f}s: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline failed: {str(e)}",
        )


@app.get("/", tags=["System"])
async def root():
    """Root endpoint — basic service info."""
    return {
        "service": "wardrobe-pipeline",
        "version": "1.0.0",
        "endpoints": {
            "health": "GET /health",
            "sync": "POST /sync",
        },
    }
