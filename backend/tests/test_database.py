import json
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.database import (
    create_database_engine,
    create_db_and_tables,
    migrate_business_identifiers,
)
from app.reports.models import (
    ReportDraftStatus,
    ReportJobRecord,
    ReportRecord,
)
from app.sessions.models import (
    CaseType,
    Media,
    ModelMode,
    Scene,
    SessionMode,
    SessionRecord,
    SessionStatus,
)


def test_create_db_and_tables_registers_model_call_metrics(
    test_engine: Engine,
) -> None:
    create_db_and_tables(test_engine)

    assert "model_call_metrics" in inspect(test_engine).get_table_names()


def test_migration_preserves_sessions_but_discards_legacy_total_score_reports(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy.db"
    engine = create_database_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sessions ("
                "id VARCHAR PRIMARY KEY, case_version_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE reports ("
                "id VARCHAR PRIMARY KEY, rubric_version VARCHAR NOT NULL, "
                "case_version VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, case_version_id) "
                "VALUES ('session-one', 'crisis_student_main:v1')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reports (id, rubric_version, case_version) "
                "VALUES ('report-one', 'demo-rubric-v1', 'crisis_student_main:v1')"
            )
        )

    migrate_business_identifiers(engine)

    assert {column["name"] for column in inspect(engine).get_columns("sessions")} == {
        "id",
        "case_id",
    }
    assert "reports" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT case_id FROM sessions")).scalar_one() == (
            "crisis_student_main"
        )


def test_existing_metric_table_gets_nullable_client_turn_id(tmp_path) -> None:
    database_path = tmp_path / "legacy-metrics.db"
    engine = create_database_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE model_call_metrics ("
                "id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO model_call_metrics (id, session_id) "
                "VALUES ('metric-one', 'session-one')"
            )
        )

    migrate_business_identifiers(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("model_call_metrics")
    }
    assert "client_turn_id" in columns
    assert "prompt_family" in columns
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT client_turn_id FROM model_call_metrics WHERE id='metric-one'")
        ).scalar_one()
    assert value is None


def test_startup_rebuilds_only_previous_unique_report_table_and_keeps_history(
    tmp_path,
) -> None:
    database_path = tmp_path / "previous-report-schema.db"
    engine = create_database_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE reports ("
                "id VARCHAR NOT NULL PRIMARY KEY, "
                "session_id VARCHAR NOT NULL UNIQUE, "
                "job_id VARCHAR NOT NULL UNIQUE, "
                "case_id VARCHAR NOT NULL, "
                "summary_json JSON NOT NULL, dimensions_json JSON NOT NULL, "
                "bottom_line_events_json JSON NOT NULL, "
                "material_conflicts_json JSON NOT NULL, "
                "screening_gap BOOLEAN NOT NULL, disclaimers_json JSON NOT NULL, "
                "rubric_fingerprint VARCHAR NOT NULL, "
                "case_package_fingerprint VARCHAR NOT NULL, "
                "model_fingerprint VARCHAR NOT NULL, "
                "prompt_fingerprint VARCHAR NOT NULL, "
                "input_fingerprint VARCHAR NOT NULL, "
                "ai_draft_status VARCHAR(8) NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reports VALUES ("
                "'old-report', 'old-session', 'old-job', 'old-case', "
                "'{}', '[]', '[]', '[]', 0, '[]', "
                "'old-rubric', 'old-case-package', 'old-model', 'old-prompt', "
                "'old-input', 'partial', '2026-08-30 00:00:00')"
            )
        )

    create_db_and_tables(engine)

    now = datetime(2026, 8, 30, tzinfo=UTC)
    session_record = SessionRecord(
        id="session-history",
        mode=SessionMode.assessment,
        scene=Scene.hotline,
        case_type=CaseType.main,
        case_id="crisis_student_main",
        media=Media.voice,
        status=SessionStatus.ended,
        model_mode=ModelMode.live,
        ended_at=now,
    )
    job = ReportJobRecord(
        id="job-history",
        session_id=session_record.id,
        frozen_input_fingerprint="input",
        rubric_fingerprint="rubric",
        case_package_fingerprint="case",
        model_fingerprint="model",
        prompt_fingerprint="prompt",
    )
    session_id = session_record.id
    job_id = job.id

    def report(report_id: str, status: ReportDraftStatus) -> ReportRecord:
        return ReportRecord(
            id=report_id,
            session_id=session_id,
            job_id=job_id,
            case_id=session_record.case_id,
            scene=session_record.scene,
            media=session_record.media,
            summary_json={},
            dimensions_json=[],
            bottom_line_events_json=[],
            material_conflicts_json=[],
            screening_gap=False,
            disclaimers_json=[],
            rubric_fingerprint="rubric",
            case_package_fingerprint="case",
            model_fingerprint="model",
            prompt_fingerprint="prompt",
            input_fingerprint="input",
            ai_draft_status=status,
            created_at=now,
        )

    with Session(engine) as db:
        db.add_all([session_record, job])
        db.commit()
        db.add(report("report-partial", ReportDraftStatus.partial))
        db.commit()
        db.add(report("report-retry", ReportDraftStatus.complete))
        db.commit()

    unique_columns = {
        tuple(item["column_names"])
        for item in inspect(engine).get_unique_constraints("reports")
    }
    assert ("session_id",) not in unique_columns
    assert ("job_id",) not in unique_columns
    with Session(engine) as db:
        assert [item.id for item in db.exec(select(ReportRecord)).all()] == [
            "report-partial",
            "report-retry",
        ]
        assert db.get(SessionRecord, session_id) is not None
        assert db.get(ReportJobRecord, job_id) is not None

    # 已是当前结构时再启动不应丢弃报告历史。
    create_db_and_tables(engine)
    with Session(engine) as db:
        assert len(db.exec(select(ReportRecord)).all()) == 2


