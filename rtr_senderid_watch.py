#!/usr/bin/env python3
"""
RTR SMS Sender ID register watcher.

Polls the Austrian RTR open-data register of alphanumeric SMS Sender IDs
(https://data.rtr.at/pages/open-data/tk-sms) and reports entries that were not
present the previous time it ran.

Stdlib only - no pip install needed.

Usage
-----
  python3 rtr_senderid_watch.py --bootstrap       # seed state, report nothing
  python3 rtr_senderid_watch.py                   # normal run, report new rows
  python3 rtr_senderid_watch.py --inspect         # print schema + sample row
  python3 rtr_senderid_watch.py --status 2        # only currently-valid entries

Exit codes: 0 = ran fine (new entries or not), 1 = fetch/parse failure.

Optional notification env vars:
  SLACK_WEBHOOK_URL   incoming-webhook URL; a message is posted when new rows appear
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage

API = "https://data.rtr.at/api/v2/tables/tk-sms"
UA = "rtr-senderid-watch/1.0 (open-data monitor)"

# Field names are guessed at runtime, so the script keeps working if RTR
# renames or adds columns. These are the preference orders.
ID_HINTS = ("id", "nummer", "lfd")
SENDER_HINTS = ("absenderkennung", "kennung", "senderid", "sender")
COMPANY_HINTS = ("firma", "unternehmen", "inhaber", "name", "company")
DATE_HINTS = ("datum", "eintrag", "created", "gueltig", "valid")
STATUS_HINTS = ("status",)


def build_url(status=None, media="json"):
    url = f"{API}?mediaType={media}&unpaged=true"
    if status is not None:
        url += f"&id_status={status}"
    return url


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def extract_rows(payload):
    """RTR wraps rows differently per endpoint version; find the list of dicts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "content", "items", "results", "rows"):
            val = payload.get(key)
            if isinstance(val, list) and (not val or isinstance(val[0], dict)):
                return val
        for val in payload.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    raise ValueError("Could not find a list of records in the API response")


def pick_field(fields, hints):
    lowered = {f.lower(): f for f in fields}
    for hint in hints:
        for low, original in lowered.items():
            if low == hint:
                return original
    for hint in hints:
        for low, original in lowered.items():
            if hint in low:
                return original
    return None


def row_key(row, id_field, sender_field, company_field):
    """Stable identity for a register entry."""
    if id_field and row.get(id_field) not in (None, ""):
        return f"id:{row[id_field]}"
    parts = [str(row.get(f, "")) for f in (sender_field, company_field) if f]
    if any(parts):
        return "sc:" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
    blob = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return "h:" + hashlib.sha1(blob.encode()).hexdigest()[:16]


def load_state(path):
    if not os.path.exists(path):
        return {"version": 1, "first_run": None, "last_run": None, "keys": {}}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def summarise(row, fields):
    sender, company, date, status = fields
    out = {}
    for label, field in (("sender_id", sender), ("company", company),
                         ("registered", date), ("status", status)):
        if field and row.get(field) not in (None, ""):
            out[label] = row[field]
    return out


def to_markdown(new_rows, fields, total, when):
    sender, company, date, status = fields
    lines = [f"## RTR Sender ID register - {len(new_rows)} new entr{'y' if len(new_rows) == 1 else 'ies'}",
             f"Checked {when} - register now holds {total} entries.", ""]
    if not new_rows:
        lines.append("No new registrations since the last check.")
        return "\n".join(lines)
    header = [h for h in ("Sender ID", "Company", "Registered", "Status") if h]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for row in new_rows:
        cells = [str(row.get(f, "")) if f else "" for f in (sender, company, date, status)]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return "\n".join(lines)


def notify_slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:  # notification failure must not fail the run
        print(f"[warn] Slack notification failed: {exc}", file=sys.stderr)


def notify_email(subject, text):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("MAIL_TO")
    if not host or not to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM", os.environ.get("SMTP_USER", "rtr-watch@localhost"))
    msg["To"] = to
    msg.set_content(text)
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:
        print(f"[warn] Email notification failed: {exc}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Watch the RTR SMS Sender ID register for new entries")
    ap.add_argument("--state", default="state.json", help="state file path (default: state.json)")
    ap.add_argument("--out", default="new_entries", help="basename for the .json/.md report")
    ap.add_argument("--status", default=None, help="filter id_status (2 = currently valid)")
    ap.add_argument("--bootstrap", action="store_true", help="seed the state file without reporting")
    ap.add_argument("--inspect", action="store_true", help="print field names and one sample row")
    args = ap.parse_args()

    url = build_url(args.status)
    try:
        rows = extract_rows(fetch(url))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError) as exc:
        print(f"[error] could not fetch {url}: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("[error] API returned zero rows - refusing to overwrite state", file=sys.stderr)
        return 1

    field_names = list(rows[0].keys())
    if args.inspect:
        print("Fields:", ", ".join(field_names))
        print(json.dumps(rows[0], ensure_ascii=False, indent=2))
        return 0

    id_field = pick_field(field_names, ID_HINTS)
    sender_field = pick_field(field_names, SENDER_HINTS)
    company_field = pick_field(field_names, COMPANY_HINTS)
    date_field = pick_field(field_names, DATE_HINTS)
    status_field = pick_field(field_names, STATUS_HINTS)
    fields = (sender_field, company_field, date_field, status_field)

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    state = load_state(args.state)
    known = state.get("keys", {})
    first_run = not known

    new_rows, seen = [], {}
    for row in rows:
        key = row_key(row, id_field, sender_field, company_field)
        if key in known:
            seen[key] = known[key]
        else:
            seen[key] = {"first_seen": now, **summarise(row, fields)}
            if not first_run and not args.bootstrap:
                new_rows.append(row)

    state.update({"version": 1, "last_run": now, "keys": seen,
                  "first_run": state.get("first_run") or now,
                  "source": url, "total": len(rows)})
    save_state(args.state, state)

    report = to_markdown(new_rows, fields, len(rows), now)
    with open(f"{args.out}.md", "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    with open(f"{args.out}.json", "w", encoding="utf-8") as fh:
        json.dump({"checked_at": now, "source": url, "total": len(rows),
                   "new": [summarise(r, fields) | {"raw": r} for r in new_rows]},
                  fh, ensure_ascii=False, indent=1)

    print(report)

    if first_run or args.bootstrap:
        print(f"\n[info] baseline seeded with {len(rows)} entries; the next run reports changes.")
    elif new_rows:
        notify_slack(report)
        notify_email(f"RTR register: {len(new_rows)} new Sender ID registration(s)", report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
