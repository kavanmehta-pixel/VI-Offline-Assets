import json
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory, make_response

DB_PATH = os.environ.get("DB_PATH", "vi_offline.db")
# The sign-in gate is ALWAYS on. Setting APP_PASSCODE in Railway overrides this default
# and is the recommended way to rotate the password without a code change.
DEFAULT_PASSCODE = "Visioni2026!"
PASSCODE = os.environ.get("APP_PASSCODE") or DEFAULT_PASSCODE
USING_DEFAULT_PASSCODE = not os.environ.get("APP_PASSCODE")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "@visioni").lower()  # substring match on email

app = Flask(__name__, static_folder=None)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row  # rows must be dict-like for the migrations below
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS weeks(
            week TEXT PRIMARY KEY,
            uploaded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assets(
            id TEXT PRIMARY KEY,
            snap TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS appearances(
            asset TEXT NOT NULL,
            week TEXT NOT NULL,
            PRIMARY KEY(asset, week)
        );
        CREATE TABLE IF NOT EXISTS fixes(
            asset TEXT NOT NULL,
            week TEXT NOT NULL,
            ts TEXT NOT NULL,
            PRIMARY KEY(asset, week)
        );
        CREATE TABLE IF NOT EXISTS weeks48(
            week TEXT PRIMARY KEY,
            uploaded_at TEXT NOT NULL,
            filename TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS assets48(
            id TEXT PRIMARY KEY,
            snap TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS appearances48(
            asset TEXT NOT NULL,
            week TEXT NOT NULL,
            PRIMARY KEY(asset, week)
        );
        CREATE TABLE IF NOT EXISTS snapshots48(
            asset TEXT NOT NULL,
            week TEXT NOT NULL,
            snap TEXT NOT NULL,
            PRIMARY KEY(asset, week)
        );
        CREATE TABLE IF NOT EXISTS fleet(
            week TEXT PRIMARY KEY,
            on_hire INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS meta(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activity(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            email TEXT NOT NULL,
            action TEXT NOT NULL,
            asset TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS users(
            email TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            logins INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS parked(
            asset TEXT NOT NULL,
            stream TEXT NOT NULL DEFAULT 'w168',
            reason TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            parked_at TEXT NOT NULL,
            auto INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(asset, stream)
        );
        CREATE TABLE IF NOT EXISTS report_files(
            week TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            data BLOB NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots(
            asset TEXT NOT NULL,
            week TEXT NOT NULL,
            snap TEXT NOT NULL,
            PRIMARY KEY(asset, week)
        );
        CREATE TABLE IF NOT EXISTS comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL,
            ts TEXT NOT NULL,
            text TEXT NOT NULL,
            by_email TEXT NOT NULL DEFAULT ''
        );
        """
    )
    con.commit()
    # --- migration: auto-park flag (safe on re-deploy) ---
    try:
        con.execute("ALTER TABLE parked ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
        con.commit()
    except sqlite3.OperationalError:
        pass
    # --- migration: scope parks to a stream so the two boards stay independent ---
    pcols = [r[1] for r in con.execute("PRAGMA table_info(parked)")]
    if pcols and "stream" not in pcols:
        con.execute("ALTER TABLE parked RENAME TO parked_legacy")
        con.execute(
            "CREATE TABLE parked(asset TEXT NOT NULL, stream TEXT NOT NULL DEFAULT 'w168',"
            " reason TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', parked_at TEXT NOT NULL,"
            " auto INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(asset, stream))")
        # auto-parks could only have come from the 48hr ingest; manual parks were on the 168hr board
        con.execute(
            "INSERT INTO parked(asset, stream, reason, note, parked_at, auto)"
            " SELECT asset, CASE WHEN COALESCE(auto,0)=1 THEN 'w48' ELSE 'w168' END,"
            " reason, note, parked_at, COALESCE(auto,0) FROM parked_legacy")
        con.execute("DROP TABLE parked_legacy")
        con.commit()
    # --- migration: attribution column on comments (safe on re-deploy) ---
    try:
        con.execute("ALTER TABLE comments ADD COLUMN by_email TEXT NOT NULL DEFAULT ''")
        con.commit()
    except sqlite3.OperationalError:
        pass  # column already present
    # --- migration: backfill snapshots from assets + appearances ---
    missing = con.execute(
        "SELECT a.asset, a.week, s.snap "
        "FROM appearances a "
        "LEFT JOIN snapshots s ON a.asset=s.asset AND a.week=s.week "
        "JOIN assets t ON a.asset=t.id "
        "WHERE s.snap IS NULL"
    ).fetchall()
    if missing:
        import json as _json
        # build week list for days-offline estimation
        weeks_list = sorted(set(r[1] for r in missing))
        week_idx = {w: i for i, w in enumerate(weeks_list)}
        all_weeks = sorted(
            r[0] for r in con.execute("SELECT week FROM weeks").fetchall()
        )
        latest_idx = {w: i for i, w in enumerate(all_weeks)}
        latest_week = all_weeks[-1] if all_weeks else None
        snap_cache = {}
        for asset, week, _ in missing:
            if asset not in snap_cache:
                row = con.execute("SELECT snap FROM assets WHERE id=?", (asset,)).fetchone()
                snap_cache[asset] = _json.loads(row[0]) if row else {}
            base = snap_cache[asset]
            if not base:
                continue
            # estimate days offline for this historical week
            snap = dict(base)
            if latest_week and week != latest_week:
                li = latest_idx.get(latest_week, 0)
                wi = latest_idx.get(week, 0)
                weeks_back = li - wi
                est_days = max(1, snap.get("daysOffline", 0) - weeks_back * 7)
                snap["daysOffline"] = est_days
                snap["hoursOffline"] = est_days * 24
            con.execute(
                "INSERT OR IGNORE INTO snapshots(asset, week, snap) VALUES(?,?,?)",
                (asset, week, _json.dumps(snap)),
            )
        con.commit()
    # --- boot tracking: proves whether the DB survives redeploys ---
    row = con.execute("SELECT value FROM meta WHERE key='created_at'").fetchone()
    if not row:
        con.execute("INSERT INTO meta(key, value) VALUES('created_at', ?)", (now(),))
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('boot_count', '1')")
    else:
        b = con.execute("SELECT value FROM meta WHERE key='boot_count'").fetchone()
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('boot_count', ?)",
                    (str(int(b["value"]) + 1) if b else "1",))
    con.commit()
    con.close()


def storage_status():
    """Is the SQLite file on a persistent volume, or ephemeral container disk?"""
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
    db_abs = os.path.abspath(DB_PATH)
    persistent = bool(mount) and db_abs.startswith(os.path.abspath(mount))
    return {"dbPath": db_abs, "volumeMount": mount or None, "persistent": persistent}


def cur_user():
    return request.cookies.get('vi_user', 'unknown')


def now():
    return datetime.now(timezone.utc).isoformat()


def log_action(action, asset="", detail=""):
    """Append to the audit trail. Never raises — logging must not break a write."""
    try:
        db().execute(
            "INSERT INTO activity(ts, email, action, asset, detail) VALUES(?,?,?,?,?)",
            (now(), cur_user(), action, asset, detail))
    except sqlite3.Error:
        pass


def valid_email(e):
    e = (e or "").strip().lower()
    if len(e) < 6 or " " in e or e.count("@") != 1:
        return False
    local, _, domain = e.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    return ALLOWED_DOMAIN in e if ALLOWED_DOMAIN else True  # domain gate


def authed():
    if request.cookies.get("vi_pass") != PASSCODE:
        return False
    return valid_email(request.cookies.get("vi_user", ""))


def require_auth(f):
    @wraps(f)
    def w(*a, **k):
        if not authed():
            return jsonify({"error": "unauthorized"}), 401
        return f(*a, **k)

    return w


@app.get("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/api/auth")
def auth_status():
    return jsonify({"required": True, "ok": authed(),
                    "email": request.cookies.get("vi_user", "")})


@app.post("/api/auth")
def auth_login():
    p = request.get_json(silent=True) or {}
    code = p.get("code", "")
    email = (p.get("email") or "").strip().lower()
    if not valid_email(email):
        return jsonify({"ok": False,
                        "error": f"Access is limited to {ALLOWED_DOMAIN} email addresses."
                        if ALLOWED_DOMAIN else "Enter a valid email address."}), 400
    if code != PASSCODE:
        return jsonify({"ok": False, "error": "Incorrect password."}), 401
    d = db()
    if d.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        d.execute("UPDATE users SET last_seen=?, logins=logins+1 WHERE email=?", (now(), email))
    else:
        d.execute("INSERT INTO users(email, first_seen, last_seen, logins) VALUES(?,?,?,1)",
                  (email, now(), now()))
    d.commit()
    d.execute("INSERT INTO activity(ts, email, action, asset, detail) VALUES(?,?,?,?,?)",
              (now(), email, "signin", "", ""))
    d.commit()
    resp = make_response(jsonify({"ok": True, "email": email}))
    resp.set_cookie("vi_pass", code, max_age=180 * 24 * 3600, httponly=False, samesite="Lax")
    resp.set_cookie("vi_user", email, max_age=180 * 24 * 3600, httponly=False, samesite="Lax")
    return resp


@app.get("/api/state")
@require_auth
def state():
    d = db()
    weeks = {r["week"]: {"uploadedAt": r["uploaded_at"], "ids": []} for r in d.execute("SELECT * FROM weeks")}
    assets = {}
    for r in d.execute("SELECT * FROM assets"):
        assets[r["id"]] = {"snap": json.loads(r["snap"]), "appearances": [], "fixes": [], "comments": [], "days": {}}
    # authoritative snap = snapshot from the latest week the asset appeared; also build days-offline series
    for r in d.execute(
        "SELECT s.asset, s.week, s.snap FROM snapshots s ORDER BY s.week"
    ):
        if r["asset"] not in assets:
            assets[r["asset"]] = {"snap": {}, "appearances": [], "fixes": [], "comments": [], "days": {}}
        snap = json.loads(r["snap"])
        assets[r["asset"]]["snap"] = snap  # ordered by week asc -> ends at latest
        assets[r["asset"]]["days"][r["week"]] = snap.get("daysOffline", 0)
    for r in d.execute("SELECT * FROM appearances ORDER BY week"):
        if r["asset"] in assets:
            assets[r["asset"]]["appearances"].append(r["week"])
        if r["week"] in weeks:
            weeks[r["week"]]["ids"].append(r["asset"])
    for r in d.execute("SELECT * FROM fixes"):
        if r["asset"] in assets:
            assets[r["asset"]]["fixes"].append({"week": r["week"], "date": r["ts"]})
    for r in d.execute("SELECT * FROM comments ORDER BY ts"):
        if r["asset"] in assets:
            assets[r["asset"]]["comments"].append({
                "ts": r["ts"], "text": r["text"],
                "by": (r["by_email"] if "by_email" in r.keys() else "") or ""})
    parked = {"w168": {}, "w48": {}}
    for r in d.execute("SELECT * FROM parked"):
        st = r["stream"] if "stream" in r.keys() else "w168"
        parked.setdefault(st, {})[r["asset"]] = {
            "reason": r["reason"], "note": r["note"], "parkedAt": r["parked_at"],
            "auto": bool(r["auto"])}
    w48 = {r["week"]: {"uploadedAt": r["uploaded_at"], "filename": r["filename"], "ids": []}
           for r in d.execute("SELECT * FROM weeks48")}
    a48 = {}
    for r in d.execute("SELECT * FROM assets48"):
        a48[r["id"]] = {"snap": json.loads(r["snap"]), "appearances": [], "days": {}}
    for r in d.execute("SELECT * FROM snapshots48 ORDER BY week"):
        if r["asset"] not in a48:
            a48[r["asset"]] = {"snap": {}, "appearances": [], "days": {}}
        snap = json.loads(r["snap"])
        a48[r["asset"]]["snap"] = snap
        a48[r["asset"]]["days"][r["week"]] = snap.get("daysOffline", 0)
    for r in d.execute("SELECT * FROM appearances48 ORDER BY week"):
        if r["asset"] in a48:
            a48[r["asset"]]["appearances"].append(r["week"])
        if r["week"] in w48:
            w48[r["week"]]["ids"].append(r["asset"])
    cur = max(weeks.keys()) if weeks else None
    fleet = {r["week"]: r["on_hire"] for r in d.execute("SELECT * FROM fleet")}
    cur48 = max(w48.keys()) if w48 else None
    return jsonify({"currentWeek": cur, "weeks": weeks, "assets": assets, "parked": parked,
                    "w48": {"currentWeek": cur48, "weeks": w48, "assets": a48},
                    "fleet": fleet})


@app.post("/api/ingest")
@require_auth
def ingest():
    p = request.get_json(force=True)
    week, rows = p.get("reportDate"), p.get("assets", [])
    file_b64 = p.get("fileBase64")
    file_name = p.get("fileName", "")
    if not week or not rows:
        return jsonify({"error": "reportDate and assets required"}), 400
    d = db()
    d.execute("INSERT OR REPLACE INTO weeks(week, uploaded_at) VALUES(?,?)", (week, now()))
    if file_b64:
        import base64
        raw = base64.b64decode(file_b64)
        d.execute("INSERT OR REPLACE INTO report_files(week, filename, data, size) VALUES(?,?,?,?)",
                  (week, file_name, raw, len(raw)))
    reappeared = []
    for a in rows:
        aid = a.get("id")
        if not aid:
            continue
        prev = d.execute("SELECT 1 FROM assets WHERE id=?", (aid,)).fetchone()
        had_fix = d.execute("SELECT 1 FROM fixes WHERE asset=? LIMIT 1", (aid,)).fetchone()
        seen = d.execute("SELECT 1 FROM appearances WHERE asset=? AND week=?", (aid, week)).fetchone()
        if prev and had_fix and not seen:
            reappeared.append(aid)
        d.execute("INSERT OR REPLACE INTO snapshots(asset, week, snap) VALUES(?,?,?)", (aid, week, json.dumps(a)))
        d.execute("INSERT OR IGNORE INTO appearances(asset, week) VALUES(?,?)", (aid, week))
        # keep assets.snap as the snapshot from the LATEST week this asset appeared
        latest = d.execute("SELECT MAX(week) w FROM appearances WHERE asset=?", (aid,)).fetchone()["w"]
        if latest == week:
            d.execute("INSERT OR REPLACE INTO assets(id, snap) VALUES(?,?)", (aid, json.dumps(a)))
    log_action("upload", "", f"report {week} — {len(rows)} cameras"
               + (f", {len(reappeared)} repeat" if reappeared else ""))
    d.commit()
    return jsonify({"ok": True, "week": week, "count": len(rows), "reappeared": reappeared})


@app.post("/api/ingest48")
@require_auth
def ingest48():
    """48-hour weekly snapshot. Same week-history model as the 168hr stream, separate stream."""
    p = request.get_json(force=True)
    rows = p.get("assets", [])
    week = (p.get("reportDate") or "").strip()
    if not rows:
        return jsonify({"error": "assets required"}), 400
    if not week:
        # report date = latest snapshot date present in the file
        week = max((a.get("snapshotDate") or "") for a in rows) or now()[:10]
    d = db()
    d.execute("INSERT OR REPLACE INTO weeks48(week, uploaded_at, filename) VALUES(?,?,?)",
              (week, now(), p.get("fileName", "")))
    fleet = p.get("fleetSize")
    try:
        fleet = int(fleet) if fleet not in (None, "") else None
    except (TypeError, ValueError):
        fleet = None
    if fleet and fleet > 0:
        d.execute("INSERT OR REPLACE INTO fleet(week, on_hire, source) VALUES(?,?,?)",
                  (week, fleet, p.get("fileName", "")))
    auto_parked, sentinels, early = [], 0, 0
    for a in rows:
        aid = (a.get("id") or "").strip()
        if not aid:
            continue
        if a.get("sentinel"):
            sentinels += 1
        if not a.get("sentinel") and (a.get("daysOffline") or 0) < 7:
            early += 1
        d.execute("INSERT OR REPLACE INTO snapshots48(asset, week, snap) VALUES(?,?,?)",
                  (aid, week, json.dumps(a)))
        d.execute("INSERT OR IGNORE INTO appearances48(asset, week) VALUES(?,?)", (aid, week))
        latest = d.execute("SELECT MAX(week) w FROM appearances48 WHERE asset=?",
                           (aid,)).fetchone()["w"]
        if latest == week:
            d.execute("INSERT OR REPLACE INTO assets48(id, snap) VALUES(?,?)",
                      (aid, json.dumps(a)))
        # auto-park cameras the hub flags as badly positioned / unreachable,
        # but never overwrite a park a human already set
        bad_loc = (a.get("suitable") or "").strip().lower() == "no"
        bad_reach = (a.get("reachable") or "").strip().lower() == "no"
        if bad_loc or bad_reach:
            if not d.execute("SELECT 1 FROM parked WHERE asset=? AND stream='w48'",
                             (aid,)).fetchone():
                detail = []
                if bad_loc:
                    detail.append("location flagged unsuitable")
                if bad_reach:
                    detail.append("not reliably reachable")
                d.execute("INSERT INTO parked(asset, stream, reason, note, parked_at, auto)"
                          " VALUES(?,'w48',?,?,?,1)",
                          (aid, "unsuitable_location" if bad_loc else "not_reachable",
                           "Auto-parked from 48hr report — " + ", ".join(detail), now()))
                auto_parked.append(aid)
    log_action("upload48", "", f"48hr report {week} — {len(rows)} cameras, {early} under 7 days, "
                              f"{len(auto_parked)} auto-parked, {sentinels} no heartbeat"
                              + (f", fleet on hire {fleet}" if fleet else ""))
    d.commit()
    return jsonify({"ok": True, "week": week, "count": len(rows), "autoParked": auto_parked,
                    "sentinels": sentinels, "early": early, "fleet": fleet})


@app.post("/api/unpark_auto")
@require_auth
def unpark_auto():
    """Release every auto-parked camera in one action."""
    d = db()
    n = d.execute("SELECT COUNT(*) c FROM parked WHERE auto=1").fetchone()["c"]
    d.execute("DELETE FROM parked WHERE auto=1")  # auto-parks only ever exist on the 48hr stream
    log_action("unparked", "", f"released {n} auto-parked cameras")
    d.commit()
    return jsonify({"ok": True, "released": n})


@app.post("/api/fix")
@require_auth
def fix():
    p = request.get_json(force=True)
    aid, week, on = p.get("id"), p.get("week"), bool(p.get("on"))
    if not aid or not week:
        return jsonify({"error": "id and week required"}), 400
    d = db()
    if on:
        d.execute("INSERT OR REPLACE INTO fixes(asset, week, ts) VALUES(?,?,?)", (aid, week, now()))
        log_action("fixed", aid, f"report {week}")
    else:
        d.execute("DELETE FROM fixes WHERE asset=? AND week=?", (aid, week))
        log_action("reopened", aid, f"report {week}")
    d.commit()
    return jsonify({"ok": True})


@app.post("/api/note")
@require_auth
def note():
    p = request.get_json(force=True)
    aid, text = p.get("id"), (p.get("text") or "").strip()
    if not aid or not text:
        return jsonify({"error": "id and text required"}), 400
    ts = now()
    d = db()
    user = cur_user()
    d.execute("INSERT INTO comments(asset, ts, text, by_email) VALUES(?,?,?,?)",
              (aid, ts, text, user))
    log_action("note", aid, text[:180])
    d.commit()
    return jsonify({"ok": True, "ts": ts, "text": text, "by": user})


@app.post("/api/import")
@require_auth
def import_state():
    """Full-state restore from a localStorage/JSON export (migration path)."""
    p = request.get_json(force=True)
    if "assets" not in p:
        return jsonify({"error": "not a valid export"}), 400
    d = db()
    for week, w in (p.get("weeks") or {}).items():
        d.execute("INSERT OR REPLACE INTO weeks(week, uploaded_at) VALUES(?,?)",
                  (week, w.get("uploadedAt") or now()))
    for aid, rec in (p.get("assets") or {}).items():
        d.execute("INSERT OR REPLACE INTO assets(id, snap) VALUES(?,?)",
                  (aid, json.dumps(rec.get("snap") or {})))
        aps = rec.get("appearances") or []
        if len(aps) == 1 and rec.get("snap"):
            d.execute("INSERT OR IGNORE INTO snapshots(asset, week, snap) VALUES(?,?,?)",
                      (aid, aps[0], json.dumps(rec["snap"])))
        for wk in rec.get("appearances") or []:
            d.execute("INSERT OR IGNORE INTO appearances(asset, week) VALUES(?,?)", (aid, wk))
        for f in rec.get("fixes") or []:
            d.execute("INSERT OR REPLACE INTO fixes(asset, week, ts) VALUES(?,?,?)",
                      (aid, f.get("week"), f.get("date") or now()))
        for c in rec.get("comments") or []:
            d.execute("INSERT INTO comments(asset, ts, text) VALUES(?,?,?)",
                      (aid, c.get("ts") or now(), c.get("text") or ""))
    d.commit()
    return jsonify({"ok": True})


@app.post("/api/park")
@require_auth
def park():
    p = request.get_json(force=True)
    aid, reason, note = p.get("id"), p.get("reason", ""), (p.get("note") or "").strip()
    stream = p.get("stream") or "w168"
    if not aid or not reason:
        return jsonify({"error": "id and reason required"}), 400
    d = db()
    user = cur_user()
    d.execute("INSERT OR REPLACE INTO parked(asset, stream, reason, note, parked_at, auto)"
              " VALUES(?,?,?,?,?,0)", (aid, stream, reason, note, now()))
    log_action("parked", aid, reason + (f" — {note}" if note else ""))
    d.commit()
    return jsonify({"ok": True})


@app.post("/api/unpark")
@require_auth
def unpark():
    p = request.get_json(force=True)
    aid = p.get("id")
    stream = p.get("stream") or "w168"
    if not aid:
        return jsonify({"error": "id required"}), 400
    d = db()
    d.execute("DELETE FROM parked WHERE asset=? AND stream=?", (aid, stream))
    log_action("unparked", aid, "returned to triage")
    d.commit()
    return jsonify({"ok": True})


@app.post("/api/logout")
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("vi_pass")
    resp.delete_cookie("vi_user")
    return resp


@app.get("/api/health")
def health():
    st = storage_status()
    d = db()
    meta = {r["key"]: r["value"] for r in d.execute("SELECT * FROM meta")}
    counts = {}
    for t in ("weeks", "assets", "activity", "users", "report_files"):
        try:
            counts[t] = d.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        except sqlite3.Error:
            counts[t] = None
    st.update({
        "passcodeSet": True,
        "usingDefaultPasscode": USING_DEFAULT_PASSCODE,
        "allowedDomain": ALLOWED_DOMAIN or None,
        "dbCreatedAt": meta.get("created_at"),
        "bootCount": int(meta.get("boot_count", 0) or 0),
        "counts": counts,
    })
    return jsonify(st)


@app.get("/api/activity")
@require_auth
def list_activity():
    try:
        limit = min(int(request.args.get("limit", 300)), 1000)
    except ValueError:
        limit = 300
    d = db()
    rows = d.execute(
        "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify([{"ts": r["ts"], "email": r["email"], "action": r["action"],
                     "asset": r["asset"], "detail": r["detail"]} for r in rows])


@app.get("/api/users")
@require_auth
def list_users():
    d = db()
    rows = d.execute("SELECT * FROM users ORDER BY last_seen DESC").fetchall()
    return jsonify([{"email": r["email"], "firstSeen": r["first_seen"],
                     "lastSeen": r["last_seen"], "logins": r["logins"]} for r in rows])


@app.get("/api/reports")
@require_auth
def list_reports():
    d = db()
    rows = d.execute(
        "SELECT r.week, r.filename, r.size, w.uploaded_at "
        "FROM report_files r JOIN weeks w ON r.week=w.week ORDER BY r.week DESC"
    ).fetchall()
    return jsonify([{"week": r["week"], "filename": r["filename"],
                     "size": r["size"], "uploadedAt": r["uploaded_at"]} for r in rows])


@app.get("/api/reports/<week>/download")
@require_auth
def download_report(week):
    d = db()
    r = d.execute("SELECT filename, data FROM report_files WHERE week=?", (week,)).fetchone()
    if not r:
        return jsonify({"error": "not found"}), 404
    from flask import Response
    resp = Response(r["data"], mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp.headers["Content-Disposition"] = f'attachment; filename="{r["filename"]}"'
    return resp


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
