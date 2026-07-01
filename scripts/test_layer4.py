"""
Stage 2 validation: runs the Layer 4 query module against a real campaign
burst and prints the results so we can check them before building Layer 5.

Usage:
    uv run scripts/test_layer4.py <io_id> <start_date YYYY-MM-DD> <end_date YYYY-MM-DD>

Example (using a real io_id from campaign_mapping):
    uv run scripts/test_layer4.py 1026326707 2026-03-04 2026-03-10
"""

import sys
from datetime import date

sys.path.insert(0, "src")

from dv360_pipeline.query import fetch_campaign_burst  # noqa: E402


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    io_id = int(sys.argv[1])
    start_date = date.fromisoformat(sys.argv[2])
    end_date = date.fromisoformat(sys.argv[3])

    data = fetch_campaign_burst(io_id, start_date, end_date)

    print("=" * 80)
    print("CAMPAIGN META")
    print("=" * 80)
    print(data.meta)
    print(f"\nreport_start={data.report_start}  report_end={data.report_end}")
    print(f"computed spend={data.spend:,.2f}")

    for name, df in [
        ("creative_df", data.creative_df),
        ("targeting_df", data.targeting_df),
        ("device_df", data.device_df),
        ("gender_df", data.gender_df),
        ("age_df", data.age_df),
        ("date_df", data.date_df),
    ]:
        print("\n" + "=" * 80)
        print(name.upper())
        print("=" * 80)
        print(df.to_string())


if __name__ == "__main__":
    main()
