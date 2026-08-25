"""
Scheduled runner (Layer 6): generate reports on a fixed schedule defined in a
Google Sheet and upload the .xlsx files to a Google Drive folder.

Designed to run as a Cloud Run job triggered daily by Cloud Scheduler. The
daily trigger is just a heartbeat — which jobs actually run each day is decided
here from each schedule row's `cadence`, so the whole schedule lives in the
Sheet and non-developers can edit it.

Auth: Application Default Credentials (the Cloud Run service account). That
service account needs BigQuery read access, plus the schedule Sheet shared with
it (Viewer) and the destination Drive folder shared with it (Editor). See
docs/SCHEDULING.md.

Env vars:
  SCHEDULE_SHEET_ID    (required) the schedule spreadsheet's ID
  SCHEDULE_SHEET_RANGE (optional) A1 range/tab, default "Schedule!A:Z"
  DRIVE_FOLDER_ID      (required) destination Drive folder ID
  SCHEDULE_TZ          (optional) IANA tz for "today"/day-of-week, default Asia/Singapore
  TEMPLATE_PATH        (optional) default templates/campaign_burst_template.xlsx

Usage:
  uv run dv360-run-scheduled                 # run jobs due today
  uv run dv360-run-scheduled --date 2026-08-26   # pretend today is this date
  uv run dv360-run-scheduled --dry-run       # list due jobs, generate nothing
  uv run dv360-run-scheduled --force         # run every active job regardless of cadence
"""

import argparse
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_TEMPLATE = "templates/campaign_burst_template.xlsx"
DEFAULT_RANGE = "Schedule!A:Z"
DEFAULT_TZ = "Asia/Singapore"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested; no Google APIs).
# --------------------------------------------------------------------------- #
def _parse_bool(value, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"true", "yes", "y", "1", "active"}


def _parse_io_ids(value: str) -> list[int]:
    """Parse a cell like '123, 456' into [123, 456]."""
    if value is None:
        return []
    return [int(p.strip()) for p in str(value).replace(";", ",").split(",") if p.strip()]


def _parse_date(value):
    if value is None or str(value).strip() == "":
        return None
    return date.fromisoformat(str(value).strip()[:10])


def is_due(cadence: str, run_date: date) -> bool:
    """Whether a schedule row runs on `run_date`, given its cadence string:
      - 'daily'
      - 'weekly:Wed'  or  'weekly:Mon,Thu'
      - 'monthly:15'  or  'monthly:last'
    Unknown/blank cadence -> not due (skipped, not an error).
    """
    if not cadence:
        return False
    cadence = str(cadence).strip().lower()
    if cadence == "daily":
        return True
    kind, _, arg = cadence.partition(":")
    arg = arg.strip()
    if kind == "weekly":
        wanted = {_WEEKDAYS[d.strip()[:3]] for d in arg.split(",") if d.strip()[:3] in _WEEKDAYS}
        return run_date.weekday() in wanted
    if kind == "monthly":
        if arg == "last":
            next_month = run_date.replace(day=28) + timedelta(days=4)
            last_dom = (next_month - timedelta(days=next_month.day)).day
            return run_date.day == last_dom
        return arg.isdigit() and run_date.day == int(arg)
    return False


def _render_filename(row: dict, io_ids: list[int], report_end: date) -> str:
    """Output filename: the row's `filename` (with {date}/{io} substitution) or
    a sensible default. Always ends in .xlsx."""
    io_label = "_".join(str(i) for i in io_ids)
    name = (row.get("filename") or "").strip()
    if name:
        name = name.replace("{date}", report_end.isoformat()).replace("{io}", io_label)
    else:
        name = f"report_{io_label}_{report_end.isoformat()}"
    return name if name.lower().endswith(".xlsx") else name + ".xlsx"


def _today(tz_name: str) -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:  # noqa: BLE001 - fall back to UTC if tz data is unavailable
        return datetime.utcnow().date()


# --------------------------------------------------------------------------- #
# Google Sheets / Drive (thin wrappers; exercised only on a live run).
# --------------------------------------------------------------------------- #
def _google_credentials():
    import google.auth

    scopes = [
        "https://www.googleapis.com/auth/bigquery",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.file",
    ]
    creds, _ = google.auth.default(scopes=scopes)
    return creds


