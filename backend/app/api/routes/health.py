from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.app_env,
        "db_dialect": settings.db_url.split("://")[0],
    }
