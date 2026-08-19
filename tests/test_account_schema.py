import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.account_schema import migrate_account_schema


class AccountSchemaMigrationTests(unittest.TestCase):
    def test_legacy_account_columns_and_case_owner_migrate_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.db"
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE users ("
                        "id INTEGER PRIMARY KEY, username VARCHAR(128) NOT NULL UNIQUE, "
                        "display_name VARCHAR(128) NOT NULL)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO users (id, username, display_name) "
                        "VALUES (1, '20260001', '张同学')"
                    )
                )
                connection.execute(
                    text("CREATE TABLE risk_cases (id INTEGER PRIMARY KEY)")
                )

            migrate_account_schema(engine)
            migrate_account_schema(engine)

            with engine.connect() as connection:
                user_columns = {
                    item["name"] for item in inspect(connection).get_columns("users")
                }
                case_columns = {
                    item["name"]
                    for item in inspect(connection).get_columns("risk_cases")
                }
                row = connection.execute(
                    text("SELECT institutional_id, name FROM users")
                ).one()
                self.assertEqual(row, ("20260001", "张同学"))
                self.assertIn("institutional_id", user_columns)
                self.assertIn("name", user_columns)
                self.assertNotIn("username", user_columns)
                self.assertIn("owner_id", case_columns)

            engine.dispose()


if __name__ == "__main__":
    unittest.main()
