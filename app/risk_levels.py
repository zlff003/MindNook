from __future__ import annotations

from sqlalchemy import inspect, text


CHAT = "CHAT"
CONSULT = "CONSULT"
RISK = "RISK"
RISK_LEVELS = (CHAT, CONSULT, RISK)


def _columns(connection, table: str) -> set[str]:
    return {item["name"] for item in inspect(connection).get_columns(table)}


def _drop_index_if_present(connection, table: str, column: str) -> None:
    for index in inspect(connection).get_indexes(table):
        if column in (index.get("column_names") or ()):
            connection.execute(text(f'DROP INDEX "{index["name"]}"'))


def migrate_legacy_risk_levels(engine) -> None:
    """Idempotently migrate low/medium/high plus legacy routes to three levels."""
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        dialect = connection.dialect.name

        if "psychological_reports" in tables:
            report_columns = _columns(connection, "psychological_reports")
            if "handoff_summary" not in report_columns:
                connection.execute(
                    text("ALTER TABLE psychological_reports ADD COLUMN handoff_summary TEXT")
                )
                report_columns.add("handoff_summary")
            dialogue_expression = (
                "dialogue_type" if "dialogue_type" in report_columns else "NULL"
            )
            connection.execute(
                text(
                    f"""
                    UPDATE psychological_reports
                    SET risk_level = CASE
                        WHEN UPPER(COALESCE(risk_level, '')) IN ('HIGH', 'RISK') THEN 'RISK'
                        WHEN UPPER(COALESCE(risk_level, '')) IN ('LOW', 'MEDIUM', 'CONSULT') THEN 'CONSULT'
                        WHEN LOWER(COALESCE({dialogue_expression}, '')) = 'safety_risk' THEN 'RISK'
                        ELSE 'CHAT'
                    END
                    """
                )
            )
            if "dialogue_type" in report_columns:
                if dialect == "mysql":
                    connection.execute(text("ALTER TABLE psychological_reports DROP COLUMN dialogue_type"))
                elif dialect == "sqlite":
                    connection.execute(text("ALTER TABLE psychological_reports DROP COLUMN dialogue_type"))

        if "agent_runs" in tables:
            columns = _columns(connection, "agent_runs")
            if "risk_level" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN risk_level VARCHAR(16)"))
                columns.add("risk_level")
            final_expression = (
                "final_risk_level" if "final_risk_level" in columns else "NULL"
            )
            route_expression = "route" if "route" in columns else "NULL"
            connection.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET risk_level = CASE
                        WHEN UPPER(COALESCE(risk_level, '')) = 'RISK'
                          OR UPPER(COALESCE({final_expression}, '')) IN ('HIGH', 'RISK')
                          OR LOWER(COALESCE({route_expression}, '')) = 'safety_risk' THEN 'RISK'
                        WHEN UPPER(COALESCE(risk_level, '')) = 'CONSULT'
                          OR UPPER(COALESCE({final_expression}, '')) IN ('LOW', 'MEDIUM', 'CONSULT')
                          OR LOWER(COALESCE({route_expression}, '')) = 'psychological_support' THEN 'CONSULT'
                        ELSE 'CHAT'
                    END
                    """
                )
            )
            for legacy_column in ("route", "final_risk_level"):
                if legacy_column not in columns:
                    continue
                if dialect == "sqlite":
                    _drop_index_if_present(connection, "agent_runs", legacy_column)
                connection.execute(text(f"ALTER TABLE agent_runs DROP COLUMN {legacy_column}"))
            if dialect == "sqlite":
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_agent_runs_risk_level "
                        "ON agent_runs (risk_level)"
                    )
                )
            elif dialect == "mysql":
                indexes = {item["name"] for item in inspect(connection).get_indexes("agent_runs")}
                if "ix_agent_runs_risk_level" not in indexes:
                    connection.execute(
                        text("CREATE INDEX ix_agent_runs_risk_level ON agent_runs (risk_level)")
                    )

        if "risk_cases" in tables:
            connection.execute(
                text(
                    """
                    UPDATE risk_cases
                    SET risk_level = CASE
                        WHEN UPPER(COALESCE(risk_level, '')) IN ('HIGH', 'RISK') THEN 'RISK'
                        WHEN UPPER(COALESCE(risk_level, '')) IN ('LOW', 'MEDIUM', 'CONSULT') THEN 'CONSULT'
                        ELSE 'CHAT'
                    END
                    """
                )
            )
