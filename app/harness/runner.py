from __future__ import annotations

import argparse
import sys

from app.harness.engineering import (
    SUITE_NAMES,
    EngineeringHarness,
    render_summary,
    report_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the deterministic MindNook engineering verification harness."
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=SUITE_NAMES,
        help="Run one suite; repeat the option to select multiple suites.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full machine-readable report instead of the concise summary.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = EngineeringHarness().run(args.suite)
    print(report_json(report) if args.json else render_summary(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
