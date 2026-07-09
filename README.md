# VI Offline Assets — weekly triage board

Self-serve dashboard for branch triage of cameras offline >168 hours.

**Weekly flow:** upload the `Weekly_Offline_Assets_DDMMYYYY.xlsx` export from the hub as-is (no conversion). The board reads the report date from the title row, strips the branch summary block, and merges into history.

**Triage order built in:** Pickles first (headline + branch split), then longest offline. Branch drill-down from the overview.

**Repair tracking:** Mark fixed per camera → running tally (reported / fixed / remaining). Notes and history are keyed to asset number and resurface automatically if a camera reappears on a later report (repeat-offender ×N badge).

**Persistence (Step 1):** browser localStorage + JSON export/import. Step 2: shared backend via Cloudflare Worker + Google Sheets (same stack as Vision-Stock-Take) so all branches see one live state, behind Cloudflare Access.
