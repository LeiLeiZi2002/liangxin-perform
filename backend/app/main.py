from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

import app.database as database
from app.api.routes.cases import router as cases_router
from app.api.routes.demo_config import router as demo_config_router
from app.api.routes.health import router as health_router
from app.api.routes.live_sessions import router as live_sessions_router
from app.api.routes.provider_config import router as provider_config_router
from app.api.routes.reports import router as reports_router
from app.api.routes.sessions import router as sessions_router
from app.config import get_settings
from app.database import create_db_and_tables
from app.reports.jobs import finalize_interrupted_report_jobs


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    create_db_and_tables()
    with Session(database.engine) as db:
        finalize_interrupted_report_jobs(db)
    yield


settings = get_settings()
app = FastAPI(title="Psych Assessment Demo", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/api")
app.include_router(cases_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(live_sessions_router, prefix="/api")
app.include_router(demo_config_router, prefix="/api")
app.include_router(provider_config_router, prefix="/api")
app.include_router(reports_router, prefix="/api")
