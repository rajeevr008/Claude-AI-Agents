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

CREATIVE_TABLE = "sg_creative_breakdown_v2"
DEMO_TABLE = "sg_demo_breakdown_v2"
DEVICE_TABLE = "sg_device_breakdown_v2"
CAMPAIGN_MAPPING_TABLE = "campaign_mapping"

# campaign_mapping.kpi selects BOTH the metric column to sum (KPI_COLUMN_MAP)
# and the spend multiplier formula (KPI_SPEND_FORMULA). Keys are the kpi
# string normalized via _normalize_kpi (stripped + lower-cased) so minor
# casing/whitespace differences in the mapping table don't cause misses.
#
# kpi type                        buying method        formula
# ------------------------------  -------------------  --------------------------------
# trueview: views                 CPV                  trueview_views * rate
# impressions                     CPM                  (impressions / 1000) * rate
# complete views (video)          CPCV                 video_q100 * rate
# clicks                          CPC                  clicks * rate
# first-quartile views (video)    CPV (1st Quartile)   video_q25 * rate
# midpoint views (video)          CPV (midpoint)       video_q50 * rate
# third-quartile views (video)    CPV (3rd Quartile)   video_q75 * rate
# complete listens (audio)        (audio)              audio_q100 * rate
KPI_COLUMN_MAP = {
    "trueview: views": "trueview_views",
    "impressions": "impressions",
    "complete views (video)": "video_q100",
    "clicks": "clicks",
    "first-quartile views (video)": "video_q25",
    "midpoint views (video)": "video_q50",
    "third-quartile views (video)": "video_q75",
    "complete listens (audio)": "audio_q100",
}

# Every buying method is rate * SUM(column) except CPM (impressions), which
# divides by 1000.
KPI_SPEND_FORMULA = {
    "trueview: views": lambda value, rate: value * rate,
    "impressions": lambda value, rate: value * rate / 1000,
    "complete views (video)": lambda value, rate: value * rate,
    "clicks": lambda value, rate: value * rate,
    "first-quartile views (video)": lambda value, rate: value * rate,
    "midpoint views (video)": lambda value, rate: value * rate,
    "third-quartile views (video)": lambda value, rate: value * rate,
    "complete listens (audio)": lambda value, rate: value * rate,
}


def _normalize_kpi(kpi):
    """Normalize a campaign_mapping.kpi value for lookup in the KPI dicts."""
    return kpi.strip().lower() if isinstance(kpi, str) else kpi


def _compute_spend(value, kpi: str, guaranteed_rate: float):
    return KPI_SPEND_FORMULA[kpi](value, guaranteed_rate)


# Shared metric aggregation for the conformed breakdown tables. Video
# quartiles exist on every breakdown; audio quartiles only exist on the
# creative and device tables (the demo/YouTube table has no audio), so they
# are appended separately.
_VIDEO_METRIC_AGG = """
            IFNULL(SUM(impressions), 0) AS impressions,
            IFNULL(SUM(trueview_views), 0) AS trueview_views,
            IFNULL(SUM(clicks), 0) AS clicks,
            IFNULL(SUM(video_q25), 0) AS video_q25,
            IFNULL(SUM(video_q50), 0) AS video_q50,
            IFNULL(SUM(video_q75), 0) AS video_q75,
            IFNULL(SUM(video_q100), 0) AS video_q100"""

_AUDIO_METRIC_AGG = """,
            IFNULL(SUM(audio_q25), 0) AS audio_q25,
            IFNULL(SUM(audio_q50), 0) AS audio_q50,
            IFNULL(SUM(audio_q75), 0) AS audio_q75,
            IFNULL(SUM(audio_q100), 0) AS audio_q100"""

_METRIC_AGG = _VIDEO_METRIC_AGG + _AUDIO_METRIC_AGG


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


def _channel_clause(channel: str | None) -> str:
    """WHERE fragment restricting a breakdown to a single channel."""
    return "\n          AND channel = @channel" if channel else ""


def _burst_params(io_id: int, start_date: date, end_date: date, channel: str | None = None) -> list:
    params = _date_params(io_id, start_date, end_date)
    if channel:
        params.append(bigquery.ScalarQueryParameter("channel", "STRING", channel))
    return params


