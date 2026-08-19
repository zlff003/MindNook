from __future__ import annotations

from sqlalchemy import inspect, text


def _columns(connection, table: str) -> set[str]:
    return {item["name"] for item in inspect(connection).get_columns(table)}


def migrate_account_schema(engine) -> None:
    """Idempotently migrate account names and add risk-case ownership."""
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        dialect = connection.dialect.name

        if "users" in tables:
            columns = _columns(connection, "users")
            if "institutional_id" not in columns and "username" in columns:
                if dialect == "mysql":
                    connection.execute(
                        text(
                            "ALTER TABLE users CHANGE COLUMN username "
                            "institutional_id VARCHAR(128) NOT NULL"
                        )
                    )
                else:
                    connection.execute(
                        text("ALTER TABLE users RENAME COLUMN username TO institutional_id")
                    )
                columns.discard("username")
                columns.add("institutional_id")
            if "name" not in columns and "display_name" in columns:
                if dialect == "mysql":
                    connection.execute(
                        text(
                            "ALTER TABLE users CHANGE COLUMN display_name "
                            "name VARCHAR(128) NOT NULL"
                        )
                    )
                else:
                    connection.execute(
                        text("ALTER TABLE users RENAME COLUMN display_name TO name")
                    )

        if "risk_cases" in tables:
            columns = _columns(connection, "risk_cases")
            if "owner_id" not in columns:
                connection.execute(
                    text("ALTER TABLE risk_cases ADD COLUMN owner_id INTEGER NULL")
                )
            indexes = {
                item["name"] for item in inspect(connection).get_indexes("risk_cases")
            }
            if "ix_risk_cases_owner_id" not in indexes:
                connection.execute(
                    text("CREATE INDEX ix_risk_cases_owner_id ON risk_cases (owner_id)")
                )
