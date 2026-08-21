#!/usr/bin/env python3
"""
Integrations dashboard.

Read-only views plus a few actions over the customer data pipeline.

Run it:
    venv/bin/python3 app.py

By default it listens on 127.0.0.1 only, so nothing is exposed to the
internet - reach it through an SSH tunnel (see README). Set
DASH_HOST=0.0.0.0 to expose it, but only do that behind TLS and auth.
"""

import csv
import io
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import (Flask, Response, jsonify, render_template, request,
                   stream_with_context)


# ── Config ───────────────────────────────────────────────────────

def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


load_env()

DB = os.environ["DB"]
HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8000"))
DASH_USER = os.environ.get("DASH_USER", "")
DASH_PASS = os.environ.get("DASH_PASS", "")

# Where the loader scripts live.
BASE = os.environ.get("INTEGRATIONS_DIR",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Only these jobs can be triggered. Anything not listed can't be run,
# so a bad request can't execute arbitrary commands.
JOBS = {
    "wifi": {
        "label": "Pull wi-fi portal",
        "cwd": os.path.join(BASE, "wifi-sync"),
        "cmd": ["venv/bin/python3", "wifi_to_postgres.py"],
    },
    "revel": {
        "label": "Pull Revel",
        "cwd": os.path.join(BASE, "revel-sync"),
        "cmd": ["venv/bin/python3", "revel_to_postgres.py", "--max-calls", "3000"],
    },
    "master": {
        "label": "Rebuild master table",
        "cwd": BASE,
        "cmd": ["psql", DB, "-c", "SELECT * FROM refresh_master();"],
    },
    "push": {
        "label": "Push to Como",
        "cwd": os.path.join(BASE, "como-sync"),
        "cmd": ["venv/bin/python3", "como_push.py"],
    },
    "push_dry": {
        "label": "Preview Como push",
        "cwd": os.path.join(BASE, "como-sync"),
        "cmd": ["venv/bin/python3", "como_push.py", "--dry-run", "--limit", "20"],
    },
}

BRANDS = ["PICKL", "BONBIRD", "SOUTHPOUR"]

app = Flask(__name__)


# ── Auth (only enforced when a password is set) ──────────────────

def protected(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if DASH_PASS:
            auth = request.authorization
            if not auth or auth.username != DASH_USER or auth.password != DASH_PASS:
                return Response(
                    "Sign in to continue", 401,
                    {"WWW-Authenticate": 'Basic realm="Integrations"'})
        return fn(*a, **kw)
    return wrapper


# ── Database helpers ─────────────────────────────────────────────

def query(sql, params=None, one=False):
    conn = psycopg2.connect(DB)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return None
            rows = cur.fetchall()
    finally:
        conn.close()      # psycopg2's context manager ends the transaction,
                          # not the connection - without this they pile up
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=None):
    conn = psycopg2.connect(DB)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()
    finally:
        conn.close()


def table_exists(name):
    row = query("SELECT to_regclass(%s) AS t", (name,), one=True)
    return row and row["t"] is not None


def jsonable(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def clean(rows):
    if rows is None:
        return None
    if isinstance(rows, dict):
        return {k: jsonable(v) for k, v in rows.items()}
    return [{k: jsonable(v) for k, v in r.items()} for r in rows]


# ── Run history + background jobs ────────────────────────────────

RUNS = {}          # run_id -> {job, started, finished, status, output[]}
RUNS_LOCK = threading.Lock()


def ensure_runs_table():
    execute("""
        CREATE TABLE IF NOT EXISTS dashboard_runs (
            id          TEXT PRIMARY KEY,
            job         TEXT NOT NULL,
            started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            status      TEXT NOT NULL,
            output      TEXT
        )
    """)


def run_job(run_id, job_key):
    job = JOBS[job_key]
    lines = []

    def append(text):
        with RUNS_LOCK:
            RUNS[run_id]["output"].append(text)
        lines.append(text)

    try:
        proc = subprocess.Popen(
            job["cmd"], cwd=job["cwd"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ},
        )
        for line in proc.stdout:
            append(line.rstrip())
        proc.wait()
        status = "ok" if proc.returncode == 0 else "failed"
        append(f"\n[exit code {proc.returncode}]")
    except Exception as exc:
        status = "failed"
        append(f"\n[error] {exc}")

    with RUNS_LOCK:
        RUNS[run_id]["status"] = status
        RUNS[run_id]["finished"] = datetime.now(timezone.utc).isoformat()

    try:
        execute(
            """UPDATE dashboard_runs
               SET finished_at = now(), status = %s, output = %s
               WHERE id = %s""",
            (status, "\n".join(lines)[-20000:], run_id))
    except Exception:
        pass


# ── Pages ────────────────────────────────────────────────────────

@app.route("/")
@protected
def index():
    return render_template("index.html")


# ── 1. Pipeline overview ─────────────────────────────────────────

@app.route("/api/overview")
@protected
def api_overview():
    stages = []

    # Wi-fi portal
    if table_exists("wifi_guests"):
        r = query("""SELECT count(*) AS rows,
                            max(updated_at) AS last_load
                     FROM wifi_guests""", one=True)
        stages.append({"key": "wifi", "label": "Wi-fi portal",
                       "rows": r["rows"], "last": jsonable(r["last_load"]),
                       "detail": "Firestore"})
    else:
        stages.append({"key": "wifi", "label": "Wi-fi portal",
                       "rows": None, "last": None, "detail": "not set up"})

    # Revel
    if table_exists("revel_customers"):
        r = query("""SELECT count(*) AS rows,
                            count(*) FILTER (WHERE email_ok) AS usable,
                            max(updated_at) AS last_load
                     FROM revel_customers""", one=True)
        state = query("SELECT * FROM revel_sync_state") if table_exists(
            "revel_sync_state") else []
        incomplete = [s["brand"] for s in state if not s["backfill_complete"]]
        stages.append({"key": "revel", "label": "Revel",
                       "rows": r["rows"], "usable": r["usable"],
                       "last": jsonable(r["last_load"]),
                       "detail": ("backfilling: " + ", ".join(incomplete))
                                 if incomplete else "up to date",
                       "warn": bool(incomplete)})
    else:
        stages.append({"key": "revel", "label": "Revel",
                       "rows": None, "last": None, "detail": "not set up"})

    # Master
    if table_exists("master_customers"):
        r = query("""SELECT count(*) AS rows,
                            count(*) FILTER (WHERE needs_push) AS pending
                     FROM master_customers""", one=True)
        stages.append({"key": "master", "label": "Master", "rows": r["rows"],
                       "detail": f"{r['pending']:,} awaiting push",
                       "warn": r["pending"] > 0})
    else:
        stages.append({"key": "master", "label": "Master", "rows": None,
                       "detail": "not built"})

    # Como
    if table_exists("como_push_state"):
        r = query("""SELECT count(*) FILTER (WHERE status='ok') AS ok,
                            count(*) FILTER (WHERE status='failed') AS failed,
                            count(*) FILTER (WHERE status='conflict') AS conflict,
                            max(last_pushed_at) AS last
                     FROM como_push_state""", one=True)
        stages.append({"key": "como", "label": "Como", "rows": r["ok"],
                       "last": jsonable(r["last"]),
                       "detail": f"{r['failed']} failed, {r['conflict']} conflicts",
                       "warn": (r["failed"] or 0) + (r["conflict"] or 0) > 0})
    else:
        stages.append({"key": "como", "label": "Como", "rows": None,
                       "detail": "nothing pushed yet"})

    return jsonify({"stages": stages, "jobs":
                    [{"key": k, "label": v["label"]} for k, v in JOBS.items()]})


# ── 2. Source detail ─────────────────────────────────────────────

@app.route("/api/source/<name>")
@protected
def api_source(name):
    if name == "revel":
        if not table_exists("revel_customers"):
            return jsonify({"exists": False})
        return jsonify({
            "exists": True,
            "state": clean(query("SELECT * FROM revel_sync_state")
                           if table_exists("revel_sync_state") else []),
            "by_brand": clean(query("""
                SELECT brand,
                       count(*) AS total,
                       count(*) FILTER (WHERE email_ok) AS usable,
                       count(*) FILTER (WHERE email_opt_in) AS opted_in
                FROM revel_customers GROUP BY brand ORDER BY brand""")),
            "rejections": clean(query("""
                SELECT split_part(email_reason, ':', 1) AS reason, count(*) AS n
                FROM revel_customers WHERE NOT email_ok
                GROUP BY 1 ORDER BY 2 DESC""")),
        })

    if name == "wifi":
        if not table_exists("wifi_guests"):
            return jsonify({"exists": False})
        return jsonify({
            "exists": True,
            "by_market": clean(query("""
                SELECT lastmarket AS market, count(*) AS n
                FROM wifi_guests WHERE lastmarket IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC""")),
            "missing_email": query(
                "SELECT count(*) AS n FROM wifi_guests "
                "WHERE email IS NULL OR email = ''", one=True)["n"],
            "total": query("SELECT count(*) AS n FROM wifi_guests",
                           one=True)["n"],
        })

    return jsonify({"exists": False})


# ── 3 + 4. Master health and data quality ────────────────────────

@app.route("/api/quality")
@protected
def api_quality():
    if not table_exists("master_customers"):
        return jsonify({"exists": False})

    totals = query("""
        SELECT count(*) AS people,
               count(*) FILTER (WHERE first_name IS NOT NULL) AS has_first,
               count(*) FILTER (WHERE last_name IS NOT NULL) AS has_last,
               count(*) FILTER (WHERE birthday IS NOT NULL) AS has_birthday,
               count(*) FILTER (WHERE nationality IS NOT NULL) AS has_nationality,
               count(*) FILTER (WHERE array_length(sources,1) > 1) AS multi_source
        FROM master_customers""", one=True)

    return jsonify({
        "exists": True,
        "totals": clean(totals),
        "tags": clean(query("""
            SELECT unnest(tags) AS tag, count(*) AS n
            FROM master_customers GROUP BY 1 ORDER BY 2 DESC LIMIT 40""")),
        "sources": clean(query("""
            SELECT array_to_string(sources, ' + ') AS combo, count(*) AS n
            FROM master_customers GROUP BY 1 ORDER BY 2 DESC""")),
        "duplicates": query("""
            SELECT count(*) AS n FROM master_customers
            WHERE duplicate_of IS NOT NULL""", one=True)["n"],
        "duplicate_sample": clean(query("""
            SELECT email, duplicate_of, dup_reason FROM master_customers
            WHERE duplicate_of IS NOT NULL ORDER BY duplicate_of LIMIT 15""")),
        "no_brand": query("""
            SELECT count(*) AS n FROM master_customers
            WHERE NOT (tags && %s)""", (BRANDS,), one=True)["n"],
        "no_brand_sample": clean(query("""
            SELECT email, tags FROM master_customers
            WHERE NOT (tags && %s) LIMIT 10""", (BRANDS,))),
    })


# ── 5. Consent ───────────────────────────────────────────────────

@app.route("/api/consent")
@protected
def api_consent():
    if not table_exists("master_customers"):
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "overall": clean(query("""
            SELECT count(*) FILTER (WHERE allow_email) AS consented,
                   count(*) FILTER (WHERE allow_email = false) AS opted_out,
                   count(*) FILTER (WHERE allow_email IS NULL) AS unknown
            FROM master_customers""", one=True)),
        "by_source": clean(query("""
            SELECT array_to_string(sources, ' + ') AS combo,
                   count(*) FILTER (WHERE allow_email) AS consented,
                   count(*) FILTER (WHERE allow_email = false) AS opted_out,
                   count(*) FILTER (WHERE allow_email IS NULL) AS unknown
            FROM master_customers GROUP BY 1 ORDER BY 1""")),
        "by_brand": clean(query("""
            SELECT b AS brand,
                   count(*) FILTER (WHERE allow_email) AS consented,
                   count(*) FILTER (WHERE allow_email = false) AS opted_out,
                   count(*) FILTER (WHERE allow_email IS NULL) AS unknown
            FROM unnest(%s::text[]) AS b
            JOIN master_customers m ON b = ANY(m.tags)
            GROUP BY b ORDER BY b""", (BRANDS,))),
    })


