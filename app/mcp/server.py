from __future__ import annotations

from functools import lru_cache

from mcp.server.fastmcp import FastMCP

from app.core.config import get_settings
from app.db import Base, build_engine, build_session_factory
from app.tools.operations import ToolOperations


mcp = FastMCP(
    "MindBridge Tools",
    instructions=(
        "Durable MindBridge operations for report ledgers, risk cases and alerts. "
        "All identifiers are MindBridge public IDs except alert_id."
    ),
    json_response=True,
)


@lru_cache
def _operations() -> ToolOperations:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    return ToolOperations(build_session_factory(engine), settings)


@mcp.tool()
def mindbridge_excel_report(report_id: str) -> dict:
    """Idempotently append one psychological report to the Excel risk ledger."""
    return _operations().excel_report(report_id)


@mcp.tool()
def mindbridge_case_create(report_id: str) -> dict:
    """Idempotently create the risk case associated with a report."""
    return _operations().case_create(report_id)


@mcp.tool()
def mindbridge_alert_send(report_id: str) -> dict:
    """Send the high-risk alert for a report after its case exists."""
    return _operations().alert_send(report_id)


@mcp.tool()
def mindbridge_alert_ack(
    alert_id: int,
    actor_username: str,
    note: str = "",
) -> dict:
    """Acknowledge an alert and append an auditable alert event."""
    return _operations().alert_ack(
        alert_id,
        actor_username=actor_username,
        note=note,
    )


@mcp.tool()
def mindbridge_case_note_add(
    case_id: str,
    note: str,
    actor_username: str = "mcp",
) -> dict:
    """Append a note to a risk case and record the case event and audit log."""
    return _operations().case_note_add(
        case_id,
        note,
        actor_username=actor_username,
    )


@mcp.tool()
def mindbridge_alert_notify(alert_id: int) -> dict:
    """Re-send an existing alert through the configured notification channel."""
    return _operations().alert_notify(alert_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
