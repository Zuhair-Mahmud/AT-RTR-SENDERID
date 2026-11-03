# RTR Sender ID register watcher

Monitors the Austrian RTR open-data register of alphanumeric SMS Sender IDs
(<https://data.rtr.at/pages/open-data/tk-sms>) and reports companies that have
appeared since the previous check.

Source endpoint, documented by RTR in *SMS-Absenderkennungen – Technische Aspekte*:

```
https://data.rtr.at/api/v2/tables/tk-sms?mediaType=json&unpaged=true
```

Add `&id_status=2` for currently valid entries only (`0` = deleted,
`1` = registered/waiting period, `2` = valid). RTR refreshes the register
**once per day**, so a 4-hourly check mostly confirms "no change" — which is
fine, it just means a new registration surfaces within hours of publication.

## Files

| File | What it is |
|---|---|
| `rtr_senderid_watch.py` | The watcher. Python 3.9+, standard library only. |
| `rtr-senderid-watch.yml` | GitHub Actions workflow, runs every 4 hours. Put it in `.github/workflows/`. |
| `state.json` | Created on first run. The baseline of entries already seen. |
| `new_entries.md` / `.json` | The latest report. Overwritten each run. |

## Running it locally

```bash
python3 rtr_senderid_watch.py --inspect     # see the real field names once
python3 rtr_senderid_watch.py --bootstrap   # seed the baseline, report nothing
python3 rtr_senderid_watch.py               # every run after that reports new rows
```

The script does not hardcode RTR's column names. It picks the ID, Sender ID,
company, date and status fields by matching the schema at runtime, so it keeps
working if RTR renames or adds a column. `--inspect` prints the real schema.

Cron on your own machine, every 4 hours:

```
17 */4 * * * cd /path/to/watcher && /usr/bin/python3 rtr_senderid_watch.py >> watch.log 2>&1
```

## Running it on GitHub Actions

1. Create a private repo, add `rtr_senderid_watch.py` and
   `.github/workflows/rtr-senderid-watch.yml`.
2. Run the workflow once manually with **bootstrap = true**. That commits a
   `state.json` baseline so you are not notified about all existing entries.
3. From then on it runs at :17 past every fourth hour and commits any change.

### Notifications (optional)

Set these as repository secrets. Without them the report still lands in the
workflow run summary, the committed `new_entries.md`, and the run artifact.

| Secret | Purpose |
|---|---|
| `SLACK_WEBHOOK_URL` | Posts the new-entry table to a Slack channel |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM`, `MAIL_TO` | Emails the same table |

## Behaviour worth knowing

- **First run never alerts.** An empty state file means baseline mode.
- **An empty API response is treated as a failure**, not as "everything was
  deleted" — the state file is left untouched so one bad response cannot wipe
  the baseline.
- **Identity** is RTR's own record id where present, otherwise a hash of Sender
  ID plus company name. Re-registrations under a new id will show as new.
- **Deletions are not reported**, only additions. The entries stay in
  `state.json` if you ever want to diff removals too.