# ── 6. Push status ───────────────────────────────────────────────

@app.route("/api/push")
@protected
def api_push():
    if not table_exists("master_customers"):
        return jsonify({"exists": False})

    pushed = {}
    if table_exists("como_push_state"):
        for r in query("""SELECT brand, status, count(*) AS n
                          FROM como_push_state GROUP BY 1,2"""):
            pushed.setdefault(r["brand"], {})[r["status"]] = r["n"]

    rows = []
    for b in BRANDS:
        tagged = query("SELECT count(*) AS n FROM master_customers "
                       "WHERE %s = ANY(tags)", (b,), one=True)["n"]
        st = pushed.get(b, {})
        rows.append({
            "brand": b,
            "tagged": tagged,
            "ok": st.get("ok", 0),
            "failed": st.get("failed", 0),
            "conflict": st.get("conflict", 0),
            "pending": tagged - st.get("ok", 0),
            "configured": bool(os.environ.get(f"{b}_COMO_API_KEY", "")),
        })

    return jsonify({"exists": True, "brands": rows})


# ── Pre-flight: prove no duplicates can reach Como ───────────────

@app.route("/api/preflight")
@protected
def api_preflight():
    """Checks the actual push queue for problems, rather than trusting
    that the merge did the right thing."""
    checks = []

    # 1. Two queued records reaching the same inbox
    collisions = query("""
        SELECT email_key, array_agg(email ORDER BY email) AS emails
        FROM master_customers
        WHERE needs_push AND duplicate_of IS NULL AND email_key IS NOT NULL
        GROUP BY email_key HAVING count(*) > 1
        LIMIT 25""")
    checks.append({
        "name": "No two queued records share an inbox",
        "pass": len(collisions) == 0,
        "count": len(collisions),
        "sample": clean(collisions),
        "detail": "Gmail ignores dots and anything after a +, so these "
                  "would create several Como members for one person.",
    })

    # 2. Anything already confirmed in Como still queued
    dupe_push = query("""
        SELECT m.email, s.brand, s.status
        FROM master_customers m
        JOIN como_push_state s ON s.email = m.email
        WHERE m.needs_push AND s.status IN ('ok', 'exists')
        LIMIT 25""") if table_exists("como_push_state") else []
    checks.append({
        "name": "Nobody already in Como is queued again",
        "pass": len(dupe_push) == 0,
        "count": len(dupe_push),
        "sample": clean(dupe_push),
        "detail": "These have been pushed or confirmed before.",
    })

    # 3. Records marked duplicate that somehow remain queued
    leaked = query("""
        SELECT email, duplicate_of FROM master_customers
        WHERE needs_push AND duplicate_of IS NOT NULL LIMIT 25""")
    checks.append({
        "name": "No records flagged as duplicates are queued",
        "pass": len(leaked) == 0,
        "count": len(leaked),
        "sample": clean(leaked),
        "detail": "Flagged duplicates should never be pushed.",
    })

    # 4. Queued records with no brand tag
    unrouted = query("""
        SELECT email, tags FROM master_customers
        WHERE needs_push AND NOT (tags && %s) LIMIT 25""", (BRANDS,))
    checks.append({
        "name": "Every queued record has a brand tag",
        "pass": len(unrouted) == 0,
        "count": len(unrouted),
        "sample": clean(unrouted),
        "detail": "Without a brand tag there's no Como account to send to.",
    })

    queued = query("""
        SELECT count(*) AS n FROM master_customers
        WHERE needs_push AND duplicate_of IS NULL""", one=True)["n"]

    return jsonify({
        "queued": queued,
        "checks": checks,
        "all_clear": all(c["pass"] for c in checks),
    })


