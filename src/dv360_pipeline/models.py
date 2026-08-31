from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass
class CampaignMeta:
    campaign_name: str
    io_name: str
    io_id: int | list[int]
    budget: float
    currency: str
    guaranteed_rate: float | str
    kpi: str
    kpi_inventory: float | None
    product: str
    flight_start: date
    flight_end: date
    reach: int | None = None  # campaign-level unique reach; None until set up


@dataclass
class CampaignBurstData:
    meta: CampaignMeta
    report_start: date
    report_end: date
    spend: float
    creative_df: pd.DataFrame
    targeting_df: pd.DataFrame
    device_df: pd.DataFrame
    gender_df: pd.DataFrame
    age_df: pd.DataFrame
    date_df: pd.DataFrame
    data_template_df: pd.DataFrame
