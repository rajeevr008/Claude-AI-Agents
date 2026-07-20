"""
Generates AI-written campaign insights from the burst report data, for the
insights box next to the metadata section on the IO_name sheet.

Auth: reads ANTHROPIC_API_KEY from the environment (set it in
local_settings.bat alongside GOOGLE_APPLICATION_CREDENTIALS - never commit it).
"""

import anthropic

from dv360_pipeline.models import CampaignBurstData

SYSTEM_PROMPT = (
    "You are an experienced advertising trader with deep marketing science "
    "knowledge. Based on campaign data, analyze it and give actionable "
    "insights that clients can implement to see measurable results."
)

MODEL = "claude-opus-4-8"


class InsightsError(Exception):
    """Raised when insight generation fails (auth, network, refusal)."""


def _format_campaign_data(data: CampaignBurstData) -> str:
    meta = data.meta
    pace = data.spend / meta.budget if meta.budget else 0
    sections = [
        f"""CAMPAIGN OVERVIEW
Campaign: {meta.campaign_name}
IO: {meta.io_name}
Channel: {meta.channel}
KPI: {meta.kpi}
Guaranteed rate: {meta.guaranteed_rate}
Budget: {meta.budget:,.2f}
Spend to date: {data.spend:,.2f}
Pace (spend/budget): {pace:.1%}
Flight: {meta.flight_start} to {meta.flight_end}
Reporting range: {data.report_start} to {data.report_end}""",
        f"BY CREATIVE\n{data.creative_df.to_string(index=False)}",
        f"BY TARGETING\n{data.targeting_df.to_string(index=False)}",
        f"BY DEVICE\n{data.device_df.to_string(index=False)}",
        f"BY GENDER\n{data.gender_df.to_string(index=False)}",
        f"BY AGE\n{data.age_df.to_string(index=False)}",
        f"BY DATE (daily trend)\n{data.date_df.to_string(index=False)}",
    ]
    return "\n\n".join(sections)


def generate_insights(data: CampaignBurstData) -> str:
    """Call Claude with the campaign data and return insight text for the report."""
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment

    user_message = (
        "Here is the campaign performance data for this reporting period:\n\n"
        f"{_format_campaign_data(data)}\n\n"
        "Write insights for the client report. Requirements:\n"
        "- 4 to 6 short, numbered insights, each 1-2 sentences.\n"
        "- Each insight must be actionable (what to change/do), grounded in a "
        "specific number from the data above.\n"
        "- Cover pacing vs budget, and the strongest/weakest creative, "
        "targeting, device, and demographic segments where the data supports it.\n"
        "- Plain text only, no markdown formatting (this goes into an Excel cell).\n"
    )

    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise InsightsError(
            "Anthropic API key missing or invalid. Set ANTHROPIC_API_KEY in "
            "local_settings.bat (or the environment) and retry."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise InsightsError(f"Could not reach the Anthropic API: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise InsightsError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc

    if response.stop_reason == "refusal":
        raise InsightsError("The model declined to analyze this data.")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise InsightsError("The model returned no insight text.")
    return text