@app.route("/api/mainpush", methods=["POST"])
@protected
def api_mainpush():
    """Start the full push, paced. Runs in the background so the page
    can poll for output."""
    body = request.get_json(force=True)

    cmd = ["venv/bin/python3", "como_push.py"]

    brand = (body.get("brand") or "").strip().upper()
    if brand and brand != "ALL":
        cmd += ["--brand", brand]

    if body.get("limit"):
        cmd += ["--limit", str(int(body["limit"]))]

    batch = int(body.get("batch_size") or 0)
    pause = int(body.get("batch_pause") or 0)
    if batch and pause:
        cmd += ["--batch-size", str(batch), "--batch-pause", str(pause)]

    if body.get("dry_run"):
        cmd.append("--dry-run")

    run_id = uuid.uuid4().hex[:12]
    with RUNS_LOCK:
        RUNS[run_id] = {"job": "push", "status": "running",
                        "started": datetime.now(timezone.utc).isoformat(),
                        "finished": None, "output": []}

    ensure_runs_table()
    execute("INSERT INTO dashboard_runs (id, job, status) VALUES (%s,'push','running')",
            (run_id,))

    def worker():
        lines = []
        try:
            proc = subprocess.Popen(
                cmd, cwd=os.path.join(BASE, "como-sync"),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env={k: v for k, v in os.environ.items() if k != "DB"})
            for line in proc.stdout:
                with RUNS_LOCK:
                    RUNS[run_id]["output"].append(line.rstrip())
                lines.append(line.rstrip())
            proc.wait()
            status = "ok" if proc.returncode == 0 else "failed"
        except Exception as exc:
            with RUNS_LOCK:
                RUNS[run_id]["output"].append(f"[error] {exc}")
            status = "failed"

        with RUNS_LOCK:
            RUNS[run_id]["status"] = status
            RUNS[run_id]["finished"] = datetime.now(timezone.utc).isoformat()
        try:
            execute("""UPDATE dashboard_runs SET finished_at = now(),
                       status = %s, output = %s WHERE id = %s""",
                    (status, "\n".join(lines)[-40000:], run_id))
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"run_id": run_id, "command": " ".join(cmd)})


