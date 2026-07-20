# DV360 Reporting Pipeline — Layers 4 & 5 (+ AI Insights)

Layers 1-3 (raw DV360 ingestion, cleaning into BigQuery) are already done and
out of scope here. This repo is Layer 4 (BigQuery query module) and Layer 5
(Excel template writer). This "Insights" variant additionally generates
AI-written campaign insights (via the Claude API) into the report.

## AI Insights (`src/dv360_pipeline/insights.py`)

`generate_insights(data)` sends the full burst data (metadata + pacing + all
six breakdowns) to Claude (`claude-opus-4-8`) with a system prompt casting it
as an experienced advertising trader, and returns 4-6 numbered, actionable,
plain-text insights each grounded in a specific metric. `write_report(...,
insights=...)` places them in a merged block at `F6:L20` on the `IO_name`
sheet (right of the metadata, above the breakdown sections so row resizing
never shifts them; verified free of template merged cells). The app has a
"Generate AI insights" checkbox (default on); if the API call fails
(`InsightsError` — missing/invalid key, network, refusal) the report is still
written **without** insights rather than failing. Auth: `ANTHROPIC_API_KEY`
env var (add it to `local_settings.bat` alongside the GCP key — never commit).

## GCP / BigQuery

- Project: `ssc-apex-apac-prd-mg`
- Dataset: `apex_dv360`
- Tables (see `src/dv360_pipeline/query.py` for exact names — they have long
  auto-generated suffixes):
  - **Creative** (`SG_creative_rpt_...`): daily grain, one row per
    `insertion_order_id` + `line_item_id` + `trueview_ad` + `date`.
  - **Demo** (`sg_demo_aiagent_...`): daily grain, broken out by
    `youtube_gender` and `youtube_age`.
  - **Device** (`sg_device_rpt_...`): daily grain, broken out by
    `device_type` (values seen: Connected TV, Smart Phone, Tablet, Desktop).
  - **campaign_mapping**: one row per IO. Columns: `campaign_name`,
    `io_name`, `io_id`, `budget`, `guaranteedrate`, `kpi`, `channel`,
    `start_date`, `end_date`. This is the only source of budget/spend-rate
    info — none of the three reporting tables have a cost/spend column.

Join key across all tables: `insertion_order_id` (creative/demo/device) /
`io_id` (campaign_mapping) — same value, different column name.

