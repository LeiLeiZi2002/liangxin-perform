from dataclasses import dataclass

from sqlmodel import Session, col, select

from app.cases.loader import CaseRepository
from app.database import begin_sqlite_immediate
from app.reports.models import (
    ReportDraftStatus,
    ReportJobRecord,
    ReportJobStage,
    ReportRecord,
    WorkRecordRecord,
)
from app.reports.schemas import ReportRead, WorkRecordRead, WorkRecordUpsert
from app.reports.scoring_domain import (
    BottomLineEvent,
    DimensionResult,
    MaterialConflict,
    ResultSummary,
)
from app.sessions.models import Media, Scene, SessionRecord, SessionStatus, TurnRecord, utc_now

FIXED_REPORT_DISCLAIMERS: tuple[str, ...] = (
    "本结果不支持对受测者是否胜任心理服务岗位作出判断。",
    "本结果不得用于录用、淘汰、晋升或其他有后果的人事决定。",
    "本结果不得用于不同案例、不同任务或不同受测者之间的横向比较。",
    "本结果不得用于推断受测者在真实工作环境中的实际表现。",
    "大模型分析结果必须核对原始对话和工作记录。",
    "本报告仅用于发展性反馈。",
)


@dataclass(frozen=True, slots=True)
class ReportWrite:
    job_id: str
    session_id: str
    case_id: str
    scene: Scene
    media: Media
    summary: ResultSummary
    dimensions: list[DimensionResult]
    bottom_line_events: list[BottomLineEvent]
    material_conflicts: list[MaterialConflict]
    screening_gap: bool
    rubric_fingerprint: str
    case_package_fingerprint: str
    model_fingerprint: str
    prompt_fingerprint: str
    input_fingerprint: str
    ai_draft_status: ReportDraftStatus


class ReportNotFoundError(LookupError):
    pass


class ReportConflictError(RuntimeError):
    pass


class WorkRecordEvidenceError(ValueError):
    pass


class ReportService:
    def __init__(self, db: Session, cases: CaseRepository) -> None:
        self._db = db
        self._cases = cases

    def put_work_record(
        self,
        session_id: str,
        request: WorkRecordUpsert,
    ) -> WorkRecordRead:
        begin_sqlite_immediate(self._db)
        session_record = self._session(session_id)
        report_job = self._db.exec(
            select(ReportJobRecord).where(ReportJobRecord.session_id == session_id)
        ).first()
        if report_job is not None:
            raise ReportConflictError("报告任务已经创建，工作记录已冻结，不能再修改。")
        report = self._db.exec(
            select(ReportRecord).where(ReportRecord.session_id == session_id)
        ).first()
        if report is not None:
            raise ReportConflictError("报告已经生成，工作记录已冻结，不能再修改。")
        if session_record.status is not SessionStatus.ended:
            raise ReportConflictError("会话结束后才能提交工作记录。")
        valid_turn_ids = {
            turn.id
            for turn in self._turns(session_id)
            if turn.speaker.value in {"worker", "client"}
        }
        invalid = [item for item in request.risk_evidence_turn_ids if item not in valid_turn_ids]
        if invalid:
            raise WorkRecordEvidenceError("风险证据必须引用当前会话中的工作者或来访者回合。")
        existing = self._db.exec(
            select(WorkRecordRecord).where(WorkRecordRecord.session_id == session_id)
        ).first()
        payload = request.model_dump(mode="json")
        if existing is None:
            existing = WorkRecordRecord(session_id=session_id, **payload)
        else:
            for name, value in payload.items():
                setattr(existing, name, value)
            existing.updated_at = utc_now()
        self._db.add(existing)
        self._db.commit()
        self._db.refresh(existing)
        return WorkRecordRead.model_validate(existing)

    def get_work_record(self, session_id: str) -> WorkRecordRead:
        self._session(session_id)
        record = self._db.exec(
            select(WorkRecordRecord).where(WorkRecordRecord.session_id == session_id)
        ).first()
        if record is None:
            raise ReportNotFoundError(session_id)
        return WorkRecordRead.model_validate(record)

    def save_report(
        self,
        write: ReportWrite,
        *,
        final_stage: ReportJobStage,
        last_error: str | None,
    ) -> ReportRead:
        if final_stage not in {ReportJobStage.succeeded, ReportJobStage.partial}:
            raise ValueError("报告只能收口为 succeeded 或 partial")
        begin_sqlite_immediate(self._db)
        session_record = self._session(write.session_id)
        job = self._db.get(ReportJobRecord, write.job_id)
        if job is None or job.session_id != write.session_id:
            raise ReportNotFoundError(write.job_id)
        if session_record.case_id != write.case_id:
            raise ReportConflictError("报告案例与冻结会话不一致。")
        if session_record.scene is not write.scene or session_record.media is not write.media:
            raise ReportConflictError("报告场域或媒介与冻结会话不一致。")
        payload: dict[str, object] = {
            "job_id": write.job_id,
            "case_id": write.case_id,
            "scene": write.scene,
            "media": write.media,
            "summary_json": write.summary.model_dump(mode="json"),
            "dimensions_json": [
                item.model_dump(mode="json") for item in write.dimensions
            ],
            "bottom_line_events_json": [
                item.model_dump(mode="json") for item in write.bottom_line_events
            ],
            "material_conflicts_json": [
                item.model_dump(mode="json") for item in write.material_conflicts
            ],
            "screening_gap": write.screening_gap,
            "disclaimers_json": list(FIXED_REPORT_DISCLAIMERS),
            "rubric_fingerprint": write.rubric_fingerprint,
            "case_package_fingerprint": write.case_package_fingerprint,
            "model_fingerprint": write.model_fingerprint,
            "prompt_fingerprint": write.prompt_fingerprint,
            "input_fingerprint": write.input_fingerprint,
            "ai_draft_status": write.ai_draft_status,
        }
        report = ReportRecord(session_id=write.session_id, **payload)
        self._db.add(report)
        try:
            self._db.flush()
            from app.reports.jobs import (
                ReportJobProgressUpdate,
                apply_report_job_progress_update,
            )

            apply_report_job_progress_update(
                job,
                ReportJobProgressUpdate(
                    stage=final_stage,
                    report_id=report.id,
                    last_error=last_error,
                    clear_last_error=last_error is None,
                ),
            )
            self._db.add(job)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        self._db.refresh(report)
        return ReportRead.from_record(report)

    def get_report(self, report_id: str) -> ReportRead:
        record = self._db.get(ReportRecord, report_id)
        if record is None:
            raise ReportNotFoundError(report_id)
        return ReportRead.from_record(record)

    def _session(self, session_id: str) -> SessionRecord:
        record = self._db.get(SessionRecord, session_id)
        if record is None:
            raise ReportNotFoundError(session_id)
        return record

    def _turns(self, session_id: str) -> list[TurnRecord]:
        return list(
            self._db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_id)
                .order_by(col(TurnRecord.sequence))
            ).all()
        )
