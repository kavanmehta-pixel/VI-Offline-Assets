import json
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory, make_response

DB_PATH = os.environ.get("DB_PATH", "vi_offline.db")
PASSCODE = os.environ.get("APP_PASSCODE", "")  # optional shared access code

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
        CREATE TABLE IF NOT EXISTS parked(
            asset TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            parked_at TEXT NOT NULL
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
            text TEXT NOT NULL
        );
        """
    )
    con.commit()
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
    con.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def authed():
    if not PASSCODE:
        return True
    return request.cookies.get("vi_pass") == PASSCODE


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
    return jsonify({"required": bool(PASSCODE), "ok": authed()})


@app.post("/api/auth")
def auth_login():
    code = (request.get_json(silent=True) or {}).get("code", "")
    if not PASSCODE or code == PASSCODE:
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie("vi_pass", code, max_age=180 * 24 * 3600, httponly=False, samesite="Lax")
        return resp
    return jsonify({"ok": False}), 401


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
            assets[r["asset"]]["comments"].append({"ts": r["ts"], "text": r["text"]})
    parked = {}
    for r in d.execute("SELECT * FROM parked"):
        parked[r["asset"]] = {"reason": r["reason"], "note": r["note"], "parkedAt": r["parked_at"]}
    cur = max(weeks.keys()) if weeks else None
    return jsonify({"currentWeek": cur, "weeks": weeks, "assets": assets, "parked": parked})


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
    d.commit()
    return jsonify({"ok": True, "week": week, "count": len(rows), "reappeared": reappeared})


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
    else:
        d.execute("DELETE FROM fixes WHERE asset=? AND week=?", (aid, week))
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
    d.execute("INSERT INTO comments(asset, ts, text) VALUES(?,?,?)", (aid, ts, text))
    d.commit()
    return jsonify({"ok": True, "ts": ts})


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
    if not aid or not reason:
        return jsonify({"error": "id and reason required"}), 400
    d = db()
    d.execute("INSERT OR REPLACE INTO parked(asset, reason, note, parked_at) VALUES(?,?,?,?)",
              (aid, reason, note, now()))
    d.commit()
    return jsonify({"ok": True})


@app.post("/api/unpark")
@require_auth
def unpark():
    p = request.get_json(force=True)
    aid = p.get("id")
    if not aid:
        return jsonify({"error": "id required"}), 400
    d = db()
    d.execute("DELETE FROM parked WHERE asset=?", (aid,))
    d.commit()
    return jsonify({"ok": True})


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
