# DV360 Reporting Pipeline — Layers 4 & 5

Layers 1-3 (raw DV360 ingestion, cleaning into BigQuery) are already done and
out of scope here. This repo is Layer 4 (BigQuery query module) and Layer 5
(Excel template writer).

## GCP / BigQuery

- Project: `ssc-apex-apac-prd-mg`
- Dataset: `apex_dv360`
- Tables (conformed breakdown views that union YouTube + Non-YouTube data;
  see `src/dv360_pipeline/query.py` for exact names):
  - **Creative** (`sg_creative_breakdown_v2`): daily grain, one row per
    `insertion_order_id` + `line_item` + `creative` + `date`. Creative name
    is the `creative` column. Carries video **and** audio quartiles.
  - **Demo** (`sg_demo_breakdown_v2`): daily grain, broken out by `gender`
    and `age`. YouTube-only, so it has **no audio** columns.
  - **Device** (`sg_device_breakdown_v2`): daily grain, broken out by
    `device_type`. Carries video and audio quartiles.
  - **campaign_mapping**: one row per IO. Columns: `campaign_name`,
    `io_name`, `io_id`, `Product`, `Currency`, `Budget`, `start_date`,
    `end_date`, `kpi_type`, `Buying_Method`, `guaranteedrate`,
    `KPI_Inventory`. This is the only source of budget/spend-rate info —
    none of the breakdown tables have a cost/spend column. Spend keys on
    `kpi_type` (normalized); `Product` (e.g. DOOH, Demand Gen) fills the
    `CampaignMeta.product` field; `Currency` fills `CampaignMeta.currency`
    (written to cell C8, and used for Budget/Spend display in the app).
    Note the mixed casing (`Budget`, `Product`, `Currency`, `Buying_Method`,
    `KPI_Inventory`) vs the lower-case breakdown/`guaranteedrate` columns;
    the SQL aliases them to lower-case. Some rows have a **null `io_id`** —
    those IOs can't be joined to the breakdown tables, so `list_ios_for_campaign`
    filters them out (`WHERE io_id IS NOT NULL`) to keep them out of the app's
    picker; the real fix is to populate `io_id` in the mapping table. When
    combining IOs across currencies, budget/spend are summed without FX
    conversion and `currency` is reported as `"Mixed"`.

Shared metric columns on the breakdown tables: `impressions`, `clicks`,
`trueview_views`, `video_q25`/`video_q50`/`video_q75`/`video_q100`, and
(creative/device only) `audio_q25`/`audio_q50`/`audio_q75`/`audio_q100`.

Join key across all tables: `insertion_order_id` (creative/demo/device) /
`io_id` (campaign_mapping) — same value, different column name.

Auth: Application Default Credentials (`gcloud auth application-default
login`, or `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service account
JSON). Never commit credentials; `config/` is gitignored for this.

## Key business logic

**Spend formula**: `campaign_mapping.kpi_type` selects both which column to sum
and which multiplier formula to apply — these differ by KPI type, so they're
two separate lookups (`KPI_COLUMN_MAP` and `KPI_SPEND_FORMULA` in
`query.py`). The `kpi` value is normalized (stripped + lower-cased) before
lookup. All formulas are `guaranteed_rate * SUM(column)` except CPM, which
divides by 1000:

| `kpi` (buying method) | column | formula |
|---|---|---|
| `trueview: views` (CPV) | `trueview_views` | `rate * SUM(trueview_views)` |
| `impressions` (CPM) | `impressions` | `rate * SUM(impressions) / 1000` |
| `complete views (video)` (CPCV) | `video_q100` | `rate * SUM(video_q100)` |
| `clicks` (CPC) | `clicks` | `rate * SUM(clicks)` |
| `first-quartile views (video)` | `video_q25` | `rate * SUM(video_q25)` |
| `midpoint views (video)` | `video_q50` | `rate * SUM(video_q50)` |
| `third-quartile views (video)` | `video_q75` | `rate * SUM(video_q75)` |
| `complete listens (audio)` | `audio_q100` | `rate * SUM(audio_q100)` |

An unrecognized `kpi` value raises `UnknownKpiError` rather than silently
computing wrong spend — add mappings in both dicts if a genuinely new KPI
type shows up. Audio KPIs produce zero spend on the demo breakdown (no audio
columns there).

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

**Creative Name** = `creative` column (not `line_item`).

## Template mapping (`templates/campaign_burst_template.xlsx`)

Three sheets: `IO_name` (the actual report), `Data Template` (flat detail
export, best-effort filled), `Sheet1` (static lookup table, untouched).

### IO_name sheet — header fields (fixed cells, never shift)

| Cell | Field | Source |
|---|---|---|
| C6 | Campaign Name | `campaign_mapping.campaign_name` |
| C7 / D7 | Flight start / end | `campaign_mapping.start_date` / `end_date` |
| C8 | Currency | `campaign_mapping.Currency` (template default `SGD`) |
| C9 | Budget | `campaign_mapping.Budget` |
| C10 | Spend | computed (see spend formula) |
| C11 | Guaranteed Rate | `campaign_mapping.guaranteedrate` |
| C12 | KPI | `campaign_mapping.kpi_type` |
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
| Creative | 17 | 19 | creative table | `creative` |
| Targeting | 21 | 26 | creative table | derived from `line_item` |
| Device | 28 | 33 | device table | `device_type` |
| Gender | 35 | 38 | demo table | `gender` |
| Age | 40 | 43 | demo table | `age` |
| Date | 45 | 102 | creative table | `date` |

Columns: B=name/date, C=Impressions, D=TrueView(`trueview_views`), E=Spends,
F=View Rate (formula), G=Clicks, H=CTR (formula). Creative and Targeting
additionally have I/J/K/L = video played to 25/50/75/100%
(`video_q25` / `video_q50` / `video_q75` / `video_q100`).

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
shown `age`/`gender` include an `'Unknown'` bucket in addition to the
expected values — plan for it.

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
`data_template_df` — a date + creative (`creative`) + targeting
(same derivation as the Targeting breakdown) grain query — one row per
distinct date/creative/targeting combination, *not* just one row per day
(the day-only `date_df` has no creative/targeting detail to draw from).
Creative = `creative`, Strategy = derived targeting string. Everything
else (Media Schedule No, Topic, Audience, Channel, Publisher, Placement,
Platform Objective, Rate Type, Media Buy Format, Currency) has no source
in the breakdown tables and is left blank. Scope may expand later.

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
- `KPI_COLUMN_MAP`/`KPI_SPEND_FORMULA` cover the eight known KPI types
  (trueview views, impressions, clicks, the four video quartiles, and audio
  complete listens) — extend both dicts if `campaign_mapping.kpi_type` gets new
  values.
