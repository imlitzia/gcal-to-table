"""Fetch Google Calendar events and print them as a copy-pasteable table.

Setup (one-time):
  1. In Google Cloud Console, create a project and enable the "Google Calendar API".
  2. Create an OAuth client ID of type "Desktop app" and download it as credentials.json
     into this same folder.
  3. pip install -r requirements.txt
  4. Run the script once; a browser window will open for you to grant access.
     A token.json will be cached so you won't have to log in again.

Usage:
  python gcal_to_table.py                                  # next 7 days, primary calendar, both formats
  python gcal_to_table.py --days 14                         # next 14 days
  python gcal_to_table.py --start 2026-04-04 --end 2026-04-10   # explicit date range (inclusive)
  python gcal_to_table.py --format slack --copy             # Slack-ready table, copied to clipboard
  python gcal_to_table.py --calendar-id someone@example.com # a single specific calendar
  python gcal_to_table.py --calendar-id a@x.com --calendar-id b@x.com  # several specific calendars
  python gcal_to_table.py --all-calendars                   # every calendar currently shown/checked
                                                              # in your Google Calendar, including ones
                                                              # other people have shared with you
  python gcal_to_table.py --list-calendars                  # see calendar IDs/names to pick from
"""

import argparse
import datetime
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(SCRIPT_DIR, "credentials.json")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "token.json")


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise SystemExit(
                    f"Missing {CREDENTIALS_PATH}. Download an OAuth Desktop app "
                    "credentials.json from Google Cloud Console and place it here."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def build_time_range(days, start, end):
    """Return (time_min, time_max) as RFC3339 strings in the local timezone."""
    tz = local_tz()
    if start or end:
        if not (start and end):
            raise SystemExit("--start and --end must be given together.")
        start_date = datetime.date.fromisoformat(start)
        end_date = datetime.date.fromisoformat(end)
        if end_date < start_date:
            raise SystemExit("--end must not be before --start.")
        time_min = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=tz)
        # end date is inclusive, so the window runs up to the start of the next day
        time_max = datetime.datetime.combine(
            end_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz
        )
    else:
        time_min = datetime.datetime.now(tz)
        time_max = time_min + datetime.timedelta(days=days)
    return time_min.isoformat(), time_max.isoformat()


def list_visible_calendars(service):
    """Calendars currently shown/checked in the user's Google Calendar, including
    calendars other people have shared with them. Mirrors what's visible in the UI:
    selected == True and not hidden."""
    calendars = []
    page_token = None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        for entry in resp.get("items", []):
            if entry.get("selected") and not entry.get("hidden", False):
                calendars.append(entry)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return calendars


def fetch_events(service, calendar_id, time_min, time_max):
    events = []
    page_token = None
    while True:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
        ).execute()
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def event_sort_key(event):
    start = event["start"].get("dateTime", event["start"].get("date"))
    return start


def format_event(event, calendar_label=None):
    start = event["start"].get("dateTime", event["start"].get("date"))
    if "T" in start:
        dt = datetime.datetime.fromisoformat(start)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M")
    else:
        # All-day event. "end.date" is exclusive, so subtract a day to get the
        # last actual day of the event and flag multi-day spans, since those
        # will otherwise show a start date that can be well outside the
        # requested window (Google returns anything overlapping the window).
        date_str = start
        end = event["end"].get("date")
        if end:
            start_d = datetime.date.fromisoformat(start)
            last_d = datetime.date.fromisoformat(end) - datetime.timedelta(days=1)
            if last_d > start_d:
                date_str = f"{start} -> {last_d.isoformat()}"
        time_str = "All day"

    title = event.get("summary", "(No title)")
    location = event.get("location", "")
    if calendar_label is not None:
        return date_str, time_str, title, location, calendar_label
    return date_str, time_str, title, location


def to_notion_table(rows, with_calendar):
    if with_calendar:
        header = "| Date | Time | Event | Location | Calendar |"
        divider = "|------|------|-------|----------|----------|"
    else:
        header = "| Date | Time | Event | Location |"
        divider = "|------|------|-------|----------|"
    lines = [header, divider]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def to_slack_table(rows, with_calendar):
    headers = ["Date", "Time", "Event", "Location"] + (["Calendar"] if with_calendar else [])
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    def fmt_row(vals):
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(vals))

    lines = [fmt_row(headers), fmt_row(["-" * w for w in widths])]
    for row in rows:
        lines.append(fmt_row(list(row)))
    return "```\n" + "\n".join(lines) + "\n```"


def main():
    parser = argparse.ArgumentParser(description="Export Google Calendar events as a table.")
    parser.add_argument("--days", type=int, default=7, help="How many days ahead to fetch (default 7). Ignored if --start/--end are given.")
    parser.add_argument("--start", help="Start date, inclusive, e.g. 2026-04-04. Must be paired with --end.")
    parser.add_argument("--end", help="End date, inclusive, e.g. 2026-04-10. Must be paired with --start.")
    parser.add_argument(
        "--calendar-id",
        action="append",
        default=None,
        help="Calendar ID to include (your own or one shared with you). Can be passed multiple times. "
             "Defaults to 'primary' unless --all-calendars is given.",
    )
    parser.add_argument(
        "--all-calendars",
        action="store_true",
        help="Include every calendar currently shown/checked in your Google Calendar, "
             "including calendars other people have shared with you.",
    )
    parser.add_argument(
        "--list-calendars",
        action="store_true",
        help="List the calendars visible in your Google Calendar (id and name) and exit.",
    )
    parser.add_argument("--format", choices=["notion", "slack", "both"], default="both")
    parser.add_argument("--copy", action="store_true", help="Copy the output to the clipboard")
    args = parser.parse_args()

    service = build("calendar", "v3", credentials=get_credentials())

    if args.list_calendars:
        for entry in list_visible_calendars(service):
            label = entry.get("summaryOverride") or entry.get("summary", "")
            print(f"{entry['id']}\t{label}")
        return

    if args.all_calendars:
        if args.calendar_id:
            raise SystemExit("Use either --calendar-id or --all-calendars, not both.")
        calendar_ids = [c["id"] for c in list_visible_calendars(service)]
        if not calendar_ids:
            print("No visible calendars found.")
            return
    else:
        calendar_ids = args.calendar_id or ["primary"]

    time_min, time_max = build_time_range(args.days, args.start, args.end)

    with_calendar = len(calendar_ids) > 1
    rows = []
    for calendar_id in calendar_ids:
        events = fetch_events(service, calendar_id, time_min, time_max)
        label = calendar_id if with_calendar else None
        rows.extend((event_sort_key(e), format_event(e, label)) for e in events)

    if not rows:
        print("No events found in that range.")
        return

    rows.sort(key=lambda pair: pair[0])
    rows = [r for _, r in rows]

    outputs = []
    if args.format in ("notion", "both"):
        notion_table = to_notion_table(rows, with_calendar)
        outputs.append(notion_table)
        print("--- Notion / Markdown table ---")
        print(notion_table)
        print()
    if args.format in ("slack", "both"):
        slack_table = to_slack_table(rows, with_calendar)
        outputs.append(slack_table)
        print("--- Slack table (paste as-is, keeps the code block) ---")
        print(slack_table)

    if args.copy:
        import pyperclip
        pyperclip.copy(outputs[-1] if len(outputs) == 1 else "\n\n".join(outputs))
        print("\n(Copied to clipboard)")


if __name__ == "__main__":
    main()
