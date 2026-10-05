"""
Main FastAPI application entrypoint for the Enterprise Mass Email System and Email Storage Platform.
Configures CORS middleware, lifespan lifecycle events, database initialization,
static asset serving, and REST API router mounting.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import get_current_user
from app.config import BASE_DIR, settings
from app.db import init_db
from app.routes import (
    auth,
    auth_scan,
    bulk,
    campaigns,
    dashboard,
    logs,
    pages,
    smtp,
    storage,
    subscribers,
    templates,
    tracking,
    transactional,
)

from app.logging_service import setup_logging_interceptor
from app.scheduler import campaign_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifecycle management:
    - Startup: Ensures storage directories exist, initializes DB, and starts campaign scheduler.
    - Shutdown: Performs graceful termination of background scheduler and workers.
    """
    # Startup
    setup_logging_interceptor()
    settings.ensure_directories()
    await init_db()
    await campaign_scheduler.start()
    yield
    # Shutdown
    await campaign_scheduler.stop()


app = FastAPI(
    title="Bitmail Enterprise Mass Email & Storage Platform",
    description="High-Throughput Mass Email Orchestration, SMTP Relay Management, and Email Storage Archive Vault API",
    version="2.4.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# Enable CORS for external frontends and API integrations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------
# Register Routers
# ----------------------------------------------------------------------
# Public / Unrestricted routes (Pages, Authentication, QR Scan, Email Tracking)
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(auth_scan.router)
app.include_router(tracking.router)

# Protected API Routers (Locked strictly behind user authentication)
app.include_router(dashboard.router, dependencies=[Depends(get_current_user)])
app.include_router(subscribers.router, dependencies=[Depends(get_current_user)])
app.include_router(templates.router, dependencies=[Depends(get_current_user)])
app.include_router(campaigns.router, dependencies=[Depends(get_current_user)])
app.include_router(storage.router, dependencies=[Depends(get_current_user)])
app.include_router(smtp.router, dependencies=[Depends(get_current_user)])
app.include_router(logs.router, dependencies=[Depends(get_current_user)])
app.include_router(transactional.router, dependencies=[Depends(get_current_user)])
app.include_router(bulk.router, dependencies=[Depends(get_current_user)])


# Alias route for API explorer in frontend: /api/v1/vault/emails -> storage.list_stored_emails
@app.get(
    "/api/v1/vault/emails",
    tags=["Sent Email Storage Vault"],
    dependencies=[Depends(get_current_user)]
)
async def alias_vault_emails(
    search_query: str = None,
    q: str = None,
    campaign_id: str = None,
    recipient_email: str = None,
    status: str = None,
    limit: int = 50,
    offset: int = 0
):
    """API explorer alias for querying stored emails in the archive vault."""
    return await storage.list_stored_emails(
        search=search_query,
        q=q,
        campaign_id=campaign_id,
        recipient=recipient_email,
        status=status,
        limit=limit,
        offset=offset
    )


# ----------------------------------------------------------------------
# Static Files Serving
# ----------------------------------------------------------------------
static_dir = Path(__file__).resolve().parent / "static"

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/health", tags=["System Health"])
async def health_check():
    """Health check probe for container and uptime monitoring."""
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "database": str(settings.DATABASE_PATH),
        "storage_vault": str(settings.EML_STORAGE_DIR)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
