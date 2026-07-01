from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass
class CampaignMeta:
    campaign_name: str
    io_name: str
    io_id: int
    budget: float
    guaranteed_rate: float
    kpi: str
    channel: str
    flight_start: date
    flight_end: date


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