Auth: Application Default Credentials (`gcloud auth application-default
login`, or `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service account
JSON). Never commit credentials; `config/` is gitignored for this.

## Key business logic

**Spend formula**: `campaign_mapping.kpi` selects both which column to sum
and which multiplier formula to apply — these differ by KPI type, so they're
two separate lookups (`KPI_COLUMN_MAP` and `KPI_SPEND_FORMULA` in
`query.py`):
- `kpi='clicks'` → `guaranteed_rate * SUM(clicks)`
- `kpi='views'` → `guaranteed_rate * SUM(youtube_views)`
- `kpi='impressions'` → `guaranteed_rate * SUM(impressions) / 1000` (CPM)
- `kpi='completed views'` → `guaranteed_rate * SUM(rich_media_video_completions)`

An unrecognized `kpi` value raises `UnknownKpiError` rather than silently
computing wrong spend — add mappings in both dicts if a genuinely new KPI
type shows up.

Spend is computed **per row** in every breakdown (creative/targeting/device/
gender/age/date), not just as a single total — each row's spend uses that
row's own KPI-column value times the guaranteed rate.

**Targeting** has no direct source column. It's derived from `line_item`:
the last `-`-delimited segment (e.g. `"...-Demand Gen-Interest"` →
`"Interest"`), computed in SQL via `SPLIT(line_item, '-')[OFFSET(...)]`.
Sourced from the creative table (grouped by the derived targeting string,
summed like the other creative-level breakdowns).

**Two distinct date concepts** — don't conflate them:
- **Flight dates** (`campaign_mapping.start_date`/`end_date`): the IO's
  full contracted flight, looked up automatically, not a function
  parameter.
- **Reporting date range**: the window for *this* burst report, passed
  explicitly to `fetch_campaign_burst(io_id, start_date, end_date)`. May be
  a sub-range of the flight (e.g. weekly report within a monthly IO).

**Creative Name** = `trueview_ad` column (not `line_item`).

## Template mapping (`templates/campaign_burst_template.xlsx`)

Three sheets: `IO_name` (the actual report), `Data Template` (flat detail
export, best-effort filled), `Sheet1` (static lookup table, untouched).

### IO_name sheet — header fields (fixed cells, never shift)

| Cell | Field | Source |
|---|---|---|
| C6 | Campaign Name | `campaign_mapping.campaign_name` |
| C7 / D7 | Flight start / end | `campaign_mapping.start_date` / `end_date` |
| C9 | Budget | `campaign_mapping.budget` |
| C10 | Spend | computed (see spend formula) |
| C11 | Guaranteed Rate | `campaign_mapping.guaranteedrate` |
| C12 | KPI | `campaign_mapping.kpi` |
| C14 / D14 | Reporting Date Range start / end | function params |
| E7 | Pace | template formula `=C10/C9`, untouched |
| E9 | Ideal | template formula `=(D14-C14)/(D7-C7)`, untouched |

Pace/Ideal are left as native Excel formulas — their inputs are fixed
single cells that never move, so the formulas stay valid regardless of how
the breakdown sections below grow or shrink.

### IO_name sheet — breakdown sections (dynamically resized)

Six sections, each: header row → N data rows → Total row. Original
template row anchors (before any resizing):

| Section | Header | Total (template) | Source | Group by |
|---|---|---|---|---|
| Creative | 17 | 19 | creative table | `trueview_ad` |
| Targeting | 21 | 26 | creative table | derived from `line_item` |
| Device | 28 | 33 | device table | `device_type` |
| Gender | 35 | 38 | demo table | `youtube_gender` |
| Age | 40 | 43 | demo table | `youtube_age` |
| Date | 45 | 102 | creative table | `date` |

Columns: B=name/date, C=Impressions, D=TrueView(`youtube_views`), E=Spends,
F=View Rate (formula), G=Clicks, H=CTR (formula). Creative and Targeting
additionally have I/J/K/L = video played to 25/50/75/100%
(`rich_media_video_first_quartile_completes` / `_midpoints` /
`_third_quartile_completes` / `_completions`).

**Row-level data is written as static values** (source data, nothing to
derive). **Per-row View Rate/CTR are Excel formulas** (`=D{r}/C{r}`,
`=G{r}/C{r}`) — safe single-row refs. **Total rows are `SUM()` formulas**,
always rewritten with the row range matching the section's *actual* current
size — never left as-is, even for sections that don't get resized, because
earlier sections resizing shifts everything below them and openpyxl does
not auto-adjust formula text elsewhere in the sheet when rows are inserted.

Age/Gender/Creative/Device/Targeting sections are resized to fit the exact
number of distinct values found (`ws.insert_rows()`/`ws.delete_rows()`),
cascading an offset through every section processed after it. Real data has
shown `youtube_age`/`youtube_gender` include an `'Unknown'` bucket in
addition to the expected values — plan for it.

The Date section's template is pre-formatted for 56 rows (fits ~8 weeks of
daily data) with number formats/borders already applied — normal reporting
windows fit inside without any resizing. Only extended if the date range
exceeds 56 days.

Implementation: `src/dv360_pipeline/writer.py`, see `_write_section()` /
`_resize_section()`.

### Data Template sheet

A subset of its 24 columns have a real source and are filled: Date,
Campaign Name, Creative, Strategy, Cost, Impressions, Clicks, Video Views,
and the four video-completion-quartile columns. Sourced from
`data_template_df` — a date + creative (`trueview_ad`) + targeting
(same derivation as the Targeting breakdown) grain query — one row per
distinct date/creative/targeting combination, *not* just one row per day
(the day-only `date_df` has no creative/targeting detail to draw from).
Creative = `trueview_ad`, Strategy = derived targeting string. Everything
else (Media Schedule No, Topic, Audience, Channel, Publisher, Placement,
Platform Objective, Rate Type, Media Buy Format, Currency) has no source
in the three reporting tables and is left blank. Scope may expand later.

### Sheet1

Static lookup table (targeting-code → label). Not written to.

## Running it

```
uv sync
uv run scripts/discover_schema.py                          # re-run if source tables/columns change
uv run scripts/test_layer4.py <io_id> <start> <end>         # Layer 4 only, prints DataFrames
uv run scripts/test_layer5.py <io_id> <start> <end> [out]   # full pipeline, writes .xlsx

uv run dv360-report <io_id>                                 # production CLI: full flight range
uv run dv360-report <io_id> --start <start> --end <end>     # sub-range burst (e.g. weekly report)
```

The CLI (`src/dv360_pipeline/cli.py`) defaults the reporting range to the
IO's full flight dates from `campaign_mapping` when `--start`/`--end` are
omitted; both must be given together if overriding.

Auth via `gcloud auth application-default login` or
`GOOGLE_APPLICATION_CREDENTIALS` env var (Windows: `set` in cmd.exe,
`$env:` in PowerShell — don't mix shells).

## Known gaps / not yet built

- No orchestration/scheduling (Layer 6 in the original architecture diagram
  — Cloud Composer, monitoring, human review) — explicitly out of scope for
  this repo.
- `KPI_COLUMN_MAP` only has `clicks`/`views`/`impressions` — extend if
  `campaign_mapping.kpi` gets new values.
