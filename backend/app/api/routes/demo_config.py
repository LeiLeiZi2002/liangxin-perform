from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.database import get_session
from app.sessions.schemas import DemoConfig
from app.sessions.service import get_or_create_demo_config

router = APIRouter(prefix="/demo-config", tags=["demo-config"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=DemoConfig)
def get_demo_config(db: SessionDep) -> DemoConfig:
    config = get_or_create_demo_config(db)
    return DemoConfig.model_validate(config)


@router.put("", response_model=DemoConfig)
def update_demo_config(
    request: DemoConfig,
    db: SessionDep,
) -> DemoConfig:
    config = get_or_create_demo_config(db)
    for field_name, value in request.model_dump().items():
        setattr(config, field_name, value)
    db.add(config)
    db.commit()
    db.refresh(config)
    return DemoConfig.model_validate(config)
