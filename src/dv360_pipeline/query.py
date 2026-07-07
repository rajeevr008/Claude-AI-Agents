"""
Layer 4: pulls creative, demo, and device data for a single campaign burst
(insertion order) from BigQuery and returns clean, structured DataFrames.

Auth: uses Application Default Credentials (gcloud auth application-default
login, or GOOGLE_APPLICATION_CREDENTIALS pointing at a service account key).
Nothing here reads or writes credentials directly.
"""

from datetime import date

import pandas as pd
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from dv360_pipeline.models import CampaignBurstData, CampaignMeta

PROJECT_ID = "ssc-apex-apac-prd-mg"
DATASET = "apex_dv360"

CREATIVE_TABLE = "SG_creative_rpt_aiagent_dv360_607124520_1677971342_20260101_20260630_20260629_220348"
DEMO_TABLE = "sg_demo_aiagent_dv360_607124520_1677978667_20260101_20260630_20260629_224448"
DEVICE_TABLE = "sg_device_rpt_dv360_607124520_1677976737_20260101_20260630_20260629_222138"
CAMPAIGN_MAPPING_TABLE = "campaign_mapping"

# Maps campaign_mapping.kpi values to the creative-table column they're
# measured against, for the spend formula: spend = guaranteed_rate * SUM(kpi_column).
KPI_COLUMN_MAP = {
    "clicks": "clicks",
    "views": "youtube_views",
    "impressions": "impressions",
}


class CampaignNotFoundError(Exception):
    """Raised when io_id has no row in campaign_mapping."""


class UnknownKpiError(Exception):
    """Raised when campaign_mapping.kpi doesn't match a known creative-table column."""


def _table_ref(table_name: str) -> str:
    return f"`{PROJECT_ID}.{DATASET}.{table_name}`"


def _date_params(io_id: int, start_date: date, end_date: date) -> list:
    return [
        bigquery.ScalarQueryParameter("io_id", "INT64", io_id),
        bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
        bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
    ]


def _run_query(client: bigquery.Client, sql: str, params: list) -> pd.DataFrame:
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return client.query(sql, job_config=job_config).result().to_dataframe()


def _fetch_campaign_meta(client: bigquery.Client, io_id: int) -> CampaignMeta:
    sql = f"""
        SELECT campaign_name, io_name, io_id, budget, guaranteedrate, kpi,
               channel, start_date, end_date
        FROM {_table_ref(CAMPAIGN_MAPPING_TABLE)}
        WHERE io_id = @io_id
        LIMIT 1
    """
    params = [bigquery.ScalarQueryParameter("io_id", "INT64", io_id)]
    df = _run_query(client, sql, params)

    if df.empty:
        raise CampaignNotFoundError(
            f"No row in campaign_mapping for io_id={io_id}. "
            "Add an entry there (campaign_name, io_name, io_id, budget, "
            "guaranteedrate, kpi, channel, start_date, end_date) before running a report."
        )

    row = df.iloc[0]
    if row["kpi"] not in KPI_COLUMN_MAP:
        raise UnknownKpiError(
            f"campaign_mapping.kpi={row['kpi']!r} for io_id={io_id} has no mapping "
            f"to a creative-table column. Known kpi values: {list(KPI_COLUMN_MAP)}. "
            "Add a mapping in KPI_COLUMN_MAP if this is a new, valid KPI."
        )

    return CampaignMeta(
        campaign_name=row["campaign_name"],
        io_name=row["io_name"],
        io_id=int(row["io_id"]),
        budget=float(row["budget"]),
        guaranteed_rate=float(row["guaranteedrate"]),
        kpi=row["kpi"],
        channel=row["channel"],
        flight_start=row["start_date"],
        flight_end=row["end_date"],
    )


def _creative_agg_sql(select_expr: str, group_by: str) -> str:
    return f"""
        SELECT
            {select_expr},
            IFNULL(SUM(impressions), 0) AS impressions,
            IFNULL(SUM(youtube_views), 0) AS youtube_views,
            IFNULL(SUM(clicks), 0) AS clicks,
            IFNULL(SUM(rich_media_video_first_quartile_completes), 0) AS video_25,
            IFNULL(SUM(rich_media_video_midpoints), 0) AS video_50,
            IFNULL(SUM(rich_media_video_third_quartile_completes), 0) AS video_75,
            IFNULL(SUM(rich_media_video_completions), 0) AS video_100
        FROM {_table_ref(CREATIVE_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date
        GROUP BY {group_by}
        ORDER BY {group_by}
    """


def _fetch_creative_df(client: bigquery.Client, io_id: int, start_date: date, end_date: date) -> pd.DataFrame:
    sql = _creative_agg_sql("trueview_ad AS creative_name", "creative_name")
    return _run_query(client, sql, _date_params(io_id, start_date, end_date))


def _fetch_targeting_df(client: bigquery.Client, io_id: int, start_date: date, end_date: date) -> pd.DataFrame:
    targeting_expr = (
        "SPLIT(line_item, '-')[OFFSET(ARRAY_LENGTH(SPLIT(line_item, '-')) - 1)] AS targeting"
    )
    sql = _creative_agg_sql(targeting_expr, "targeting")
    return _run_query(client, sql, _date_params(io_id, start_date, end_date))


def _fetch_date_df(client: bigquery.Client, io_id: int, start_date: date, end_date: date) -> pd.DataFrame:
    sql = _creative_agg_sql("date", "date")
    return _run_query(client, sql, _date_params(io_id, start_date, end_date))


