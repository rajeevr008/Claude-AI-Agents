"""
Stage 3 validation: fetches a real campaign burst and writes it into the
Excel template, producing an actual .xlsx to open and check.

Usage:
    uv run scripts/test_layer5.py <io_id> <start_date YYYY-MM-DD> <end_date YYYY-MM-DD> [output_path]

Example:
    uv run scripts/test_layer5.py 1026326707 2026-03-04 2026-03-31 output/malay_outreach_report.xlsx
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, "src")

from dv360_pipeline.query import fetch_campaign_burst  # noqa: E402
from dv360_pipeline.writer import write_report  # noqa: E402

TEMPLATE_PATH = "templates/campaign_burst_template.xlsx"


def main() -> None:
    if len(sys.argv) not in (4, 5):
        print(__doc__)
        sys.exit(1)

    io_id = int(sys.argv[1])
    start_date = date.fromisoformat(sys.argv[2])
    end_date = date.fromisoformat(sys.argv[3])
    output_path = sys.argv[4] if len(sys.argv) == 5 else f"output/report_{io_id}_{start_date}_{end_date}.xlsx"

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching data for io_id={io_id} range {start_date}..{end_date}...")
    data = fetch_campaign_burst(io_id, start_date, end_date)

    print(f"Writing report to {output_path}...")
    write_report(data, TEMPLATE_PATH, output_path)

    print(f"Done. Open {output_path} in Excel to check formatting and formula recalculation.")


if __name__ == "__main__":
    main()
