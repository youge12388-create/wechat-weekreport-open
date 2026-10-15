from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.insight_service import analysis_coverage, insights
from app.services.view_service import resolve_period

router = APIRouter(prefix="/api/insights", tags=["insights"])


@router.get("")
def list_insights(
    session: Annotated[Session, Depends(get_db)],
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict:
    start_date, end_date = resolve_period(start, end)
    return {
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "coverage": analysis_coverage(session, start_date, end_date),
        "items": insights(session, start_date, end_date, limit=limit),
    }
