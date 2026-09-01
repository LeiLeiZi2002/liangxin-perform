import json
from collections.abc import Iterator, Mapping
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine, RowMapping, make_url
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.reports.models import (  # noqa: F401
    ReportDraftStatus,
    ReportJobRecord,
    ReportRecord,
)
from app.reports.scoring_domain import (
    BottomLineEvent,
    DimensionResult,
    MaterialConflict,
    ResultSummary,
)
from app.runtime.models import ModelCallMetricRecord, RuntimeFailureRecord  # noqa: F401


def _prepare_sqlite_directory(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return
    if url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_database_engine(database_url: str) -> Engine:
    connect_args: dict[str, bool] = {}
    if make_url(database_url).get_backend_name() == "sqlite":
        _prepare_sqlite_directory(database_url)
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


engine = create_database_engine(get_settings().database_url)


_REPORT_TABLE_DDL = """
CREATE TABLE reports_migrating (
    id VARCHAR NOT NULL PRIMARY KEY,
    session_id VARCHAR NOT NULL,
    job_id VARCHAR NOT NULL,
    case_id VARCHAR NOT NULL,
    scene VARCHAR NOT NULL,
    media VARCHAR NOT NULL,
    summary_json JSON NOT NULL,
    dimensions_json JSON NOT NULL,
    bottom_line_events_json JSON NOT NULL,
    material_conflicts_json JSON NOT NULL,
    screening_gap BOOLEAN NOT NULL,
    disclaimers_json JSON NOT NULL,
    rubric_fingerprint VARCHAR NOT NULL,
    case_package_fingerprint VARCHAR NOT NULL,
    model_fingerprint VARCHAR NOT NULL,
    prompt_fingerprint VARCHAR NOT NULL,
    input_fingerprint VARCHAR NOT NULL,
    ai_draft_status VARCHAR(8) NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions (id),
    FOREIGN KEY(job_id) REFERENCES report_jobs (id)
)
"""

_MIGRATABLE_REPORT_CONTENT_COLUMNS = {
    "summary_json",
    "dimensions_json",
    "bottom_line_events_json",
    "material_conflicts_json",
    "screening_gap",
    "disclaimers_json",
    "rubric_fingerprint",
    "case_package_fingerprint",
    "model_fingerprint",
    "prompt_fingerprint",
    "input_fingerprint",
    "ai_draft_status",
    "created_at",
}


def _decoded_json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _has_readable_report_content(
    row: Mapping[str, object] | RowMapping,
) -> bool:
    try:
        required_text = (
            "rubric_fingerprint",
            "case_package_fingerprint",
            "model_fingerprint",
            "prompt_fingerprint",
            "input_fingerprint",
        )
        if any(not str(row[field] or "").strip() for field in required_text):
            return False
        if row["created_at"] is None:
            return False
        ResultSummary.model_validate(_decoded_json(row["summary_json"]))
        dimensions = _decoded_json(row["dimensions_json"])
        bottom_line_events = _decoded_json(row["bottom_line_events_json"])
        conflicts = _decoded_json(row["material_conflicts_json"])
        disclaimers = _decoded_json(row["disclaimers_json"])
        if not isinstance(dimensions, list):
            return False
        if not isinstance(bottom_line_events, list):
            return False
        if not isinstance(conflicts, list):
            return False
        if not isinstance(disclaimers, list):
            return False
        for item in dimensions:
            DimensionResult.model_validate(item)
        for item in bottom_line_events:
            BottomLineEvent.model_validate(item)
        for item in conflicts:
            MaterialConflict.model_validate(item)
        if any(not isinstance(item, str) for item in disclaimers):
            return False
        ReportDraftStatus(str(row["ai_draft_status"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _clear_report_job_links(
    connection: Connection,
    table_names: set[str],
) -> None:
    if "report_jobs" not in table_names:
        return
    job_columns = {
        column["name"] for column in inspect(connection).get_columns("report_jobs")
    }
    if "report_id" in job_columns:
        connection.execute(text("UPDATE report_jobs SET report_id = NULL"))


def _rebuild_reports_preserving_rows(
    connection: Connection,
    report_columns: set[str],
    table_names: set[str],
) -> None:
    """把仍可关联到会话的旧报告搬入当前结构，并补齐真实场域。"""
    required_columns = {
        "id",
        "session_id",
        *_MIGRATABLE_REPORT_CONTENT_COLUMNS,
    }
    if not required_columns.issubset(report_columns) or "sessions" not in table_names:
        _clear_report_job_links(connection, table_names)
        connection.execute(text("DROP TABLE reports"))
        return

    session_columns = {
        column["name"] for column in inspect(connection).get_columns("sessions")
    }
    if not {"id", "scene", "media", "case_id"}.issubset(session_columns):
        _clear_report_job_links(connection, table_names)
        connection.execute(text("DROP TABLE reports"))
        return

    def old_or(column: str, fallback: str) -> str:
        return f"r.{column}" if column in report_columns else fallback

    if "report_jobs" not in table_names:
        connection.execute(text("DROP TABLE reports"))
        return
    job_columns = {
        column["name"] for column in inspect(connection).get_columns("report_jobs")
    }
    if not {"id", "session_id"}.issubset(job_columns):
        _clear_report_job_links(connection, table_names)
        connection.execute(text("DROP TABLE reports"))
        return
    job_candidates: list[str] = []
    if "job_id" in report_columns:
        job_candidates.append(
            "(SELECT j.id FROM report_jobs AS j "
            "WHERE j.id = r.job_id AND j.session_id = r.session_id LIMIT 1)"
        )
    if "report_id" in job_columns:
        job_candidates.append(
            "(SELECT j.id FROM report_jobs AS j "
            "WHERE j.report_id = r.id AND j.session_id = r.session_id LIMIT 1)"
        )
    if not job_candidates:
        _clear_report_job_links(connection, table_names)
        connection.execute(text("DROP TABLE reports"))
        return
    job_id_expression = (
        job_candidates[0]
        if len(job_candidates) == 1
        else "COALESCE(" + ", ".join(job_candidates) + ")"
    )

    candidate_rows = connection.execute(
        text(
            "SELECT r.*, "
            + job_id_expression
            + " AS resolved_job_id FROM reports AS r "
            "JOIN sessions AS s ON s.id = r.session_id"
        )
    ).mappings()
    valid_report_ids = [
        str(row["id"])
        for row in candidate_rows
        if row["resolved_job_id"] is not None and _has_readable_report_content(row)
    ]

    values = {
        "id": "r.id",
        "session_id": "r.session_id",
        "job_id": job_id_expression,
        "case_id": "s.case_id",
        "scene": "s.scene",
        "media": "s.media",
        "summary_json": old_or("summary_json", "'{}'"),
        "dimensions_json": old_or("dimensions_json", "'[]'"),
        "bottom_line_events_json": old_or("bottom_line_events_json", "'[]'"),
        "material_conflicts_json": old_or("material_conflicts_json", "'[]'"),
        "screening_gap": old_or("screening_gap", "0"),
        "disclaimers_json": old_or("disclaimers_json", "'[]'"),
        "rubric_fingerprint": old_or("rubric_fingerprint", "'legacy'"),
        "case_package_fingerprint": old_or("case_package_fingerprint", "'legacy'"),
        "model_fingerprint": old_or("model_fingerprint", "'legacy'"),
        "prompt_fingerprint": old_or("prompt_fingerprint", "'legacy'"),
        "input_fingerprint": old_or("input_fingerprint", "'legacy'"),
        "ai_draft_status": old_or("ai_draft_status", "'partial'"),
        "created_at": old_or("created_at", "CURRENT_TIMESTAMP"),
    }
    columns = list(values)
    connection.execute(text("DROP TABLE IF EXISTS reports_migrating"))
    connection.execute(text(_REPORT_TABLE_DDL))
    insert_statement = text(
        "INSERT INTO reports_migrating ("
        + ", ".join(columns)
        + ") SELECT "
        + ", ".join(values[column] for column in columns)
        + " FROM reports AS r JOIN sessions AS s ON s.id = r.session_id "
        "WHERE r.id = :report_id"
    )
    for report_id in valid_report_ids:
        connection.execute(insert_statement, {"report_id": report_id})
    connection.execute(text("DROP TABLE reports"))
    connection.execute(text("ALTER TABLE reports_migrating RENAME TO reports"))
    if "report_id" in job_columns:
        connection.execute(
            text(
                "UPDATE report_jobs SET report_id = NULL "
                "WHERE report_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM reports WHERE reports.id = report_jobs.report_id)"
            )
        )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_reports_session_id ON reports (session_id)")
    )
    connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_reports_job_id ON reports (job_id)")
    )


def begin_sqlite_immediate(db: Session) -> None:
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _migrate_sqlite_schema(connection: Connection) -> None:
    table_names = set(inspect(connection).get_table_names())
    if "sessions" in table_names:
        session_columns = {
            column["name"] for column in inspect(connection).get_columns("sessions")
        }
        if "case_version_id" in session_columns and "case_id" not in session_columns:
            connection.execute(
                text("ALTER TABLE sessions RENAME COLUMN case_version_id TO case_id")
            )
        connection.execute(
            text(
                "UPDATE sessions SET case_id = "
                "substr(case_id, 1, instr(case_id, char(58) || 'v') - 1) "
                "WHERE instr(case_id, char(58) || 'v') > 0"
            )
        )

    if "reports" in table_names:
        report_inspector = inspect(connection)
        report_columns = {
            column["name"] for column in inspect(connection).get_columns("reports")
        }
        expected_report_columns = set(ReportRecord.model_fields)
        unique_report_columns = {
            tuple(
                column
                for column in constraint.get("column_names") or ()
                if column is not None
            )
            for constraint in report_inspector.get_unique_constraints("reports")
        }
        unique_report_columns.update(
            tuple(
                column
                for column in index.get("column_names") or ()
                if column is not None
            )
            for index in report_inspector.get_indexes("reports")
            if index.get("unique")
        )
        has_obsolete_report_uniqueness = bool(
            {("session_id",), ("job_id",)} & unique_report_columns
        )
        if report_columns != expected_report_columns or has_obsolete_report_uniqueness:
            _rebuild_reports_preserving_rows(
                connection,
                report_columns,
                table_names,
            )

    if "model_call_metrics" in table_names:
        metric_columns = {
            column["name"]
            for column in inspect(connection).get_columns("model_call_metrics")
        }
        if "client_turn_id" not in metric_columns:
            connection.execute(
                text(
                    "ALTER TABLE model_call_metrics "
                    "ADD COLUMN client_turn_id VARCHAR"
                )
            )
        if "prompt_family" not in metric_columns:
            connection.execute(
                text(
                    "ALTER TABLE model_call_metrics "
                    "ADD COLUMN prompt_family VARCHAR"
                )
            )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_model_call_metrics_client_turn_id "
                "ON model_call_metrics (client_turn_id)"
            )
        )


def migrate_business_identifiers(database_engine: Engine) -> None:
    if database_engine.dialect.name != "sqlite":
        return

    # SQLite 不允许在外键检查开启时替换仍被 report_jobs 引用的 reports 表。
    # 在同一底层连接上暂时关闭检查，完成事务后立即恢复，并在提交前主动核验。
    with database_engine.connect() as connection:
        foreign_keys_enabled = bool(
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        )
        connection.commit()
        if foreign_keys_enabled:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
        try:
            with connection.begin():
                _migrate_sqlite_schema(connection)
                if foreign_keys_enabled:
                    violation = connection.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).first()
                    if violation is not None:
                        raise RuntimeError(f"SQLite 外键校验失败：{tuple(violation)}")
        finally:
            if foreign_keys_enabled:
                if connection.in_transaction():
                    connection.rollback()
                connection.exec_driver_sql("PRAGMA foreign_keys = ON")
                connection.commit()


def create_db_and_tables(database_engine: Engine | None = None) -> None:
    selected_engine = database_engine or engine
    migrate_business_identifiers(selected_engine)
    SQLModel.metadata.create_all(selected_engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