def read_schedule(creds, sheet_id: str, sheet_range: str) -> list[dict]:
    """Read the schedule sheet into a list of dict rows keyed by the header row
    (lower-cased). Blank rows are skipped."""
    from googleapiclient.discovery import build

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=sheet_range)
        .execute()
        .get("values", [])
    )
    if not values:
        return []
    header = [h.strip().lower() for h in values[0]]
    rows = []
    for raw in values[1:]:
        if not any(str(c).strip() for c in raw):
            continue
        rows.append({header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))})
    return rows


def upload_to_drive(creds, folder_id: str, file_path: str, file_name: str) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(file_path, mimetype=XLSX_MIME, resumable=False)
    created = (
        service.files()
        .create(
            body={"name": file_name, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,  # required for Shared Drive folders
        )
        .execute()
    )
    return created["id"]


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #
def _generate_report(row: dict, template_path: str, out_dir: str):
    """Run the pipeline for one schedule row; returns (output_path, file_name)."""
    from dv360_pipeline.query import (
        fetch_combined_campaign_burst,
        fetch_separate_campaign_bursts,
    )
    from dv360_pipeline.writer import write_multi_io_report, write_report

    io_ids = _parse_io_ids(row.get("io_ids") or row.get("io_id"))
    if not io_ids:
        raise ValueError("row has no io_ids")
    start = _parse_date(row.get("start"))
    end = _parse_date(row.get("end"))
    mode = (row.get("mode") or "combine").strip().lower()

    if mode == "separate":
        datas = fetch_separate_campaign_bursts(io_ids, start, end)
        report_end = max(d.report_end for d in datas)
        file_name = _render_filename(row, io_ids, report_end)
        out_path = str(Path(out_dir) / file_name)
        write_multi_io_report(datas, template_path, out_path)
    else:
        data = fetch_combined_campaign_burst(io_ids, start, end)
        file_name = _render_filename(row, io_ids, data.report_end)
        out_path = str(Path(out_dir) / file_name)
        write_report(data, template_path, out_path)
    return out_path, file_name


def run(run_date: date, *, dry_run: bool, force: bool) -> int:
    sheet_id = os.environ.get("SCHEDULE_SHEET_ID")
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    sheet_range = os.environ.get("SCHEDULE_SHEET_RANGE", DEFAULT_RANGE)
    template_path = os.environ.get("TEMPLATE_PATH", DEFAULT_TEMPLATE)
    if not sheet_id:
        print("ERROR: SCHEDULE_SHEET_ID is not set", file=sys.stderr)
        return 2
    if not folder_id and not dry_run:
        print("ERROR: DRIVE_FOLDER_ID is not set", file=sys.stderr)
        return 2

    creds = _google_credentials()
    rows = read_schedule(creds, sheet_id, sheet_range)
    due = [r for r in rows if _parse_bool(r.get("active")) and (force or is_due(r.get("cadence"), run_date))]
    print(f"{run_date} ({run_date:%A}): {len(rows)} schedule rows, {len(due)} due"
          + (" [FORCE]" if force else ""))

    failures = 0
    for r in due:
        label = r.get("io_ids") or r.get("io_id") or "?"
        if dry_run:
            print(f"  DUE  io_ids={label} mode={r.get('mode') or 'combine'} cadence={r.get('cadence')}")
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_path, file_name = _generate_report(r, template_path, tmp)
                file_id = upload_to_drive(creds, folder_id, out_path, file_name)
            print(f"  OK   io_ids={label} -> {file_name} (drive id {file_id})")
        except Exception as exc:  # noqa: BLE001 - keep going; report at the end
            failures += 1
            print(f"  FAIL io_ids={label}: {exc}", file=sys.stderr)

    if failures:
        print(f"{failures} job(s) failed", file=sys.stderr)
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="dv360-run-scheduled")
    parser.add_argument("--date", type=lambda s: date.fromisoformat(s), default=None,
                        help="Run as if today were this YYYY-MM-DD (default: today in SCHEDULE_TZ)")
    parser.add_argument("--dry-run", action="store_true", help="List due jobs; generate nothing")
    parser.add_argument("--force", action="store_true", help="Run every active job regardless of cadence")
    args = parser.parse_args()
    run_date = args.date or _today(os.environ.get("SCHEDULE_TZ", DEFAULT_TZ))
    sys.exit(run(run_date, dry_run=args.dry_run, force=args.force))


if __name__ == "__main__":
    main()