@app.route("/api/pushplan")
@protected
def api_pushplan():
    """How long a paced run would take, given what's actually queued."""
    brand = (request.args.get("brand") or "ALL").upper()
    batch = int(request.args.get("batch_size") or 0)
    pause = int(request.args.get("batch_pause") or 0)

    if brand == "ALL":
        queued = query("""
            SELECT count(*) AS n FROM master_customers m
            WHERE m.duplicate_of IS NULL
              AND m.tags && %s
              AND NOT EXISTS (
                  SELECT 1 FROM como_push_state s
                  WHERE s.email = m.email AND s.status IN ('ok','exists'))
            """, (BRANDS,), one=True)["n"] if table_exists("como_push_state") else 0
    else:
        queued = query("""
            SELECT count(*) AS n FROM master_customers m
            WHERE m.duplicate_of IS NULL
              AND %s = ANY(m.tags)
              AND NOT EXISTS (
                  SELECT 1 FROM como_push_state s
                  WHERE s.email = m.email AND s.status IN ('ok','exists'))
            """, (brand,), one=True)["n"] if table_exists("como_push_state") else 0

    per_record = float(os.environ.get("COMO_REQUEST_DELAY_SECONDS", "0.5"))
    # roughly one existence check, plus a create for people not there yet
    calls = int(queued * 1.5)
    seconds = queued * per_record * 1.5
    batches = 0
    if batch and pause and queued:
        batches = (queued + batch - 1) // batch
        seconds += (batches - 1) * pause

    return jsonify({
        "queued": queued,
        "api_calls": calls,
        "batches": batches,
        "seconds": int(seconds),
        "human": _human_time(seconds),
    })


