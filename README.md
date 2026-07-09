# VI Offline Assets — weekly triage board (Railway)

Shared self-serve dashboard for branch triage of cameras offline >168 hours.
Flask + SQLite; all fixes, notes and history persist server-side so every branch sees one live board.

## Deploy (Railway)
1. New Project -> Deploy from GitHub repo -> `kavanmehta-pixel/vi-offline-assets`
2. Env vars: `DB_PATH=/data/vi_offline.db`, optionally `APP_PASSCODE=<code>` to gate access
3. Add a Volume mounted at `/data` (persists the DB between deploys — do not skip)
4. Deploy

## Weekly flow
Upload the `Weekly_Offline_Assets_DDMMYYYY.xlsx` hub export as-is. Report date is read
from the title row; the branch summary block is stripped. Cameras are keyed by asset
number — notes and history resurface automatically when a camera reappears (x N badge).

## Migration
Use Import to load a JSON export from the GitHub Pages (localStorage) version.
