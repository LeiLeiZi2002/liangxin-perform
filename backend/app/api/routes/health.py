from typing import Literal, TypedDict

from fastapi import APIRouter

router = APIRouter(tags=["health"])


class HealthResponse(TypedDict):
    status: Literal["ready"]
    service: Literal["psych-assessment-demo"]


@router.get("/health")
def get_health() -> HealthResponse:
    return {
        "status": "ready",
        "service": "psych-assessment-demo",
    }
