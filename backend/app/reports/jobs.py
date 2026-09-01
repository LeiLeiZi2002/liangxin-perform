import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.cases.loader import CaseRepository
from app.database import begin_sqlite_immediate
from app.reports.competency_rubric import iter_rubrics
from app.reports.job_inputs import (
    CodingInput,
    CodingSessionInput,
    CodingTechnicalInterruption,
    CodingTurnInput,
    OpportunityCheckInput,
    OpportunityTurnStateInput,
    SessionTerminationInput,
    WorkRecordSnapshotInput,
)
from app.reports.models import (
    PlannedAction,
    ReportJobRecord,
    ReportJobStage,
    WorkRecordRecord,
)
from app.reports.report_provider import REPORT_PROMPT_BUNDLE
from app.reports.schemas import ReportJobRead
from app.runtime.models import RuntimeFailureRecord
from app.runtime_config import (
    DEFAULT_REPORT_MODEL,
    DEFAULT_REPORT_TEMPERATURE,
    RuntimeCredentialStore,
    runtime_credential_store,
)
from app.sessions.models import SessionMode, SessionRecord, SessionStatus, TurnRecord, utc_now

REPORT_MODEL_SNAPSHOT: dict[str, Any] = {
    "report_model": DEFAULT_REPORT_MODEL,
    "sampling_parameters": {"temperature": DEFAULT_REPORT_TEMPERATURE},
}
INTERRUPTED_JOB_ERROR = "应用重启时报告任务仍在执行，已收口为可重试状态。"
UNCONFIGURED_PROCESSOR_ERROR = "报告处理器尚未配置，任务未执行。"


class ReportJobNotFoundError(LookupError):
    pass


class ReportJobConflictError(RuntimeError):
    pass


class ReportJobProcessor(Protocol):
    def process(self, job_id: str) -> None: ...


class UnconfiguredReportJobProcessor:
    """未配置真实 worker 时，将任务明确收口为可重试失败。"""

    def __init__(self, engine_provider: Callable[[], Engine]) -> None:
        self._engine_provider = engine_provider

    def process(self, job_id: str) -> None:
        with Session(self._engine_provider()) as db:
            job = db.get(ReportJobRecord, job_id)
            if job is None or job.stage is not ReportJobStage.queued:
                return
            job.stage = ReportJobStage.failed
            job.last_error = UNCONFIGURED_PROCESSOR_ERROR
            job.updated_at = utc_now()
            db.add(job)
            db.commit()


@dataclass(frozen=True, slots=True)
class ReportJobCreation:
    job: ReportJobRead
    should_process: bool


@dataclass(frozen=True, slots=True)
class ReportJobProgressUpdate:
    stage: ReportJobStage | None = None
    coding_json: dict[str, Any] | None = None
    scoring_group_id: str | None = None
    scoring_group_result: dict[str, Any] | None = None
    completed_group: str | None = None
    incomplete_group: str | None = None
    attempt_key: str | None = None
    report_id: str | None = None
    last_error: str | None = None
    clear_last_error: bool = False


def apply_report_job_progress_update(
    job: ReportJobRecord,
    update: ReportJobProgressUpdate,
) -> None:
    if (update.scoring_group_id is None) is not (
        update.scoring_group_result is None
    ):
        raise ValueError("定级组标识与结果必须同时提供。")
    if update.completed_group is not None and update.incomplete_group is not None:
        raise ValueError("同一次更新不能同时完成和取消定级组。")
    if update.stage is not None:
        job.stage = update.stage
    if update.coding_json is not None:
        job.coding_json = deepcopy(update.coding_json)
    if update.scoring_group_id is not None:
        group_results = deepcopy(job.scoring_group_results_json)
        group_results[update.scoring_group_id] = deepcopy(
            update.scoring_group_result
        )
        job.scoring_group_results_json = group_results
    if update.completed_group is not None:
        completed_groups = list(job.scoring_groups_done)
        if update.completed_group not in completed_groups:
            completed_groups.append(update.completed_group)
        job.scoring_groups_done = completed_groups
    if update.incomplete_group is not None:
        job.scoring_groups_done = [
            group
            for group in job.scoring_groups_done
            if group != update.incomplete_group
        ]
    if update.attempt_key is not None:
        attempts = dict(job.attempts)
        attempts[update.attempt_key] = attempts.get(update.attempt_key, 0) + 1
        job.attempts = attempts
    if update.report_id is not None:
        job.report_id = update.report_id
    if update.clear_last_error:
        job.last_error = None
    elif update.last_error is not None:
        job.last_error = update.last_error
    job.updated_at = utc_now()


