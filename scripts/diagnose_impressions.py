"""
Diagnose an impressions mismatch in the creative table for one io_id + date.

Prints three things so we can see exactly where impressions are lost:
  1. Every raw row for the io_id on that date (line_item, trueview_ad, row_num,
     impressions) - so we can see whether the base table even contains all rows.
  2. Per-line-item and per-trueview_ad sums.
  3. The exact aggregate the report uses (GROUP BY trueview_ad), plus the grand
     total.

Usage:
    uv run scripts/diagnose_impressions.py <io_id> <date YYYY-MM-DD>
    uv run scripts/diagnose_impressions.py 1029774313 2026-07-13
"""

import sys
from datetime import date

from google.cloud import bigquery

sys.path.insert(0, "src")

from dv360_pipeline.query import CREATIVE_TABLE, DATASET, PROJECT_ID  # noqa: E402


def _ref() -> str:
    return f"`{PROJECT_ID}.{DATASET}.{CREATIVE_TABLE}`"


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    io_id = int(sys.argv[1])
    day = date.fromisoformat(sys.argv[2])
    client = bigquery.Client(project=PROJECT_ID)
    params = [
        bigquery.ScalarQueryParameter("io_id", "INT64", io_id),
        bigquery.ScalarQueryParameter("day", "DATE", day),
    ]

    # 0. What columns does this table actually have? (confirms row_num, types)
    print("=" * 90)
    print("SCHEMA (name / type)")
    print("=" * 90)
    table = client.get_table(f"{PROJECT_ID}.{DATASET}.{CREATIVE_TABLE}")
    colnames = {f.name for f in table.schema}
    for f in table.schema:
        print(f"  {f.name:45s} {f.field_type}")
    has_row_num = "row_num" in colnames

    # 1. Every raw row for this io_id + date
    row_num_col = "row_num," if has_row_num else ""
    print("\n" + "=" * 90)
    print(f"RAW ROWS for io_id={io_id} on {day}")
    print("=" * 90)
    raw_sql = f"""
        SELECT line_item_id, trueview_ad, {row_num_col} impressions
        FROM {_ref()}
        WHERE insertion_order_id = @io_id AND date = @day
        ORDER BY line_item_id, {('row_num' if has_row_num else 'impressions')}
    """
    raw = list(client.query(raw_sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    for r in raw:
        d = dict(r)
        print("  " + "  ".join(f"{k}={d[k]!r}" for k in d))
    print(f"\n  ROW COUNT: {len(raw)}")

    # 2. Per-line-item sum
    print("\n" + "=" * 90)
    print("PER LINE_ITEM_ID SUM(impressions)")
    print("=" * 90)
    per_li = client.query(
        f"""SELECT line_item_id, COUNT(*) AS n_rows, SUM(impressions) AS impressions
            FROM {_ref()} WHERE insertion_order_id = @io_id AND date = @day
            GROUP BY line_item_id ORDER BY line_item_id""",
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()
    for r in per_li:
        print(f"  line_item_id={r.line_item_id}  n_rows={r.n_rows}  impressions={r.impressions}")

    # 3. The report's actual aggregate (GROUP BY trueview_ad) + grand total
    print("\n" + "=" * 90)
    print("REPORT AGGREGATE (GROUP BY trueview_ad) — what the creative breakdown uses")
    print("=" * 90)
    agg = client.query(
        f"""SELECT trueview_ad, COUNT(*) AS n_rows, SUM(impressions) AS impressions
            FROM {_ref()} WHERE insertion_order_id = @io_id AND date = @day
            GROUP BY trueview_ad ORDER BY trueview_ad""",
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()
    grand = 0
    for r in agg:
        print(f"  n_rows={r.n_rows}  impressions={r.impressions}  trueview_ad={r.trueview_ad!r}")
        grand += r.impressions or 0
    print(f"\n  GRAND TOTAL impressions for {day}: {grand}")


if __name__ == "__main__":
    main()
