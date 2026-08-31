"""
Layer 4: pulls creative, demo, and device data for a single campaign burst
(insertion order) from BigQuery and returns clean, structured DataFrames.

Auth: uses Application Default Credentials (gcloud auth application-default
login, or GOOGLE_APPLICATION_CREDENTIALS pointing at a service account key).
Nothing here reads or writes credentials directly.
"""

import math
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
# Campaign-level reach: one row per campaign (campaign_id, campaign_name, reach).
# Joined to campaign_mapping.campaign_id (a column added for this). Optional —
# if the table or the campaign_id column is absent, reach is simply omitted.
CAMPAIGN_REACH_TABLE = "campaign_reach"

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
               guaranteedrate, kpi_type AS kpi, KPI_Inventory AS kpi_inventory,
               Product AS product, start_date, end_date
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
        kpi_inventory=(None if pd.isna(row["kpi_inventory"]) else float(row["kpi_inventory"])),
        product=row["product"],
        flight_start=row["start_date"],
        flight_end=row["end_date"],
        reach=_fetch_reach(client, io_id),
    )


def _fetch_reach(client: bigquery.Client, io_id: int) -> int | None:
    """Campaign-level reach for the IO's campaign, or None.

    Optional and self-disabling: joins campaign_mapping.campaign_id to the
    campaign_reach table. If either the campaign_id column or the campaign_reach
    table doesn't exist yet (or there's no matching row), returns None and the
    report renders exactly as before — so this can be merged before the reach
    data is set up. campaign_id is CAST to STRING on both sides to avoid
    INT64/STRING join-type mismatches.
    """
    sql = f"""
        SELECT r.reach AS reach
        FROM {_table_ref(CAMPAIGN_MAPPING_TABLE)} m
        JOIN {_table_ref(CAMPAIGN_REACH_TABLE)} r
          ON CAST(m.campaign_id AS STRING) = CAST(r.campaign_id AS STRING)
        WHERE m.io_id = @io_id
        LIMIT 1
    """
    try:
        df = _run_query(client, sql, [bigquery.ScalarQueryParameter("io_id", "INT64", io_id)])
    except Exception:  # noqa: BLE001 - reach is optional; missing table/column => no reach
        return None
    if df.empty or pd.isna(df.iloc[0]["reach"]):
        return None
    return int(df.iloc[0]["reach"])


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


# Metric columns reconciled against the Creative breakdown (integer counts).
_INT_METRIC_COLS = (
    "impressions", "trueview_views", "clicks",
    "video_q25", "video_q50", "video_q75", "video_q100",
    "audio_q25", "audio_q50", "audio_q75", "audio_q100",
)


def _apportion(values: list[float], target: float, ndigits: int) -> list[float]:
    """Scale `values` proportionally so their rounded total is exactly `target`
    (at `ndigits` precision), distributing the rounding residual by largest
    fractional part (Hamilton/largest-remainder). Preserves the input's shape."""
    n = len(values)
    if n == 0:
        return []
    current = sum(values)
    if current <= 0:
        return list(values)  # no distribution to scale from
    factor = 10 ** ndigits
    target_units = int(round(target * factor))
    scaled = [v * target / current * factor for v in values]
    base = [math.floor(s) for s in scaled]
    residual = target_units - sum(base)
    if residual:
        order = sorted(range(n), key=lambda i: scaled[i] - base[i], reverse=True)
        step = 1 if residual > 0 else -1
        for k in range(abs(residual)):
            base[order[k % n]] += step
    return [b / factor for b in base]


def _reconcile_to_creative(df: pd.DataFrame, creative_df: pd.DataFrame) -> pd.DataFrame:
    """Force a breakdown's metric totals to match the Creative breakdown's
    (Creative is authoritative), keeping the breakdown's own proportions.
    Integer metrics stay integers; spend keeps 2 decimals; each rounded column
    sums exactly to Creative's displayed total (the writer's SUM row)."""
    if df.empty or creative_df.empty:
        return df
    df = df.copy()
    for col in df.columns:
        if col in _INT_METRIC_COLS and col in creative_df.columns:
            target = int(round(float(creative_df[col].sum())))
            df[col] = _apportion([float(v) for v in df[col]], target, 0)
        elif col == "spend" and "spend" in creative_df.columns:
            # Match the Creative Total as displayed: sum of per-row rounded spend.
            target = float(sum(round(float(v), 2) for v in creative_df["spend"]))
            df[col] = _apportion([float(v) for v in df[col]], target, 2)
    return df


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

    # Creative is the source of truth: rescale every other breakdown's metric
    # totals to match it, so Creative/Targeting/Device/Gender/Age/Date all
    # reconcile (the device/demo tables can differ from creative by a few rows).
    targeting_df = _reconcile_to_creative(targeting_df, creative_df)
    date_df = _reconcile_to_creative(date_df, creative_df)
    device_df = _reconcile_to_creative(device_df, creative_df)
    gender_df = _reconcile_to_creative(gender_df, creative_df)
    age_df = _reconcile_to_creative(age_df, creative_df)

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
    inventories = [m.kpi_inventory for m in metas if m.kpi_inventory is not None]
    reaches = {m.reach for m in metas if m.reach is not None}

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
        kpi_inventory=sum(inventories) if inventories else None,
        product=products.pop() if len(products) == 1 else "Mixed",
        # Reach can't be summed across campaigns; show it only when the combined
        # IOs share one campaign's reach, otherwise omit it.
        reach=reaches.pop() if len(reaches) == 1 else None,
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