def _resolve_io_channel(client: bigquery.Client, io_id: int, start_date: date, end_date: date) -> str:
    """Pick the single channel to report for an IO.

    The conformed breakdown tables union YouTube + Non-YouTube rows. When an
    IO has YouTube delivery, the Non-YouTube rows duplicate the same line
    items (they appear in both source reports), so summing across channels
    double-counts impressions/clicks/views. YouTube is authoritative whenever
    present; otherwise the IO is Non-YouTube. Every breakdown for the burst is
    then restricted to this one channel. Demo is YouTube-only, so this never
    removes demo rows for a YouTube IO.
    """
    sql = f"""
        SELECT COUNTIF(channel = 'youtube') AS youtube_rows
        FROM {_table_ref(CREATIVE_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date
    """
    df = _run_query(client, sql, _date_params(io_id, start_date, end_date))
    has_youtube = not df.empty and int(df.iloc[0]["youtube_rows"]) > 0
    return "youtube" if has_youtube else "non_youtube"


def _fetch_campaign_meta(client: bigquery.Client, io_id: int) -> CampaignMeta:
    sql = f"""
        SELECT campaign_name, io_name, io_id, Budget AS budget, Currency AS currency,
               guaranteedrate, kpi_type AS kpi, Product AS product, start_date, end_date
        FROM {_table_ref(CAMPAIGN_MAPPING_TABLE)}
        WHERE io_id = @io_id
        LIMIT 1
    """
    params = [bigquery.ScalarQueryParameter("io_id", "INT64", io_id)]
    df = _run_query(client, sql, params)

    if df.empty:
        raise CampaignNotFoundError(
            f"No row in campaign_mapping for io_id={io_id}. "
            "Add an entry there (campaign_name, io_name, io_id, Budget, "
            "guaranteedrate, kpi_type, Product, start_date, end_date) before running a report."
        )

    row = df.iloc[0]
    kpi = _normalize_kpi(row["kpi"])
    if kpi not in KPI_COLUMN_MAP:
        raise UnknownKpiError(
            f"campaign_mapping.kpi_type={row['kpi']!r} for io_id={io_id} has no mapping "
            f"to a breakdown-table column. Known kpi values: {list(KPI_COLUMN_MAP)}. "
            "Add a mapping in KPI_COLUMN_MAP and KPI_SPEND_FORMULA if this is a "
            "new, valid KPI."
        )

    return CampaignMeta(
        campaign_name=row["campaign_name"],
        io_name=row["io_name"],
        io_id=int(row["io_id"]),
        budget=float(row["budget"]),
        currency=row["currency"],
        guaranteed_rate=float(row["guaranteedrate"]),
        kpi=kpi,
        product=row["product"],
        flight_start=row["start_date"],
        flight_end=row["end_date"],
    )


def list_ios_for_campaign(campaign_name: str) -> pd.DataFrame:
    """All IOs under a campaign_name, for the app's IO picker table."""
    client = bigquery.Client(project=PROJECT_ID)
    sql = f"""
        SELECT io_id, io_name, Budget AS budget, Currency AS currency, guaranteedrate,
               kpi_type AS kpi, Product AS product, start_date, end_date
        FROM {_table_ref(CAMPAIGN_MAPPING_TABLE)}
        WHERE LOWER(campaign_name) LIKE LOWER(CONCAT('%', @campaign_name, '%'))
          AND io_id IS NOT NULL
        ORDER BY io_name
    """
    params = [bigquery.ScalarQueryParameter("campaign_name", "STRING", campaign_name)]
    return _run_query(client, sql, params)


def _creative_agg_sql(select_expr: str, group_by: str, channel: str | None) -> str:
    return f"""
        SELECT
            {select_expr},{_METRIC_AGG}
        FROM {_table_ref(CREATIVE_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date{_channel_clause(channel)}
        GROUP BY {group_by}
        ORDER BY {group_by}
    """


def _fetch_creative_df(client, io_id, start_date, end_date, channel) -> pd.DataFrame:
    sql = _creative_agg_sql("creative AS creative_name", "creative_name", channel)
    return _run_query(client, sql, _burst_params(io_id, start_date, end_date, channel))


def _fetch_targeting_df(client, io_id, start_date, end_date, channel) -> pd.DataFrame:
    targeting_expr = (
        "SPLIT(line_item, '-')[OFFSET(ARRAY_LENGTH(SPLIT(line_item, '-')) - 1)] AS targeting"
    )
    sql = _creative_agg_sql(targeting_expr, "targeting", channel)
    return _run_query(client, sql, _burst_params(io_id, start_date, end_date, channel))