def canonical_fingerprint(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _rubric_snapshot() -> dict[str, Any]:
    return {
        "rubric_id": "competency_rubric",
        "rubrics": [
            {
                "target": target.value,
                **rubric.model_dump(mode="json", exclude={"id"}),
            }
            for target, rubric in sorted(iter_rubrics(), key=lambda item: item[0].value)
        ],
    }


class ReportJobService:
    def __init__(
        self,
        db: Session,
        cases: CaseRepository,
        credential_store: RuntimeCredentialStore = runtime_credential_store,
    ) -> None:
        self._db = db
        self._cases = cases
        self._credential_store = credential_store

    def create(self, session_id: str) -> ReportJobCreation:
        begin_sqlite_immediate(self._db)
        session_record = self._session(session_id)
        existing = self._by_session(session_id)
        if existing is not None:
            return ReportJobCreation(
                job=ReportJobRead.from_record(existing),
                should_process=False,
            )
        if session_record.status is not SessionStatus.ended:
            raise ReportJobConflictError("会话结束后才能生成测评报告。")

        work_record = self._db.exec(
            select(WorkRecordRecord).where(WorkRecordRecord.session_id == session_id)
        ).first()
        if session_record.mode is SessionMode.assessment and work_record is None:
            raise ReportJobConflictError("正式测评生成报告前必须提交工作记录。")

        case_package = self._cases.get(session_record.case_id)
        case_snapshot = case_package.model_dump(mode="json")
        coding_input, opportunity_check_input = self._freeze_inputs(
            session_record,
            work_record,
            case_snapshot=case_snapshot,
        )
        coding_input_json = coding_input.model_dump(mode="json")
        opportunity_check_json = opportunity_check_input.model_dump(mode="json")
        credentials = self._credential_store.credentials()
        model_snapshot = {
            "report_model": credentials.report_model,
            "sampling_parameters": {"temperature": credentials.report_temperature},
        }
        job = ReportJobRecord(
            session_id=session_id,
            frozen_input_json=coding_input_json,
            opportunity_check_json=opportunity_check_json,
            frozen_input_fingerprint=canonical_fingerprint(
                {
                    "coding_input": coding_input_json,
                    "opportunity_check_input": opportunity_check_json,
                }
            ),
            rubric_fingerprint=canonical_fingerprint(_rubric_snapshot()),
            case_package_fingerprint=canonical_fingerprint(case_snapshot),
            model_snapshot=model_snapshot,
            model_fingerprint=canonical_fingerprint(model_snapshot),
            prompt_fingerprint=canonical_fingerprint(REPORT_PROMPT_BUNDLE),
        )
        self._db.add(job)
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            concurrent = self._by_session(session_id)
            if concurrent is None:
                raise
            return ReportJobCreation(
                job=ReportJobRead.from_record(concurrent),
                should_process=False,
            )
        self._db.refresh(job)
        return ReportJobCreation(job=ReportJobRead.from_record(job), should_process=True)

    def get(self, job_id: str) -> ReportJobRead:
        return ReportJobRead.from_record(self._job(job_id))

    def get_coding_input(self, job_id: str) -> CodingInput:
        return CodingInput.model_validate(self._job(job_id).frozen_input_json)

    def get_opportunity_check_input(self, job_id: str) -> OpportunityCheckInput:
        return OpportunityCheckInput.model_validate(
            self._job(job_id).opportunity_check_json
        )

    def claim_for_processing(self, job_id: str) -> bool:
        """短事务领取 queued 任务；SQLite 在读取前即取得写锁。"""

        begin_sqlite_immediate(self._db)
        job = self._db.get(ReportJobRecord, job_id)
        if job is None or job.stage is not ReportJobStage.queued:
            self._db.rollback()
            return False
        apply_report_job_progress_update(
            job,
            ReportJobProgressUpdate(stage=ReportJobStage.coding),
        )
        self._db.add(job)
        self._db.commit()
        return True

    def update_progress(
        self,
        job_id: str,
        update: ReportJobProgressUpdate,
    ) -> ReportJobRead:
        begin_sqlite_immediate(self._db)
        job = self._job(job_id)
        apply_report_job_progress_update(job, update)
        self._db.add(job)
        self._db.commit()
        self._db.refresh(job)
        return ReportJobRead.from_record(job)

    def retry(self, job_id: str) -> ReportJobCreation:
        begin_sqlite_immediate(self._db)
        job = self._job(job_id)
        if job.stage not in {ReportJobStage.failed, ReportJobStage.partial}:
            raise ReportJobConflictError("当前报告任务状态不可重试。")
        apply_report_job_progress_update(
            job,
            ReportJobProgressUpdate(
                stage=ReportJobStage.queued,
                attempt_key="manual_retry",
                clear_last_error=True,
            ),
        )
        self._db.add(job)
        self._db.commit()
        self._db.refresh(job)
        return ReportJobCreation(job=ReportJobRead.from_record(job), should_process=True)

    def _session(self, session_id: str) -> SessionRecord:
        record = self._db.get(SessionRecord, session_id)
        if record is None:
            raise ReportJobNotFoundError(session_id)
        return record

    def _job(self, job_id: str) -> ReportJobRecord:
        record = self._db.get(ReportJobRecord, job_id)
        if record is None:
            raise ReportJobNotFoundError(job_id)
        return record

    def _by_session(self, session_id: str) -> ReportJobRecord | None:
        return self._db.exec(
            select(ReportJobRecord).where(ReportJobRecord.session_id == session_id)
        ).first()

    def _freeze_inputs(
        self,
        session_record: SessionRecord,
        work_record: WorkRecordRecord | None,
        *,
        case_snapshot: dict[str, Any],
    ) -> tuple[CodingInput, OpportunityCheckInput]:
        turns = list(
            self._db.exec(
                select(TurnRecord)
                .where(TurnRecord.session_id == session_record.id)
                .order_by(col(TurnRecord.sequence))
            ).all()
        )
        interruptions = list(
            self._db.exec(
                select(RuntimeFailureRecord)
                .where(RuntimeFailureRecord.session_id == session_record.id)
                .where(RuntimeFailureRecord.disposition != "recovered")
                .order_by(
                    col(RuntimeFailureRecord.created_at),
                    col(RuntimeFailureRecord.id),
                )
            ).all()
        )
        coding_input = CodingInput(
            session=CodingSessionInput(
                session_id=session_record.id,
                mode=session_record.mode,
                scene=session_record.scene,
                case_type=session_record.case_type,
                case_id=session_record.case_id,
                media=session_record.media,
                status=session_record.status,
                model_mode=session_record.model_mode,
                soft_duration_minutes=session_record.soft_duration_minutes,
                created_at=session_record.created_at,
                ended_at=session_record.ended_at,
                end_reason=session_record.end_reason,
            ),
            turns=[
                CodingTurnInput(
                    turn_id=turn.id,
                    sequence=turn.sequence,
                    speaker=turn.speaker,
                    text=turn.text,
                    created_at=turn.created_at,
                )
                for turn in turns
            ],
            work_record=(
                WorkRecordSnapshotInput(
                    id=work_record.id,
                    session_id=work_record.session_id,
                    problem_understanding=work_record.problem_understanding,
                    risk_level=work_record.risk_level,
                    risk_reasoning=work_record.risk_reasoning,
                    risk_evidence_turn_ids=list(work_record.risk_evidence_turn_ids),
                    missing_information=list(work_record.missing_information),
                    planned_actions=[
                        PlannedAction(value) for value in work_record.planned_actions
                    ],
                    referral_decision=work_record.referral_decision,
                    supervision_decision=work_record.supervision_decision,
                    follow_up=work_record.follow_up,
                    limitations=work_record.limitations,
                    created_at=work_record.created_at,
                    updated_at=work_record.updated_at,
                )
                if work_record is not None
                else None
            ),
            technical_interruptions=[
                CodingTechnicalInterruption(
                    interruption_id=interruption.id,
                    client_turn_id=interruption.client_turn_id,
                    component=interruption.component,
                    phase=interruption.phase,
                    operation=interruption.operation,
                    failure_code=interruption.failure_code,
                    attempt_count=interruption.attempt_count,
                    retryable=interruption.retryable,
                    disposition=interruption.disposition,
                    created_at=interruption.created_at,
                )
                for interruption in interruptions
            ],
            termination=SessionTerminationInput(
                status=session_record.status,
                ended_at=session_record.ended_at,
                end_reason=session_record.end_reason,
            ),
        )
        opportunity_check_input = OpportunityCheckInput(
            session_id=session_record.id,
            session_state=session_record.state_json,
            turn_states=[
                OpportunityTurnStateInput(
                    turn_id=turn.id,
                    state_before_json=turn.state_before_json,
                    state_after_json=turn.state_after_json,
                    signals_json=turn.signals_json,
                    used_fact_ids=turn.used_fact_ids,
                )
                for turn in turns
            ],
            case_package=case_snapshot,
        )
        return coding_input, opportunity_check_input


def finalize_interrupted_report_jobs(db: Session) -> int:
    interrupted = list(
        db.exec(
            select(ReportJobRecord).where(
                col(ReportJobRecord.stage).in_(
                    [
                        ReportJobStage.queued,
                        ReportJobStage.coding,
                        ReportJobStage.scoring,
                        ReportJobStage.assembling,
                    ]
                )
            )
        ).all()
    )
    if not interrupted:
        return 0
    now = utc_now()
    for job in interrupted:
        apply_report_job_progress_update(
            job,
            ReportJobProgressUpdate(
                stage=ReportJobStage.failed,
                last_error=INTERRUPTED_JOB_ERROR,
            ),
        )
        job.updated_at = now
        db.add(job)
    db.commit()
    return len(interrupted)
