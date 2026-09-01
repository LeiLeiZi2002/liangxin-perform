from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import Session

import app.database as database
from app.cases.loader import CaseRepository
from app.database import get_session
from app.reports.jobs import (
    ReportJobConflictError,
    ReportJobNotFoundError,
    ReportJobProcessor,
    ReportJobService,
)
from app.reports.report_pipeline import ReportPipeline, ReportProcessor
from app.reports.report_provider import ReportProvider
from app.reports.rubric_document import read_rubric_document
from app.reports.schemas import (
    ReportJobRead,
    ReportRead,
    RubricDocumentRead,
    WorkRecordRead,
    WorkRecordUpsert,
)
from app.reports.service import (
    ReportConflictError,
    ReportNotFoundError,
    ReportService,
    WorkRecordEvidenceError,
)
from app.runtime.metrics import ModelCallRecorder
from app.runtime_config import runtime_credential_store

router = APIRouter(tags=["reports"])
SessionDep = Annotated[Session, Depends(get_session)]
case_repository = CaseRepository()


def _build_report_pipeline(engine: object) -> ReportPipeline:
    from sqlalchemy.engine import Engine

    if not isinstance(engine, Engine):
        raise TypeError("engine must be sqlalchemy Engine")
    provider = ReportProvider(
        runtime_credential_store,
        recorder=ModelCallRecorder(engine),
    )
    return ReportPipeline(engine, case_repository, provider)


report_job_processor = ReportProcessor(
    lambda: database.engine,
    _build_report_pipeline,
)


def get_report_job_processor() -> ReportJobProcessor:
    return report_job_processor


ProcessorDep = Annotated[ReportJobProcessor, Depends(get_report_job_processor)]


def _service(db: Session) -> ReportService:
    return ReportService(db, case_repository)


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话或报告不存在")


@router.get("/rubric", response_model=RubricDocumentRead)
def get_rubric_document() -> RubricDocumentRead:
    try:
        title, markdown = read_rubric_document()
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="量规暂时无法读取，请稍后重试。",
        ) from exc
    return RubricDocumentRead(title=title, markdown=markdown)


@router.put("/sessions/{session_id}/work-record", response_model=WorkRecordRead)
def put_work_record(
    session_id: str,
    request: WorkRecordUpsert,
    db: SessionDep,
) -> WorkRecordRead:
    try:
        return _service(db).put_work_record(session_id, request)
    except ReportNotFoundError as exc:
        raise _not_found(exc) from exc
    except ReportConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except WorkRecordEvidenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get("/sessions/{session_id}/work-record", response_model=WorkRecordRead)
def get_work_record(session_id: str, db: SessionDep) -> WorkRecordRead:
    try:
        return _service(db).get_work_record(session_id)
    except ReportNotFoundError as exc:
        raise _not_found(exc) from exc


@router.post(
    "/sessions/{session_id}/reports",
    response_model=ReportJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_report_job(
    session_id: str,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    processor: ProcessorDep,
) -> ReportJobRead:
    try:
        created = ReportJobService(db, case_repository).create(session_id)
    except ReportJobNotFoundError as exc:
        raise _not_found(exc) from exc
    except ReportJobConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if created.should_process:
        background_tasks.add_task(processor.process, created.job.id)
    return created.job


@router.get("/report-jobs/{job_id}", response_model=ReportJobRead)
def get_report_job(job_id: str, db: SessionDep) -> ReportJobRead:
    try:
        return ReportJobService(db, case_repository).get(job_id)
    except ReportJobNotFoundError as exc:
        raise _not_found(exc) from exc


@router.post(
    "/report-jobs/{job_id}/retry",
    response_model=ReportJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_report_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: SessionDep,
    processor: ProcessorDep,
) -> ReportJobRead:
    try:
        retried = ReportJobService(db, case_repository).retry(job_id)
    except ReportJobNotFoundError as exc:
        raise _not_found(exc) from exc
    except ReportJobConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    background_tasks.add_task(processor.process, retried.job.id)
    return retried.job


@router.get("/reports/{report_id}", response_model=ReportRead)
def get_report(report_id: str, db: SessionDep) -> ReportRead:
    try:
        return _service(db).get_report(report_id)
    except ReportNotFoundError as exc:
        raise _not_found(exc) from exc
