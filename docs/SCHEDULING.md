# Scheduled reports (Cloud Run + Cloud Scheduler → Google Drive)

Generate reports automatically on a schedule defined in a **Google Sheet**, and
drop the `.xlsx` files into a **Google Drive folder**. A daily Cloud Scheduler
trigger runs a Cloud Run job; the runner (`dv360-run-scheduled`) reads the sheet
and generates only the rows whose `cadence` is due that day, so the whole
schedule is editable in the Sheet with no redeploy.

```
Cloud Scheduler (daily)  ──▶  Cloud Run job  ──▶  reads Schedule Sheet
                                              ──▶  BigQuery (per IO)
                                              ──▶  writes .xlsx
                                              ──▶  uploads to Drive folder
```

## 1. The schedule Google Sheet

Create a sheet with a tab named **`Schedule`** and this header row (column order
doesn't matter; names are matched case-insensitively):

| Column | Required | Example | Meaning |
|---|---|---|---|
| `io_ids` | yes | `1030304653` or `123,456` | one IO, or several for a combined report |
| `cadence` | yes | `weekly:Wed` | `daily` · `weekly:Wed` / `weekly:Mon,Thu` · `monthly:15` · `monthly:last` |
| `mode` | no | `separate` | `separate` (default; a sheet per IO) or `combine` (one summed report) |
| `start` | no | `2026-08-01` | reporting-range start; blank = each IO's flight start |
| `end` | no | `2026-08-31` | reporting-range end; blank = flight end |
| `filename` | no | `NEA_{date}.xlsx` | output name; `{date}` = report end, `{io}` = io ids |
| `active` | no | `TRUE` | `FALSE` to pause a row (default TRUE) |

Then **share the sheet** (Viewer) with the Cloud Run **service account's email**
(e.g. `dv360-ingest@ssc-apex-apac-prd-mg.iam.gserviceaccount.com`). Copy the
sheet ID from its URL: `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

## 2. The Drive destination folder

⚠️ **Use a Shared Drive folder.** A service account has no personal Drive
storage quota, so uploading into a normal "My Drive" folder fails with a quota
error. Create the folder inside a **Shared Drive**, add the service account as a
member (**Content manager**), and copy the folder ID from its URL
(`drive.google.com/drive/folders/<FOLDER_ID>`). The runner passes
`supportsAllDrives=true`.

## 3. Enable APIs & grant roles (one-time)

```bash
gcloud config set project ssc-apex-apac-prd-mg
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  sheets.googleapis.com drive.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com

# The service account needs to read BigQuery (data + run queries).
SA=dv360-ingest@ssc-apex-apac-prd-mg.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding ssc-apex-apac-prd-mg \
  --member="serviceAccount:$SA" --role="roles/bigquery.dataViewer"
gcloud projects add-iam-policy-binding ssc-apex-apac-prd-mg \
  --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"
```
(Sheets/Drive access comes from *sharing* the sheet and folder with `$SA`, not
from IAM roles.)

## 4. Build & deploy the Cloud Run job

```bash
SA=dv360-ingest@ssc-apex-apac-prd-mg.iam.gserviceaccount.com

gcloud run jobs deploy dv360-scheduled-reports \
  --source . \
  --region asia-southeast1 \
  --service-account "$SA" \
  --set-env-vars "SCHEDULE_SHEET_ID=<SHEET_ID>,DRIVE_FOLDER_ID=<FOLDER_ID>,SCHEDULE_TZ=Asia/Singapore" \
  --max-retries 1 --task-timeout 1800
```

Test it before scheduling — a dry run lists what *would* run today, generating
nothing:

```bash
gcloud run jobs execute dv360-scheduled-reports --region asia-southeast1 --args="--dry-run"
# then a real one-off (ignores cadence, runs every active row once):
gcloud run jobs execute dv360-scheduled-reports --region asia-southeast1 --args="--force"
```
Check the execution logs in the Cloud Console, and confirm files appear in the
Drive folder.

## 5. Create the daily trigger

The runner decides per-row what's due, so Cloud Scheduler only needs to fire
once a day (here 07:00 Singapore time):

```bash
gcloud scheduler jobs create http dv360-scheduled-daily \
  --location asia-southeast1 \
  --schedule "0 7 * * *" --time-zone "Asia/Singapore" \
  --uri "https://asia-southeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/ssc-apex-apac-prd-mg/jobs/dv360-scheduled-reports:run" \
  --http-method POST \
  --oauth-service-account-email "$SA"
```

That's it — edit the Sheet any time to add/pause IOs or change cadence; no
redeploy needed. Redeploy (step 4) only when the report *code* changes.

## Local testing

With `local_settings`/ADC pointing at the service account and the env vars set:

```bash
export SCHEDULE_SHEET_ID=... DRIVE_FOLDER_ID=... SCHEDULE_TZ=Asia/Singapore
uv run dv360-run-scheduled --dry-run                # what's due today
uv run dv360-run-scheduled --date 2026-08-26 --dry-run   # what's due on a given day
uv run dv360-run-scheduled --force                  # generate + upload every active row now
```
