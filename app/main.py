"""
Main FastAPI application entrypoint for the Enterprise Mass Email System and Email Storage Platform.
Configures CORS middleware, lifespan lifecycle events, database initialization,
static asset serving, and REST API router mounting.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import BASE_DIR, settings
from app.db import init_db
from app.routes import (
    auth_scan,
    bulk,
    campaigns,
    dashboard,
    pages,
    smtp,
    storage,
    subscribers,
    templates,
    tracking,
    transactional,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifecycle management:
    - Startup: Ensures storage directories exist and initializes SQLite DB schema.
    - Shutdown: Performs graceful termination of background workers.
    """
    # Startup
    settings.ensure_directories()
    await init_db()
    yield
    # Shutdown


app = FastAPI(
    title="NexusMail Enterprise Mass Email & Storage Platform",
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
app.include_router(pages.router)
app.include_router(auth_scan.router)
app.include_router(dashboard.router)
app.include_router(subscribers.router)
app.include_router(templates.router)
app.include_router(campaigns.router)
app.include_router(storage.router)
app.include_router(smtp.router)
app.include_router(tracking.router)
app.include_router(transactional.router)
app.include_router(bulk.router)


# Alias route for API explorer in frontend: /api/v1/vault/emails -> storage.list_stored_emails
@app.get("/api/v1/vault/emails", tags=["Sent Email Storage Vault"])
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
