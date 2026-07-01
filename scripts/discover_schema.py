"""
Stage 1 discovery script. Run this locally (where your gcloud/service-account
auth is already set up) to print schema + sample rows for the three source
tables. Paste the output back so the query/mapping layer can be built against
the real columns instead of assumptions.

Auth: uses Application Default Credentials. Either run
    gcloud auth application-default login
or set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON path before
running this script. Nothing here reads or writes credentials directly.

Usage:
    uv run scripts/discover_schema.py
"""

from google.cloud import bigquery

PROJECT_ID = "ssc-apex-apac-prd-mg"
DATASET = "apex_dv360"

TABLES = {
    "creative": "SG_creative_rpt_aiagent_dv360_607124520_1677971342_20260101_20260630_20260629_220348",
    "demo": "sg_demo_aiagent_dv360_607124520_1677978667_20260101_20260630_20260629_224448",
    "device": "sg_device_rpt_dv360_607124520_1677976737_20260101_20260630_20260629_222138",
}


def describe_table(client: bigquery.Client, label: str, table_name: str) -> None:
    table_ref = f"{PROJECT_ID}.{DATASET}.{table_name}"
    print("=" * 100)
    print(f"{label.upper()} TABLE: {table_ref}")
    print("=" * 100)

    table = client.get_table(table_ref)
    print(f"\nRow count (approx): {table.num_rows}")
    print("\nSCHEMA:")
    for field in table.schema:
        print(f"  {field.name:40s} {field.field_type:12s} mode={field.mode}")

    print("\nSAMPLE ROWS (LIMIT 5):")
    rows = client.query(f"SELECT * FROM `{table_ref}` LIMIT 5").result()
    for i, row in enumerate(rows):
        print(f"\n  --- row {i} ---")
        for key, value in dict(row).items():
            print(f"    {key}: {value!r}")
    print()


def main() -> None:
    client = bigquery.Client(project=PROJECT_ID)
    for label, table_name in TABLES.items():
        describe_table(client, label, table_name)


if __name__ == "__main__":
    main()