def _human_time(seconds):
    if seconds < 60:
        return f"{int(seconds)} sec"
    if seconds < 3600:
        return f"{seconds/60:.0f} min"
    return f"{seconds/3600:.1f} hours"


@app.route("/api/testpush", methods=["POST"])
@protected
def api_testpush():
    """Push one named person, so you can watch a single record land in
    Como before running the whole set."""
    body = request.get_json(force=True)
    email = (body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "no email given"}), 400

    cmd = ["venv/bin/python3", "como_push.py", "--email", email]
    if body.get("dry_run"):
        cmd.append("--dry-run")

    run_id = uuid.uuid4().hex[:12]
    with RUNS_LOCK:
        RUNS[run_id] = {"job": "testpush", "status": "running",
                        "started": datetime.now(timezone.utc).isoformat(),
                        "finished": None, "output": []}

    def worker():
        try:
            proc = subprocess.Popen(
                cmd, cwd=os.path.join(BASE, "como-sync"),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env={k: v for k, v in os.environ.items() if k != "DB"})
            for line in proc.stdout:
                with RUNS_LOCK:
                    RUNS[run_id]["output"].append(line.rstrip())
            proc.wait()
            status = "ok" if proc.returncode == 0 else "failed"
        except Exception as exc:
            with RUNS_LOCK:
                RUNS[run_id]["output"].append(f"[error] {exc}")
            status = "failed"
        with RUNS_LOCK:
            RUNS[run_id]["status"] = status

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"run_id": run_id})


