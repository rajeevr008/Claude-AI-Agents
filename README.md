# DV360 Reporting Pipeline — Layers 4 & 5

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

## Stage 1 — discovery

```
uv run scripts/discover_schema.py     # prints schema + sample rows for the 3 BQ tables
uv run scripts/inspect_template.py    # prints template sheet structure
```

See `CLAUDE.md` for the confirmed schema, template mapping, and
formula-handling decisions once Stage 1 is complete.
