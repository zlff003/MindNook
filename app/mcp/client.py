from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _tool_result(result) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        try:
            return json.loads(content[0].text)
        except json.JSONDecodeError:
            return content[0].text
    return None


async def run_report_tools(
    report_id: str,
    risk_level: str,
    *,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Start the stdio server and invoke the report tool chain in dependency order."""
    normalized_risk = risk_level.strip().upper()
    if normalized_risk not in {"CHAT", "CONSULT", "RISK"}:
        raise ValueError("risk_level must be CHAT, CONSULT, or RISK")
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.server"],
        cwd=PROJECT_ROOT,
        env=env or dict(os.environ),
    )
    calls = []
    if normalized_risk in {"CONSULT", "RISK"}:
        calls.append(("mindbridge_excel_report", {"report_id": report_id}))
    if normalized_risk == "RISK":
        calls.append(("mindbridge_case_create", {"report_id": report_id}))
        calls.append(("mindbridge_alert_send", {"report_id": report_id}))

    outputs: list[dict[str, Any]] = []
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for tool_name, arguments in calls:
                result = await session.call_tool(tool_name, arguments=arguments)
                if getattr(result, "isError", False):
                    raise RuntimeError(f"MCP tool failed: {tool_name}: {_tool_result(result)}")
                outputs.append({"tool": tool_name, "result": _tool_result(result)})
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MindBridge report tools over MCP stdio")
    parser.add_argument("report_id")
    parser.add_argument("--risk", choices=("CHAT", "CONSULT", "RISK"), required=True)
    args = parser.parse_args()
    output = asyncio.run(run_report_tools(args.report_id, args.risk))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