# ── 7. Conflict queue ────────────────────────────────────────────

@app.route("/api/conflicts")
@protected
def api_conflicts():
    if not table_exists("como_push_state"):
        return jsonify({"rows": []})
    return jsonify({"rows": clean(query("""
        SELECT s.email, s.brand, s.detail, s.last_pushed_at,
               m.first_name, m.last_name, m.tags
        FROM como_push_state s
        LEFT JOIN master_customers m ON m.email = s.email
        WHERE s.status IN ('conflict', 'failed')
        ORDER BY s.status, s.last_pushed_at DESC
        LIMIT 200"""))})


@app.route("/api/conflicts/resolve", methods=["POST"])
@protected
def api_resolve():
    body = request.get_json(force=True)
    execute("DELETE FROM como_push_state WHERE email = %s AND brand = %s",
            (body["email"], body["brand"]))
    return jsonify({"ok": True})


# ── 8 + 14. Trigger jobs, run history ────────────────────────────

@app.route("/api/run/<job_key>", methods=["POST"])
@protected
def api_run(job_key):
    if job_key not in JOBS:
        return jsonify({"error": "unknown job"}), 400

    run_id = uuid.uuid4().hex[:12]
    with RUNS_LOCK:
        RUNS[run_id] = {"job": job_key, "status": "running",
                        "started": datetime.now(timezone.utc).isoformat(),
                        "finished": None, "output": []}

    ensure_runs_table()
    execute("INSERT INTO dashboard_runs (id, job, status) VALUES (%s,%s,'running')",
            (run_id, job_key))

    threading.Thread(target=run_job, args=(run_id, job_key), daemon=True).start()
    return jsonify({"run_id": run_id})


@app.route("/api/run/<run_id>/output")
@protected
def api_run_output(run_id):
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run:
            return jsonify({"error": "unknown run"}), 404
        return jsonify({"status": run["status"],
                        "output": "\n".join(run["output"])})


@app.route("/api/runs")
@protected
def api_runs():
    if not table_exists("dashboard_runs"):
        return jsonify({"rows": []})
    return jsonify({"rows": clean(query("""
        SELECT id, job, started_at, finished_at, status
        FROM dashboard_runs ORDER BY started_at DESC LIMIT 50"""))})


@app.route("/api/runs/<run_id>")
@protected
def api_run_detail(run_id):
    row = query("SELECT * FROM dashboard_runs WHERE id = %s", (run_id,), one=True)
    return jsonify(clean(row) or {})


# ── 10. Customer lookup ──────────────────────────────────────────

@app.route("/api/customer")
@protected
def api_customer():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "no email given"}), 400

    master = query("SELECT * FROM master_customers WHERE email = %s",
                   (email,), one=True) if table_exists("master_customers") else None

    result = {"email": email, "master": clean(master), "sources": {}, "push": []}

    if table_exists("wifi_guests"):
        result["sources"]["wifi_guests"] = clean(query(
            "SELECT * FROM wifi_guests WHERE lower(email) = %s", (email,)))
    if table_exists("revel_customers"):
        result["sources"]["revel_customers"] = clean(query(
            "SELECT * FROM revel_customers WHERE lower(email) = %s", (email,)))
    if table_exists("como_push_state"):
        result["push"] = clean(query(
            "SELECT * FROM como_push_state WHERE email = %s ORDER BY brand",
            (email,)))

    return jsonify(result)


# ── 9. Preview what would be sent to Como ────────────────────────

