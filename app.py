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

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, "src")

import streamlit as st

from dv360_pipeline.query import CampaignNotFoundError, UnknownKpiError, fetch_campaign_burst
from dv360_pipeline.writer import write_report

TEMPLATE_PATH = "templates/campaign_burst_template.xlsx"

GOLD = "#C9A876"
TEAL = "#1A6B7A"
NAVY_DARK = "#0A0E27"
NAVY_MID = "#12224A"

st.set_page_config(page_title="APEX Campaign Report", page_icon="📊", layout="centered")

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
        text-align: right;
        line-height: 1;
        white-space: nowrap;
    }}
    .apex-logo .wordmark {{
        font-size: 1.8rem;
        font-weight: 800;
        letter-spacing: 1px;
    }}
    .apex-logo .wordmark .ap {{
        color: #1A1A1A;
        -webkit-text-stroke: 0.5px #FFFFFF;
    }}
    .apex-logo .wordmark .x {{
        color: {GOLD};
    }}
    .apex-logo .tagline {{
        font-size: 0.6rem;
        font-weight: 600;
        letter-spacing: 2px;
        color: #DDDDDD;
        margin-top: 0.1rem;
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
                <div class="wordmark"><span class="ap">AP</span><span class="x">X</span></div>
                <div class="tagline">APEX EXCHANGE</div>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.form("report_form"):
    io_id = st.number_input("Insertion Order ID", min_value=1, step=1, format="%d")

    override_dates = st.checkbox(
        "Use a custom reporting date range (default: the IO's full flight dates)"
    )
    start_date = end_date = None
    if override_dates:
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Reporting start date", value=date.today())
        with col2:
            end_date = st.date_input("Reporting end date", value=date.today())

    submitted = st.form_submit_button("Generate Report")

if submitted:
    if not io_id:
        st.error("Enter an Insertion Order ID.")
    elif override_dates and start_date > end_date:
        st.error(f"Start date ({start_date}) is after end date ({end_date}).")
    else:
        try:
            with st.spinner("Fetching data from BigQuery..."):
                data = fetch_campaign_burst(int(io_id), start_date, end_date)
        except CampaignNotFoundError as exc:
            st.error(str(exc))
        except UnknownKpiError as exc:
            st.error(str(exc))
        except ValueError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001 - surface auth/network errors to the user
            st.error(f"Failed to fetch data: {exc}")
        else:
            st.success(f"Fetched data for **{data.meta.campaign_name}** ({data.meta.io_name})")

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Budget", f"${data.meta.budget:,.2f}")
            col2.metric("Spend", f"${data.spend:,.2f}")
            col3.metric("Pace", f"{data.spend / data.meta.budget:.1%}" if data.meta.budget else "N/A")
            col4.metric("KPI", data.meta.kpi)
            st.write(f"Reporting range: **{data.report_start}** to **{data.report_end}**")

            with tempfile.TemporaryDirectory() as tmp_dir:
                output_path = Path(tmp_dir) / f"report_{io_id}_{data.report_start}_{data.report_end}.xlsx"
                write_report(data, TEMPLATE_PATH, str(output_path))
                report_bytes = output_path.read_bytes()

            st.download_button(
                label="Download report (.xlsx)",
                data=report_bytes,
                file_name=f"report_{io_id}_{data.report_start}_{data.report_end}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