def _fetch_data_template_df(client: bigquery.Client, io_id: int, start_date: date, end_date: date) -> pd.DataFrame:
    """One row per date + creative + targeting, for the Data Template sheet's
    Creative and Strategy columns (date_df alone only has date granularity)."""
    targeting_expr = (
        "SPLIT(line_item, '-')[OFFSET(ARRAY_LENGTH(SPLIT(line_item, '-')) - 1)] AS targeting"
    )
    sql = f"""
        SELECT
            date,
            trueview_ad AS creative_name,
            {targeting_expr},
            IFNULL(SUM(impressions), 0) AS impressions,
            IFNULL(SUM(youtube_views), 0) AS youtube_views,
            IFNULL(SUM(clicks), 0) AS clicks,
            IFNULL(SUM(rich_media_video_first_quartile_completes), 0) AS video_25,
            IFNULL(SUM(rich_media_video_midpoints), 0) AS video_50,
            IFNULL(SUM(rich_media_video_third_quartile_completes), 0) AS video_75,
            IFNULL(SUM(rich_media_video_completions), 0) AS video_100
        FROM {_table_ref(CREATIVE_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date
        GROUP BY date, creative_name, targeting
        ORDER BY date, creative_name, targeting
    """
    return _run_query(client, sql, _date_params(io_id, start_date, end_date))


def _fetch_device_df(client: bigquery.Client, io_id: int, start_date: date, end_date: date) -> pd.DataFrame:
    sql = f"""
        SELECT
            device_type,
            IFNULL(SUM(impressions), 0) AS impressions,
            IFNULL(SUM(youtube_views), 0) AS youtube_views,
            IFNULL(SUM(clicks), 0) AS clicks
        FROM {_table_ref(DEVICE_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date
        GROUP BY device_type
        ORDER BY device_type
    """
    return _run_query(client, sql, _date_params(io_id, start_date, end_date))


def _fetch_demo_df(client: bigquery.Client, io_id: int, start_date: date, end_date: date, group_col: str) -> pd.DataFrame:
    sql = f"""
        SELECT
            {group_col},
            IFNULL(SUM(impressions), 0) AS impressions,
            IFNULL(SUM(youtube_views), 0) AS youtube_views,
            IFNULL(SUM(clicks), 0) AS clicks
        FROM {_table_ref(DEMO_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date
        GROUP BY {group_col}
        ORDER BY {group_col}
    """
    return _run_query(client, sql, _date_params(io_id, start_date, end_date))


def _add_spend_column(df: pd.DataFrame, kpi: str, guaranteed_rate: float) -> pd.DataFrame:
    kpi_column = KPI_COLUMN_MAP[kpi]
    df = df.copy()
    df["spend"] = df[kpi_column] * guaranteed_rate
    return df


def fetch_campaign_burst(
    io_id: int, start_date: date | None = None, end_date: date | None = None
) -> CampaignBurstData:
    """
    Fetch all data needed for one campaign burst report.

    io_id: DV360 insertion order ID (join key across all tables).
    start_date, end_date: the reporting date range for this burst (may be a
        sub-range of the IO's full flight dates). If omitted, defaults to
        the IO's full flight dates from campaign_mapping.
    """
    client = bigquery.Client(project=PROJECT_ID)

    try:
        meta = _fetch_campaign_meta(client, io_id)
    except NotFound as exc:
        raise RuntimeError(
            f"BigQuery table not found while looking up campaign_mapping: {exc}"
        ) from exc

    if start_date is None:
        start_date = meta.flight_start
    if end_date is None:
        end_date = meta.flight_end

    if start_date > end_date:
        raise ValueError(f"start_date ({start_date}) is after end_date ({end_date})")

    creative_df = _fetch_creative_df(client, io_id, start_date, end_date)
    targeting_df = _fetch_targeting_df(client, io_id, start_date, end_date)
    date_df = _fetch_date_df(client, io_id, start_date, end_date)
    device_df = _fetch_device_df(client, io_id, start_date, end_date)
    gender_df = _fetch_demo_df(client, io_id, start_date, end_date, "youtube_gender")
    age_df = _fetch_demo_df(client, io_id, start_date, end_date, "youtube_age")
    data_template_df = _fetch_data_template_df(client, io_id, start_date, end_date)

    kpi_column = KPI_COLUMN_MAP[meta.kpi]
    total_kpi_value = float(creative_df[kpi_column].sum()) if not creative_df.empty else 0.0
    spend = total_kpi_value * meta.guaranteed_rate

    creative_df = _add_spend_column(creative_df, meta.kpi, meta.guaranteed_rate)
    targeting_df = _add_spend_column(targeting_df, meta.kpi, meta.guaranteed_rate)
    date_df = _add_spend_column(date_df, meta.kpi, meta.guaranteed_rate)
    data_template_df = _add_spend_column(data_template_df, meta.kpi, meta.guaranteed_rate)

    # Device/gender/age tables don't carry every creative-table KPI column
    # (e.g. no video-quartile-based spend split needed there); spend on
    # these breakdowns uses whichever of clicks/youtube_views/impressions
    # the KPI maps to, same as the creative-level breakdown.
    device_df = _add_spend_column(device_df, meta.kpi, meta.guaranteed_rate)
    gender_df = _add_spend_column(gender_df, meta.kpi, meta.guaranteed_rate)
    age_df = _add_spend_column(age_df, meta.kpi, meta.guaranteed_rate)

    return CampaignBurstData(
        meta=meta,
        report_start=start_date,
        report_end=end_date,
        spend=spend,
        creative_df=creative_df,
        targeting_df=targeting_df,
        device_df=device_df,
        gender_df=gender_df,
        age_df=age_df,
        date_df=date_df,
        data_template_df=data_template_df,
    )
