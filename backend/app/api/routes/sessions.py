from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, col, select

from app.cases.domain import CaseStatus
from app.cases.loader import CaseNotFoundError, CaseRepository
from app.database import get_session
from app.runtime.character_kernel import CHARACTER_PROMPT_ENGINE, WORKFLOW_ENGINE
from app.runtime.character_provider import (
    CharacterLoadError,
    CharacterNotFoundError,
    CharacterRepository,
)
from app.runtime.domain import initialize_actor_state
from app.sessions.models import (
    SCENE_MEDIA,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
    TurnRecord,
)
from app.sessions.schemas import (
    EndSessionRequest,
    SessionCreate,
    SessionDetail,
    SessionRead,
    TurnRead,
)
from app.sessions.service import get_or_create_demo_config, mark_session_ended

router = APIRouter(prefix="/sessions", tags=["sessions"])
SessionDep = Annotated[Session, Depends(get_session)]
case_repository = CaseRepository()
character_repository = CharacterRepository()


@router.post("", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
def create_session(request: SessionCreate, db: SessionDep) -> SessionRecord:
    demo_config = get_or_create_demo_config(db)
    if request.mode is SessionMode.assessment:
        if request.scene is not None and request.scene is not demo_config.scene:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="正式测评场域与当前管理配置不一致",
            )
        if request.case_type is not None and request.case_type is not demo_config.case_type:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="正式测评个案类型与当前管理配置不一致",
            )
        scene = demo_config.scene
        case_type = demo_config.case_type
    else:
        scene = request.scene or demo_config.scene
        case_type = request.case_type or demo_config.case_type
    try:
        package = case_repository.get(request.case_id)
    except CaseNotFoundError as exc:
        raise HTTPException(status_code=422, detail="个案不存在") from exc
    case = package.case
    if case.status is not CaseStatus.published:
        raise HTTPException(status_code=422, detail="个案尚未发布")
    if case.case_type is not case_type:
        raise HTTPException(status_code=422, detail="个案类型与会话不匹配")
    if scene not in case.supported_scenes:
        raise HTTPException(status_code=422, detail="个案不支持当前场域")

    try:
        character_repository.get_for_case(case)
    except CharacterNotFoundError as exc:
        if case.character_required:
            raise HTTPException(
                status_code=422,
                detail="个案要求角色配置，但配置文件不存在",
            ) from exc
        if scene is Scene.hotline:
            raise HTTPException(
                status_code=422,
                detail="该热线个案暂时无法开始，请联系管理员检查配置",
            ) from exc
        runtime_engine = WORKFLOW_ENGINE
        state_json: dict[str, object] = {
            "actor_state": initialize_actor_state(package).model_dump(mode="json"),
            "runtime": {
                "engine": runtime_engine,
                "phase": "listening",
            },
        }
    except CharacterLoadError as exc:
        raise HTTPException(status_code=422, detail="角色配置与个案不兼容") from exc
    else:
        runtime_engine = CHARACTER_PROMPT_ENGINE
        state_json = {
            "runtime": {
                "engine": runtime_engine,
                "phase": "listening",
            }
        }

    record = SessionRecord(
        mode=request.mode,
        scene=scene,
        case_type=case_type,
        case_id=request.case_id,
        media=SCENE_MEDIA[scene],
        model_mode=ModelMode.live,
        state_json=state_json,
        soft_duration_minutes=(
            None
            if request.mode is SessionMode.experience
            else demo_config.soft_duration_minutes
        ),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/{session_id}", response_model=SessionDetail)
def get_session_detail(session_id: str, db: SessionDep) -> SessionDetail:
    record = db.get(SessionRecord, session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    turns = list(
        db.exec(
            select(TurnRecord)
            .where(TurnRecord.session_id == session_id)
            .order_by(col(TurnRecord.sequence))
        ).all()
    )
    return SessionDetail(
        session=SessionRead.model_validate(record),
        transcript=[
            TurnRead.model_validate(turn).model_copy(
                update={"audio_available": bool(turn.audio_path)}
            )
            for turn in turns
        ],
    )


@router.post("/{session_id}/end", response_model=SessionRead)
def end_session(
    session_id: str,
    request: EndSessionRequest,
    db: SessionDep,
) -> SessionRecord:
    record = db.get(SessionRecord, session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if mark_session_ended(record, request.reason):
        db.add(record)
        db.commit()
        db.refresh(record)
    return record