def test_report_schema_upgrade_preserves_rows_and_job_links_and_backfills_media(
    tmp_path,
) -> None:
    database_path = tmp_path / "report-media-upgrade.db"
    engine = create_database_engine(f"sqlite:///{database_path}")
    summary_json = json.dumps(
        {
            "scored_core_count": 0,
            "unscored": [],
            "analysis_failed": [],
            "activated_modules": [],
            "inactive_modules": [],
            "bottom_line_events": [],
            "screening_gap": False,
            "level_distribution": "原报告结论",
            "next_behaviors": [],
        },
        ensure_ascii=False,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.execute(
            text(
                "CREATE TABLE sessions ("
                "id VARCHAR PRIMARY KEY, scene VARCHAR NOT NULL, media VARCHAR NOT NULL, "
                "case_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE reports ("
                "id VARCHAR NOT NULL PRIMARY KEY, session_id VARCHAR NOT NULL UNIQUE, "
                "job_id VARCHAR NOT NULL UNIQUE, case_id VARCHAR NOT NULL, "
                "summary_json JSON NOT NULL, dimensions_json JSON NOT NULL, "
                "bottom_line_events_json JSON NOT NULL, material_conflicts_json JSON NOT NULL, "
                "screening_gap BOOLEAN NOT NULL, disclaimers_json JSON NOT NULL, "
                "rubric_fingerprint VARCHAR NOT NULL, case_package_fingerprint VARCHAR NOT NULL, "
                "model_fingerprint VARCHAR NOT NULL, prompt_fingerprint VARCHAR NOT NULL, "
                "input_fingerprint VARCHAR NOT NULL, ai_draft_status VARCHAR(8) NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE report_jobs ("
                "id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL, report_id VARCHAR, "
                "FOREIGN KEY(report_id) REFERENCES reports(id))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions VALUES "
                "('session-online', 'online', 'text', 'marriage_boundary_main')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions VALUES "
                "('session-invalid', 'hotline', 'voice', 'crisis_student_main')"
            )
        )
        for session_id in ("session-fallback", "session-mismatch", "session-other"):
            connection.execute(
                text(
                    "INSERT INTO sessions VALUES "
                    "(:session_id, 'online', 'text', 'marriage_boundary_main')"
                ),
                {"session_id": session_id},
            )
        connection.execute(
            text(
                "INSERT INTO reports VALUES ("
                "'report-online', 'session-online', 'job-online', 'marriage_boundary_main', "
                ":summary_json, '[]', '[]', '[]', 0, '[]', "
                "'rubric-old', 'case-old', 'model-old', 'prompt-old', 'input-old', "
                "'complete', '2026-08-30 00:00:00')"
            ),
            {"summary_json": summary_json},
        )
        connection.execute(
            text(
                "INSERT INTO report_jobs VALUES "
                "('job-online', 'session-online', 'report-online')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reports VALUES ("
                "'report-invalid', 'session-invalid', 'job-invalid', 'crisis_student_main', "
                "'{}', '[]', '[]', '[]', 0, '[]', "
                "'rubric-old', 'case-old', 'model-old', 'prompt-old', 'input-old', "
                "'complete', '2026-08-30 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO report_jobs VALUES "
                "('job-invalid', 'session-invalid', 'report-invalid')"
            )
        )
        report_values = (
            ":report_id, :session_id, :job_id, 'marriage_boundary_main', "
            ":summary_json, '[]', '[]', '[]', 0, '[]', "
            "'rubric-old', 'case-old', 'model-old', 'prompt-old', 'input-old', "
            "'complete', '2026-08-30 00:00:00'"
        )
        for report_id, session_id, job_id in (
            ("report-fallback", "session-fallback", "job-forward-wrong"),
            ("report-mismatch", "session-mismatch", "job-forward-wrong-2"),
            ("report-other", "session-other", "job-other"),
        ):
            connection.execute(
                text(f"INSERT INTO reports VALUES ({report_values})"),
                {
                    "report_id": report_id,
                    "session_id": session_id,
                    "job_id": job_id,
                    "summary_json": summary_json,
                },
            )
        for job_id, session_id, report_id in (
            ("job-forward-wrong", "session-other", None),
            ("job-fallback", "session-fallback", "report-fallback"),
            ("job-forward-wrong-2", "session-other", None),
            ("job-reverse-wrong", "session-other", "report-mismatch"),
            ("job-other", "session-other", "report-other"),
        ):
            connection.execute(
                text("INSERT INTO report_jobs VALUES (:job_id, :session_id, :report_id)"),
                {"job_id": job_id, "session_id": session_id, "report_id": report_id},
            )

    create_db_and_tables(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("reports")}
    assert {"scene", "media"}.issubset(columns)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT scene, media, summary_json FROM reports "
                "WHERE id='report-online'"
            )
        ).one()
        linked_report = connection.execute(
            text("SELECT report_id FROM report_jobs WHERE id='job-online'")
        ).scalar_one()
        cleared_invalid_link = connection.execute(
            text("SELECT report_id FROM report_jobs WHERE id='job-invalid'")
        ).scalar_one()
        report_rows = connection.execute(
            text("SELECT id, job_id FROM reports ORDER BY id")
        ).all()
        wrong_reverse_link = connection.execute(
            text("SELECT report_id FROM report_jobs WHERE id='job-reverse-wrong'")
        ).scalar_one()
        other_link = connection.execute(
            text("SELECT report_id FROM report_jobs WHERE id='job-other'")
        ).scalar_one()
        foreign_keys_enabled = connection.exec_driver_sql(
            "PRAGMA foreign_keys"
        ).scalar_one()
        foreign_key_violations = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).all()
    assert tuple(row[:2]) == ("online", "text")
    assert "原报告结论" in row[2]
    assert linked_report == "report-online"
    assert cleared_invalid_link is None
    assert report_rows == [
        ("report-fallback", "job-fallback"),
        ("report-online", "job-online"),
        ("report-other", "job-other"),
    ]
    assert wrong_reverse_link is None
    assert other_link == "report-other"
    assert foreign_keys_enabled == 1
    assert foreign_key_violations == []

    from app.cases.loader import CaseRepository
    from app.reports.service import ReportService

    with Session(engine) as db:
        restored = ReportService(db, CaseRepository()).get_report("report-online")
    assert restored.scene is Scene.online
    assert restored.media is Media.text
    assert restored.summary.level_distribution == "原报告结论"

    create_db_and_tables(engine)
    with Session(engine) as db:
        restored_again = ReportService(db, CaseRepository()).get_report("report-online")
    assert restored_again.scene is Scene.online
    assert restored_again.media is Media.text


def test_incompatible_total_score_report_is_removed_without_creating_fake_report(
    tmp_path,
) -> None:
    database_path = tmp_path / "legacy-total-score-report.db"
    engine = create_database_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.execute(
            text(
                "CREATE TABLE sessions ("
                "id VARCHAR PRIMARY KEY, scene VARCHAR NOT NULL, media VARCHAR NOT NULL, "
                "case_id VARCHAR NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE reports ("
                "id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL, "
                "raw_score FLOAT, normalized_score FLOAT, coverage FLOAT, "
                "result_status VARCHAR, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions VALUES "
                "('session-legacy', 'hotline', 'voice', 'crisis_student_main')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reports VALUES ("
                "'report-legacy', 'session-legacy', 18, 75, 0.8, "
                "'scored', '2026-08-29 00:00:00')"
            )
        )

    create_db_and_tables(engine)

    with engine.connect() as connection:
        report_count = connection.execute(text("SELECT count(*) FROM reports")).scalar_one()
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    assert report_count == 0
    assert violations == []
