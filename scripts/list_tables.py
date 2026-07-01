"""
Lists all tables in the apex_dv360 dataset whose name contains a given
substring. Use this when a table name from discover_schema.py 404s, to find
the exact current name instead of guessing.

Usage:
    uv run scripts/list_tables.py demo
    uv run scripts/list_tables.py device
"""

import sys

from google.cloud import bigquery

PROJECT_ID = "ssc-apex-apac-prd-mg"
DATASET = "apex_dv360"


def main() -> None:
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    client = bigquery.Client(project=PROJECT_ID)
    dataset_ref = f"{PROJECT_ID}.{DATASET}"
    print(f"Tables in {dataset_ref} matching {needle!r}:\n")
    for table in client.list_tables(dataset_ref):
        if needle in table.table_id.lower():
            print(f"  {table.table_id}")


if __name__ == "__main__":
    main()