@app.route("/api/preview")
@protected
def api_preview():
    email = (request.args.get("email") or "").strip().lower()
    row = query("SELECT * FROM master_customers WHERE email = %s",
                (email,), one=True)
    if not row:
        return jsonify({"error": "not in master_customers"}), 404

    payload = {}
    if row["first_name"]:
        payload["firstName"] = row["first_name"]
    if row["last_name"]:
        payload["lastName"] = row["last_name"]
    if row["birthday"]:
        payload["birthday"] = row["birthday"].strftime("%d.%m.%Y")
    if row["allow_email"] is not None:
        payload["allowEmail"] = row["allow_email"]

    targets = [b for b in BRANDS if b in (row["tags"] or [])]

    return jsonify({
        "email": email,
        "brands": targets,
        "request": {"customer": {"email": email}, "registrationData": payload},
        "note": "Nationality and Tag are omitted until their Como generic "
                "fields are configured.",
    })


# ── 11 + 12. Registry editing ────────────────────────────────────

@app.route("/api/registry")
@protected
def api_registry():
    return jsonify({
        "sources": clean(query("SELECT * FROM source_map ORDER BY priority")
                         if table_exists("source_map") else []),
        "blocked": clean(query("SELECT * FROM blocked_domains ORDER BY domain")
                         if table_exists("blocked_domains") else []),
        "countries": clean(query("SELECT * FROM country_map ORDER BY slug")
                           if table_exists("country_map") else []),
    })


@app.route("/api/registry/source", methods=["POST"])
@protected
def api_registry_source():
    b = request.get_json(force=True)
    allowed = {"email_expr", "first_name_expr", "last_name_expr",
               "nationality_expr", "birthday_expr", "source_tag",
               "extra_tags_expr", "allow_email_expr", "where_extra",
               "priority", "enabled"}
    field = b.get("field")
    if field not in allowed:
        return jsonify({"error": "field not editable"}), 400
    execute(f"UPDATE source_map SET {field} = %s WHERE source_table = %s",
            (b.get("value") or None, b["source_table"]))
    return jsonify({"ok": True})


@app.route("/api/registry/list", methods=["POST"])
@protected
def api_registry_list():
    b = request.get_json(force=True)
    which = b.get("list")
    if which == "blocked":
        if b["action"] == "add":
            execute("INSERT INTO blocked_domains (domain) VALUES (%s) "
                    "ON CONFLICT DO NOTHING", (b["value"].strip().lower(),))
        else:
            execute("DELETE FROM blocked_domains WHERE domain = %s",
                    (b["value"],))
    elif which == "country":
        if b["action"] == "add":
            execute("INSERT INTO country_map (slug, tag) VALUES (%s,%s) "
                    "ON CONFLICT (slug) DO UPDATE SET tag = EXCLUDED.tag",
                    (b["value"].strip().lower(), b["tag"].strip().upper()))
        else:
            execute("DELETE FROM country_map WHERE slug = %s", (b["value"],))
    else:
        return jsonify({"error": "unknown list"}), 400
    return jsonify({"ok": True})


# ── 15. Export ───────────────────────────────────────────────────

@app.route("/export.csv")
@protected
def export_csv():
    brand = request.args.get("brand")
    sql = """SELECT email, first_name, last_name, nationality,
                    birthday, tags, allow_email
             FROM master_customers"""
    params = ()
    if brand:
        sql += " WHERE %s = ANY(tags)"
        params = (brand.upper(),)
    sql += " ORDER BY email"

    rows = query(sql, params)

    def generate():
        buf = io.StringIO()
        w = csv.writer(buf)
        # Column names match Como's import template
        w.writerow(["Email Address", "First Name", "Last Name",
                    "Nationality", "Birthday", "Tag", "AllowEmail"])
        yield buf.getvalue()
        buf.seek(0), buf.truncate(0)

        for r in rows:
            w.writerow([
                r["email"], r["first_name"] or "", r["last_name"] or "",
                r["nationality"] or "",
                r["birthday"].strftime("%d.%m.%Y") if r["birthday"] else "",
                json.dumps(r["tags"] or []),
                "" if r["allow_email"] is None else str(r["allow_email"]).lower(),
            ])
            yield buf.getvalue()
            buf.seek(0), buf.truncate(0)

    name = f"como-export{'-' + brand.lower() if brand else ''}.csv"
    return Response(stream_with_context(generate()), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


if __name__ == "__main__":
    if HOST != "127.0.0.1" and not DASH_PASS:
        sys.exit("Refusing to listen on %s without DASH_PASS set in .env" % HOST)
    print(f"Dashboard on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True)