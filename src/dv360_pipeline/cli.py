"""
CLI entry point wiring Layer 4 (query) and Layer 5 (writer) together.

Usage:
    uv run dv360-report <io_id>
    uv run dv360-report <io_id> --start 2026-03-04 --end 2026-03-10

If --start/--end are omitted, the report covers the IO's full flight range
(start_date/end_date from campaign_mapping).
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from dv360_pipeline.query import CampaignNotFoundError, UnknownKpiError, fetch_campaign_burst
from dv360_pipeline.writer import write_report

DEFAULT_TEMPLATE = "templates/campaign_burst_template.xlsx"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}, expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dv360-report",
        description="Generate a DV360 campaign burst Excel report from BigQuery data.",
    )
    parser.add_argument("io_id", type=int, help="DV360 insertion order ID (campaign_mapping.io_id)")
    parser.add_argument(
        "--start",
        type=_parse_date,
        default=None,
        help="Reporting range start (YYYY-MM-DD). Defaults to the IO's flight start_date.",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=None,
        help="Reporting range end (YYYY-MM-DD). Defaults to the IO's flight end_date.",
    )
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help=f"Path to the Excel template (default: {DEFAULT_TEMPLATE})",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output .xlsx path (default: output/report_<io_id>_<start>_<end>.xlsx)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be provided together, or both omitted to use the IO's full flight range.")

    try:
        print(f"Fetching data for io_id={args.io_id}...")
        data = fetch_campaign_burst(args.io_id, args.start, args.end)
    except (CampaignNotFoundError, UnknownKpiError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or f"output/report_{args.io_id}_{data.report_start}_{data.report_end}.xlsx"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"Report range: {data.report_start} to {data.report_end}")
    print(f"Writing report to {output_path}...")
    write_report(data, args.template, output_path)
    print(f"Done: {output_path}")


if __name__ == "__main__":
    main()
