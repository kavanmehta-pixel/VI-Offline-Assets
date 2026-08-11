# VI Offline Assets — weekly triage board (Railway)

Shared self-serve dashboard for branch triage of cameras offline >168 hours.
Flask + SQLite; all fixes, notes and history persist server-side so every branch sees one live board.

## ⚠ Required Railway configuration
The sign-in gate is always active (fails closed). The **volume is the critical setting** —
without it every upload is lost on the next redeploy.
`/api/health` reports the live status and the app shows a red banner if either is wrong.

| Setting | Value | Why |
|---|---|---|
| Volume | mounted at `/data` | SQLite lives here; container disk is wiped on every deploy |
| `DB_PATH` | `/data/vi_offline.db` | points SQLite at the volume |
| `APP_PASSCODE` | optional | rotates the password; the gate is always on regardless |
| `ALLOWED_EMAIL_DOMAIN` | `@visioni` (default) | restricts sign-in to VI addresses |

## Deploy (Railway)
1. New Project -> Deploy from GitHub repo -> `kavanmehta-pixel/vi-offline-assets`
2. Env vars: `DB_PATH=/data/vi_offline.db`, `APP_PASSCODE=<shared password>` to gate access (required — leaving it unset makes the board open)
3. Add a Volume mounted at `/data` (persists the DB between deploys — do not skip)
4. Deploy

## Access control
Sign-in requires a Vision Intelligence email address plus the shared password.

Env vars: `APP_PASSCODE` (shared password) and `ALLOWED_EMAIL_DOMAIN` (default `@visioni`).
Neither is stored in this repo. Set both in Railway.
The password is never stored in this repo. Emails are recorded in the `users` table and
attributed to every fix, note, and park action, so the history shows who did what.
Every sign-in, upload, fix, reopen, note, park and unpark is written to the `activity`
table against the signed-in email and shown in the **Activity log** tab, filterable by
user and action. `GET /api/users` returns the access roster; `GET /api/activity` the log.

## Analytics
- **Per-week snapshots** — every upload stores that week's full camera state; the board always renders the newest report date, so historical uploads merge into history without touching the front view.
- **Trends tab** — movement cards (new / boomerangs / carried / cleared), weekly totals chart with in/out flow, chronic-camera leaderboard with presence timelines, branch week-over-week matrix.
- **Drawer** — per-camera weekly presence dots and days-offline trajectory.

## Two independent boards
A switch in the header toggles between the **168-hour** and **48-hour** boards. They share
nothing operationally: separate week history, separate parked/known-issues lists, separate
triage counts. Parking a camera on one board does not affect the other. Reports and the
activity log are common to both.

## Two report streams
The uploader auto-detects which export it's been given — no separate button:

| | 168-hour weekly | 48-hour weekly |
|---|---|---|
| Detected by | `Asset #` header + "as of DATE" title row | `Asset_No` header, row 1 |
| Report date | from the title row | max `latest_snapshot_date` in the file |
| Shown on | main board (Overview / Pickles / All offline) | **48-hour** tab |

Both build independent week-over-week history. The 48-hour board surfaces **early warning**
(under 7 days — not yet on the 168hr board), flags rows whose heartbeat is the hub's
1900-01-01 placeholder (durations excluded from the longest-offline figure), and
**auto-parks** cameras the hub flags as unsuitable-location or not-reliably-reachable
into its own Known issues list — these never touch the 168-hour board. Auto-parks are
badged AUTO and releasable in bulk; a manual park is never overwritten. Branch names are normalised across streams ("Melbourne Cameras" →
"Melbourne") so history doesn't split.

## Fleet size / % offline
The 48-hour export carries an `OnHireAssets` trailer (a label row then the count) after a
blank row below the data. That count is captured per week into the `fleet` table and drives
a **% of fleet offline** tracker in Trends on *both* boards — headline for the week, delta in
percentage points, average / best / worst week, and a weekly line chart. The figure also
appears on each triage strip. The 168-hour export has no fleet count, so it matches the
48-hour figure from the same week (within 6 days); if no 48-hour report has been uploaded,
the tracker explains what's missing rather than showing a wrong number.

## Bulk upload / recovery
The file picker and drop zone both accept **many files at once**. Reports are parsed first,
sorted oldest → newest, then ingested in order, so history rebuilds correctly regardless of
the order you select them. A file that fails to parse is skipped and reported at the end
rather than aborting the batch. Re-uploading a week already present is idempotent.

## Weekly flow
Upload the `Weekly_Offline_Assets_DDMMYYYY.xlsx` hub export as-is. Report date is read
from the title row; the branch summary block is stripped. Cameras are keyed by asset
number — notes and history resurface automatically when a camera reappears (x N badge).

## Migration
Use Import to load a JSON export from the GitHub Pages (localStorage) version.
