from random import Random
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.cases.domain import CasePackage
from app.cases.loader import CaseRepository
from app.cases.schemas import CaseDrawRequest, CaseMetadata
from app.sessions.models import SCENE_MEDIA, CaseType, Scene

router = APIRouter(prefix="/cases", tags=["cases"])
case_repository = CaseRepository()


def to_metadata(package: CasePackage, scene: Scene | None) -> CaseMetadata:
    case = package.case
    return CaseMetadata(
        case_id=case.case_id,
        title=case.title,
        case_type=case.case_type,
        public_entry=case.public_entry_for(scene),
        estimated_duration_minutes=case.estimated_duration_minutes,
        scene=scene,
        media=SCENE_MEDIA[scene] if scene is not None else None,
        available_scenes=sorted(case.supported_scenes, key=lambda item: item.value),
    )


@router.get("", response_model=list[CaseMetadata])
def list_cases(
    scene: Annotated[Scene | None, Query()] = None,
    case_type: Annotated[CaseType | None, Query()] = None,
) -> list[CaseMetadata]:
    return [
        to_metadata(package, scene)
        for package in case_repository.list_published(scene=scene, case_type=case_type)
    ]


@router.post("/draw", response_model=CaseMetadata)
def draw_case(request: CaseDrawRequest) -> CaseMetadata:
    excluded = set(request.excluded_case_ids)
    candidates = [
        package
        for package in case_repository.list_published(
            scene=request.scene,
            case_type=request.case_type,
        )
        if package.case.case_id not in excluded
    ]
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="没有符合当前场域和个案类型的可用个案",
        )
    selected = Random(request.seed).choice(candidates)
    return to_metadata(selected, request.scene)
