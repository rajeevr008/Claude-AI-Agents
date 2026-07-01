# DV360 Reporting Pipeline — AI Agents

Layers 1-3 (raw DV360 ingestion, cleaning, BigQuery storage) are already done.
This repo builds:

- **Layer 4**: query module that pulls creative, demo, and device data for a
  campaign burst from BigQuery into pandas DataFrames.
- **Layer 5**: writer that fills the Excel report template from that data.

## Setup

```
uv sync
```

Auth: either run `gcloud auth application-default login`, or set
`GOOGLE_APPLICATION_CREDENTIALS` to point at a service account JSON file
(keep it outside the repo, or under `config/` which is gitignored).

## Generating a report

```
uv run dv360-report <io_id>                                 # CLI: full flight range
uv run dv360-report <io_id> --start <start> --end <end>     # CLI: sub-range burst

uv run streamlit run app.py                                 # web app, opens in browser
```

On Windows, double-click `run_app.bat` instead of using the CLI — it opens
the same Streamlit app in your browser (requires `uv` to already be
installed, and `GOOGLE_APPLICATION_CREDENTIALS` set as a permanent
environment variable, since a double-clicked script won't inherit a
one-off `set`/`$env:` from a terminal session).

## Stage 1 — discovery

```
uv run scripts/discover_schema.py     # prints schema + sample rows for the 3 BQ tables
uv run scripts/inspect_template.py    # prints template sheet structure
```

See `CLAUDE.md` for the confirmed schema, template mapping, and
formula-handling decisions once Stage 1 is complete.