def _fetch_date_df(client, io_id, start_date, end_date, channel) -> pd.DataFrame:
    sql = _creative_agg_sql("date", "date", channel)
    return _run_query(client, sql, _burst_params(io_id, start_date, end_date, channel))


def _fetch_data_template_df(client, io_id, start_date, end_date, channel) -> pd.DataFrame:
    """One row per date + creative + targeting, for the Data Template sheet's
    Creative and Strategy columns (date_df alone only has date granularity)."""
    targeting_expr = (
        "SPLIT(line_item, '-')[OFFSET(ARRAY_LENGTH(SPLIT(line_item, '-')) - 1)] AS targeting"
    )
    sql = f"""
        SELECT
            date,
            creative AS creative_name,
            {targeting_expr},{_METRIC_AGG}
        FROM {_table_ref(CREATIVE_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date{_channel_clause(channel)}
        GROUP BY date, creative_name, targeting
        ORDER BY date, creative_name, targeting
    """
    return _run_query(client, sql, _burst_params(io_id, start_date, end_date, channel))


def _fetch_device_df(client, io_id, start_date, end_date, channel) -> pd.DataFrame:
    # Device table (like Creative) carries both video and audio quartiles.
    sql = f"""
        SELECT
            device_type,{_METRIC_AGG}
        FROM {_table_ref(DEVICE_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date{_channel_clause(channel)}
        GROUP BY device_type
        ORDER BY device_type
    """
    return _run_query(client, sql, _burst_params(io_id, start_date, end_date, channel))


def _fetch_demo_df(client, io_id, start_date, end_date, group_col, channel) -> pd.DataFrame:
    # Demo is YouTube-only and has no audio columns, so it uses the
    # video-only metric aggregation. An audio-KPI IO therefore has no demo
    # spend (handled by _add_spend_column when the column is absent).
    sql = f"""
        SELECT
            {group_col},{_VIDEO_METRIC_AGG}
        FROM {_table_ref(DEMO_TABLE)}
        WHERE insertion_order_id = @io_id
          AND date BETWEEN @start_date AND @end_date{_channel_clause(channel)}
        GROUP BY {group_col}
        ORDER BY {group_col}
    """
    return _run_query(client, sql, _burst_params(io_id, start_date, end_date, channel))


def _add_spend_column(df: pd.DataFrame, kpi: str, guaranteed_rate: float) -> pd.DataFrame:
    kpi_column = KPI_COLUMN_MAP[kpi]
    df = df.copy()
    # A breakdown may not carry the KPI's metric column (e.g. the demo table
    # has no audio_* columns for an audio KPI); its spend is then zero.
    if kpi_column not in df.columns:
        df["spend"] = 0.0
    else:
        df["spend"] = _compute_spend(df[kpi_column], kpi, guaranteed_rate)
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

    # Report a single channel per IO so YouTube IOs don't double-count the
    # duplicate Non-YouTube rows (see _resolve_io_channel).
    channel = _resolve_io_channel(client, io_id, start_date, end_date)

    creative_df = _fetch_creative_df(client, io_id, start_date, end_date, channel)
    targeting_df = _fetch_targeting_df(client, io_id, start_date, end_date, channel)
    date_df = _fetch_date_df(client, io_id, start_date, end_date, channel)
    device_df = _fetch_device_df(client, io_id, start_date, end_date, channel)
    gender_df = _fetch_demo_df(client, io_id, start_date, end_date, "gender", channel)
    age_df = _fetch_demo_df(client, io_id, start_date, end_date, "age", channel)
    data_template_df = _fetch_data_template_df(client, io_id, start_date, end_date, channel)

    kpi_column = KPI_COLUMN_MAP[meta.kpi]
    total_kpi_value = float(creative_df[kpi_column].sum()) if not creative_df.empty else 0.0
    spend = _compute_spend(total_kpi_value, meta.kpi, meta.guaranteed_rate)

    creative_df = _add_spend_column(creative_df, meta.kpi, meta.guaranteed_rate)
    targeting_df = _add_spend_column(targeting_df, meta.kpi, meta.guaranteed_rate)
    date_df = _add_spend_column(date_df, meta.kpi, meta.guaranteed_rate)
    data_template_df = _add_spend_column(data_template_df, meta.kpi, meta.guaranteed_rate)

    # Device carries the same metric columns as Creative; the demo table is
    # YouTube-only and has no audio columns, so an audio KPI yields zero demo
    # spend (handled in _add_spend_column). Every breakdown's spend uses the
    # KPI's own metric column, same as the creative-level breakdown.
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


