"""FastAPI entry point."""

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.config import get_settings
from app.dashboard.api import router as dashboard_router
from app.mobile.api import router as app_router

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = None
    if settings.scheduler_enabled:
        from app.scheduler import start_scheduler

        scheduler = start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(app_router)
app.include_router(dashboard_router)
DASHBOARD_PAGE = Path(__file__).parent / "static" / "dashboard.html"


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    """The dashboard page; it reads its login token from the link's #fragment and calls /api."""
    return FileResponse(DASHBOARD_PAGE, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
    })


@app.get("/health")
def health() -> dict[str, str]:
    """Simple liveness check."""
    return {"status": "ok", "app": settings.app_name}
