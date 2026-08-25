"""
Streamlit front end for the DV360 campaign burst report - "APEX Campaign Report".

Run:
    uv run streamlit run app.py

Wraps the existing Layer 4 (query.py) / Layer 5 (writer.py) modules - no
report logic lives here, this is just a form + download button on top of
dv360_pipeline.query.fetch_campaign_burst() and dv360_pipeline.writer.write_report().

Color scheme matches apxexchange.com: dark navy-to-teal gradient background,
gold/tan accent for headings, white body text (see .streamlit/config.toml
for the Streamlit widget theme).
"""

import base64
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, "src")

import streamlit as st

from dv360_pipeline.query import (
    CampaignNotFoundError,
    UnknownKpiError,
    fetch_combined_campaign_burst,
    fetch_separate_campaign_bursts,
    list_ios_for_campaign,
)
from dv360_pipeline.writer import (
    _excel_safe_sheet_name,
    _io_sheet_label,
    write_multi_io_report,
    write_report,
)

TEMPLATE_PATH = "templates/campaign_burst_template.xlsx"
LOGO_PATH = "logo/apx_logo.png"

COMBINE_MODE = "Combine into one report"
SEPARATE_MODE = "Separate sheet per IO"


def _slug(text: str, maxlen: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_")
    return slug[:maxlen] or "report"


def _offer_download(write_fn, file_name: str) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / file_name
        write_fn(str(output_path))
        report_bytes = output_path.read_bytes()
    st.download_button(
        label="Download report (.xlsx)",
        data=report_bytes,
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _generate_combined(io_ids: list[int], start_date, end_date) -> None:
    try:
        with st.spinner("Fetching data from BigQuery..."):
            data = fetch_combined_campaign_burst(io_ids, start_date, end_date)
    except (CampaignNotFoundError, UnknownKpiError, ValueError) as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - surface auth/network errors to the user
        st.error(f"Failed to fetch data: {exc}")
        return

    st.success(f"Fetched data for **{data.meta.campaign_name}** ({data.meta.io_name})")
    cur = data.meta.currency
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Budget", f"{cur} {data.meta.budget:,.2f}")
    col2.metric("Spend", f"{cur} {data.spend:,.2f}")
    col3.metric("Pace", f"{data.spend / data.meta.budget:.1%}" if data.meta.budget else "N/A")
    col4.metric("KPI", str(data.meta.kpi))
    if cur == "Mixed":
        st.warning(
            "Selected IOs span multiple currencies — Budget and Spend are "
            "summed without FX conversion and are not directly comparable."
        )
    st.write(f"Reporting range: **{data.report_start}** to **{data.report_end}**")
    if len(io_ids) > 1:
        st.caption(f"Combined from {len(io_ids)} insertion orders: {io_ids}")

    io_id_label = "_".join(str(i) for i in io_ids)
    _offer_download(
        lambda p: write_report(data, TEMPLATE_PATH, p),
        f"report_{io_id_label}_{data.report_start}_{data.report_end}.xlsx",
    )


def _generate_separate(io_ids: list[int], start_date, end_date) -> None:
    import pandas as pd

    try:
        with st.spinner("Fetching each IO from BigQuery..."):
            datas = fetch_separate_campaign_bursts(io_ids, start_date, end_date)
    except (CampaignNotFoundError, UnknownKpiError, ValueError) as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - surface auth/network errors to the user
        st.error(f"Failed to fetch data: {exc}")
        return

    st.success(f"Fetched {len(datas)} insertion order(s) — one sheet per IO.")

    # Preview the exact sheet-tab names (truncated/de-duped like the writer).
    used: set = set()
    sheet_names = [_excel_safe_sheet_name(_io_sheet_label(d.meta.io_name), used) for d in datas]
    summary = pd.DataFrame(
        {
            "Sheet tab": sheet_names,
            "IO": [d.meta.io_name for d in datas],
            "Currency": [d.meta.currency for d in datas],
            "Budget": [round(d.meta.budget, 2) for d in datas],
            "Spend": [round(d.spend, 2) for d in datas],
            "Pace": [f"{d.spend / d.meta.budget:.1%}" if d.meta.budget else "N/A" for d in datas],
        }
    )
    st.dataframe(summary, hide_index=True, use_container_width=True)

    campaign = datas[0].meta.campaign_name if datas else "report"
    _offer_download(
        lambda p: write_multi_io_report(datas, TEMPLATE_PATH, p),
        f"report_{_slug(campaign)}_{len(datas)}_IOs.xlsx",
    )


GOLD = "#C9A876"
TEAL = "#1A6B7A"
NAVY_DARK = "#0A0E27"
NAVY_MID = "#12224A"

st.set_page_config(page_title="APEX Campaign Report", page_icon="📊", layout="centered")

_logo_b64 = base64.b64encode(Path(LOGO_PATH).read_bytes()).decode() if Path(LOGO_PATH).exists() else None

st.markdown(
    f"""
    <style>
    .apex-header {{
        background: linear-gradient(135deg, #000000 0%, {NAVY_DARK} 35%, {NAVY_MID} 65%, {TEAL} 100%);
        padding: 1.75rem 2rem;
        border-radius: 10px;
        margin-bottom: 1.5rem;
    }}
    .apex-header h1 {{
        color: {GOLD};
        font-weight: 700;
        margin: 0;
        font-size: 1.9rem;
    }}
    .apex-header p {{
        color: #FFFFFF;
        margin: 0.25rem 0 0 0;
        opacity: 0.85;
    }}
    .apex-header-row {{
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
    }}
    .apex-logo {{
        background: #FFFFFF;
        border-radius: 8px;
        padding: 0.5rem 0.9rem;
        display: flex;
        align-items: center;
    }}
    .apex-logo img {{
        height: 42px;
        display: block;
    }}
    div[data-testid="stMetricLabel"] {{
        color: {GOLD} !important;
    }}
    </style>
    <div class="apex-header">
        <div class="apex-header-row">
            <div>
                <h1>APEX Campaign Report</h1>
                <p>Generates the campaign Report in APEX template.</p>
            </div>
            <div class="apex-logo">
                {f'<img src="data:image/png;base64,{_logo_b64}" />' if _logo_b64 else ''}
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

campaign_name = st.text_input("Campaign Name (partial match is fine)")
find_clicked = st.button("Find Insertion Orders")

if find_clicked:
    if not campaign_name.strip():
        st.error("Enter a campaign name to search for.")
    else:
        with st.spinner("Looking up insertion orders..."):
            try:
                ios_df = list_ios_for_campaign(campaign_name.strip())
            except Exception as exc:  # noqa: BLE001 - surface auth/network errors to the user
                st.error(f"Failed to look up campaign: {exc}")
                ios_df = None
        if ios_df is not None:
            if ios_df.empty:
                st.warning(f"No insertion orders found for campaign name matching {campaign_name!r}.")
                st.session_state.pop("ios_df", None)
            else:
                ios_df.insert(0, "Combine", False)
                st.session_state["ios_df"] = ios_df

if "ios_df" in st.session_state:
    st.write("Select the insertion order(s) to include in the report:")
    edited_df = st.data_editor(
        st.session_state["ios_df"],
        column_config={
            "Combine": st.column_config.CheckboxColumn("Select"),
            "io_id": st.column_config.NumberColumn("Insertion Order ID", format="%d", disabled=True),
            "io_name": st.column_config.TextColumn("IO Name", disabled=True),
            "budget": st.column_config.NumberColumn("Budget", format="%.2f", disabled=True),
            "currency": st.column_config.TextColumn("Currency", disabled=True),
            "guaranteedrate": st.column_config.NumberColumn("Guaranteed Rate", disabled=True),
            "kpi": st.column_config.TextColumn("KPI", disabled=True),
            "product": st.column_config.TextColumn("Product", disabled=True),
            "start_date": st.column_config.DateColumn("Flight Start", disabled=True),
            "end_date": st.column_config.DateColumn("Flight End", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        key="ios_editor",
    )
    selected_io_ids = edited_df.loc[edited_df["Combine"], "io_id"].astype(int).tolist()

    report_mode = st.radio(
        "Report type",
        [COMBINE_MODE, SEPARATE_MODE],
        help=(
            "Combine sums the selected IOs into one report. "
            "Separate creates one workbook with a separate sheet per IO."
        ),
        horizontal=True,
    )

    if report_mode == COMBINE_MODE:
        date_help = "default: the union of the selected IOs' flight dates"
    else:
        date_help = "default: each IO's own flight dates"
    override_dates = st.checkbox(f"Use a custom reporting date range ({date_help})")
    start_date = end_date = None
    if override_dates:
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Reporting start date", value=date.today())
        with col2:
            end_date = st.date_input("Reporting end date", value=date.today())

    generate_clicked = st.button("Generate Report", disabled=not selected_io_ids)
    if not selected_io_ids:
        st.caption("Check at least one row above to enable report generation.")

    if generate_clicked:
        if override_dates and start_date > end_date:
            st.error(f"Start date ({start_date}) is after end date ({end_date}).")
        elif report_mode == COMBINE_MODE:
            _generate_combined(selected_io_ids, start_date, end_date)
        else:
            _generate_separate(selected_io_ids, start_date, end_date)