def fetch_separate_campaign_bursts(
    io_ids: list[int], start_date: date | None = None, end_date: date | None = None
) -> list[CampaignBurstData]:
    """One CampaignBurstData per IO (kept separate, not merged) for the
    "separate sheet per IO" export. If start_date/end_date are omitted, each
    IO uses its own flight range from campaign_mapping.
    """
    if not io_ids:
        raise ValueError("io_ids must not be empty")
    return [fetch_campaign_burst(io_id, start_date, end_date) for io_id in io_ids]


def _combine_dfs(dfs: list[pd.DataFrame], group_cols: list[str]) -> pd.DataFrame:
    non_empty = [df for df in dfs if not df.empty]
    if not non_empty:
        return dfs[0]
    combined = pd.concat(non_empty, ignore_index=True)
    sum_cols = [c for c in combined.columns if c not in group_cols]
    return combined.groupby(group_cols, as_index=False)[sum_cols].sum()


def fetch_combined_campaign_burst(
    io_ids: list[int], start_date: date | None = None, end_date: date | None = None
) -> CampaignBurstData:
    """
    Fetch and merge multiple IOs into one combined report (e.g. the same
    campaign split across several IOs/markets). Row-level breakdowns are
    summed across IOs on their shared key column (creative_name, targeting,
    device_type, etc.) - each IO's spend is computed with its own kpi/rate
    first, then summed, so this works even if the selected IOs have
    different KPI types or guaranteed rates.

    If start_date/end_date are omitted, defaults to the union of the
    selected IOs' flight ranges (min start, max end).
    """
    if not io_ids:
        raise ValueError("io_ids must not be empty")
    if len(io_ids) == 1:
        return fetch_campaign_burst(io_ids[0], start_date, end_date)

    client = bigquery.Client(project=PROJECT_ID)
    metas = []
    for io_id in io_ids:
        try:
            metas.append(_fetch_campaign_meta(client, io_id))
        except NotFound as exc:
            raise RuntimeError(
                f"BigQuery table not found while looking up campaign_mapping: {exc}"
            ) from exc

    if start_date is None:
        start_date = min(m.flight_start for m in metas)
    if end_date is None:
        end_date = max(m.flight_end for m in metas)
    if start_date > end_date:
        raise ValueError(f"start_date ({start_date}) is after end_date ({end_date})")

    results = [fetch_campaign_burst(io_id, start_date, end_date) for io_id in io_ids]

    guaranteed_rates = {m.guaranteed_rate for m in metas}
    kpis = {m.kpi for m in metas}
    products = {m.product for m in metas}
    currencies = {m.currency for m in metas}

    combined_meta = CampaignMeta(
        campaign_name=metas[0].campaign_name,
        io_name=" + ".join(m.io_name for m in metas),
        io_id=list(io_ids),
        # NB: budget/spend are summed in each IO's own currency; when the
        # selected IOs span currencies this total is not FX-converted and
        # `currency` becomes "Mixed" to flag it.
        budget=sum(m.budget for m in metas),
        currency=currencies.pop() if len(currencies) == 1 else "Mixed",
        guaranteed_rate=guaranteed_rates.pop() if len(guaranteed_rates) == 1 else "Mixed",
        kpi=kpis.pop() if len(kpis) == 1 else "Mixed",
        product=products.pop() if len(products) == 1 else "Mixed",
        flight_start=min(m.flight_start for m in metas),
        flight_end=max(m.flight_end for m in metas),
    )

    return CampaignBurstData(
        meta=combined_meta,
        report_start=start_date,
        report_end=end_date,
        spend=sum(r.spend for r in results),
        creative_df=_combine_dfs([r.creative_df for r in results], ["creative_name"]),
        targeting_df=_combine_dfs([r.targeting_df for r in results], ["targeting"]),
        device_df=_combine_dfs([r.device_df for r in results], ["device_type"]),
        gender_df=_combine_dfs([r.gender_df for r in results], ["gender"]),
        age_df=_combine_dfs([r.age_df for r in results], ["age"]),
        date_df=_combine_dfs([r.date_df for r in results], ["date"]),
        data_template_df=_combine_dfs(
            [r.data_template_df for r in results], ["date", "creative_name", "targeting"]
        ),
    )
