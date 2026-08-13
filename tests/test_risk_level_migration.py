import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect, text

from app.db import build_engine
from app.risk_levels import migrate_legacy_risk_levels


class RiskLevelMigrationTests(unittest.TestCase):
    def test_legacy_levels_and_columns_migrate_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "legacy.db"
            engine = build_engine(f"sqlite:///{path.as_posix()}")
            try:
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE psychological_reports (id INTEGER PRIMARY KEY, dialogue_type VARCHAR(32), risk_level VARCHAR(16))"))
                    connection.execute(text("CREATE TABLE agent_runs (id INTEGER PRIMARY KEY, route VARCHAR(32), final_risk_level VARCHAR(16))"))
                    connection.execute(text("CREATE TABLE risk_cases (id INTEGER PRIMARY KEY, risk_level VARCHAR(16))"))
                    connection.execute(text("INSERT INTO psychological_reports VALUES (1, 'psychological_support', 'low'), (2, 'psychological_support', 'medium'), (3, 'safety_risk', 'high')"))
                    connection.execute(text("INSERT INTO agent_runs VALUES (1, 'general', NULL), (2, 'psychological_support', 'medium'), (3, 'safety_risk', 'high')"))
                    connection.execute(text("INSERT INTO risk_cases VALUES (1, 'medium'), (2, 'high')"))

                migrate_legacy_risk_levels(engine)
                migrate_legacy_risk_levels(engine)

                with engine.connect() as connection:
                    report_columns = {item["name"] for item in inspect(connection).get_columns("psychological_reports")}
                    run_columns = {item["name"] for item in inspect(connection).get_columns("agent_runs")}
                    reports = connection.execute(text("SELECT risk_level FROM psychological_reports ORDER BY id")).scalars().all()
                    runs = connection.execute(text("SELECT risk_level FROM agent_runs ORDER BY id")).scalars().all()
                    cases = connection.execute(text("SELECT risk_level FROM risk_cases ORDER BY id")).scalars().all()

                self.assertNotIn("dialogue_type", report_columns)
                self.assertNotIn("route", run_columns)
                self.assertNotIn("final_risk_level", run_columns)
                self.assertEqual(reports, ["CONSULT", "CONSULT", "RISK"])
                self.assertEqual(runs, ["CHAT", "CONSULT", "RISK"])
                self.assertEqual(cases, ["CONSULT", "RISK"])
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
