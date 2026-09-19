# The Screen Time Check

A lead magnet diagnostic for OneRevel. Built by Navvai.

## Read this first, it is the one thing that breaks deploys

**`render.yaml` only applies when it sits at the REPOSITORY ROOT.** If you unzip this into a
subfolder and push that, Render never reads the blueprint, no disk is created, and `DATA_DIR`
points at a mount that does not exist. Two ways to get it right:

1. Unzip so that `app.py` and `render.yaml` are at the top level of the repo, then deploy as a
   **Blueprint**, or
2. Create the service by hand with the commands below and add the disk yourself under **Disks**.

The app will still boot and still capture leads if the disk is missing. It falls back and says so
on the admin sheet. But leads will not survive a redeploy until the disk is mounted.

## Deploy

```
BUILD COMMAND    pip install -r requirements.txt
START COMMAND    gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60
HEALTH CHECK     /healthz
PERSISTENT DISK  /var/data
```

## Environment variables

| Variable | Required | What it enables |
| --- | --- | --- |
| `ADMIN_KEY` | Yes | Unlocks `/admin/leads` and `/admin/leads.csv`. Without it both return 401. `render.yaml` generates one. |
| `DATA_DIR` | No | Where `leads.json` is written. Defaults to `/var/data`. Falls back safely if unwritable. |
| `RESEND_API_KEY` | No | Turns on email delivery of each lead through Resend. |
| `LEAD_TO` | No | The address leads are emailed to. Email only fires when this and `RESEND_API_KEY` are both set. |
| `LEAD_FROM` | No | The from address. Defaults to `onboarding@resend.dev`, which works for testing only. |

## Where leads land

Three places, so no single failure loses one:

1. A `LEAD` line in the Render log, always.
2. `leads.json` on the persistent disk, when storage is writable.
3. An email through Resend, when `RESEND_API_KEY` and `LEAD_TO` are both set.

Storage is a convenience, never a startup dependency. `pick_data_dir()` tries the configured
directory, then `/var/data`, then a folder beside `app.py`, then `/tmp`, printing why each one
failed. If none work the app still serves, still logs and still emails.

## Admin

- `/admin/leads?key=YOUR_KEY` sorted best fit first, with each lead's worst answers, what they did
  not know, and their goal in their own words.
- `/admin/leads.csv?key=YOUR_KEY` the same data as a spreadsheet.

The sheet shows a warning banner when storage is missing or ephemeral, rather than hiding it.

## The page itself

`static/index.html` is one self contained file. It makes no external requests, loads no fonts and
no libraries, and works opened straight from disk. When served it asks `/api/config` on load so the
delivery line tells the truth about where the answers went.
