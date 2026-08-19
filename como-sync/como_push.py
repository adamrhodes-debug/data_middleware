#!/usr/bin/env python3
"""
Pushes customers from master_customers into Como.

Start here:
    python3 como_push.py --verify-auth     check credentials, show your location ID
    python3 como_push.py --dry-run         show what would be sent, send nothing
    python3 como_push.py --limit 10        push 10 customers as a test
    python3 como_push.py                   push everything flagged needs_push

Como has no bulk endpoint, so this is one API call per customer. It
tries updateMember first (most people already exist on a repeat run)
and falls back to registration/quick if Como says they don't.

Results are written back to master_customers: successful pushes clear
needs_push, failures stay flagged for the next run, and conflicts are
left for a human to look at.
"""

import argparse
import json
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests


# ── Config from .env in this folder ──────────────────────────────

def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        sys.exit(f"Missing {path}")
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


load_env()

DB = os.environ["DB"]
BASE_URL = os.environ.get("COMO_BASE_URL", "https://api.prod.bcomo.com")
API_KEY = os.environ.get("COMO_API_KEY", "")
BRANCH_ID = os.environ.get("COMO_BRANCH_ID", "1")
POS_ID = os.environ.get("COMO_POS_ID", "1")
SOURCE_TYPE = os.environ.get("COMO_SOURCE_TYPE", "Backoffice")
SOURCE_NAME = os.environ.get("COMO_SOURCE_NAME", "CentralCustomerSync")
SOURCE_VERSION = os.environ.get("COMO_SOURCE_VERSION", "1.0.0")

# Nationality and Tag have no named field in Como's API - they map to
# configurable generic slots. Blank means "don't send it".
NATIONALITY_FIELD = os.environ.get("COMO_NATIONALITY_FIELD", "")
TAG_FIELD = os.environ.get("COMO_TAG_FIELD", "")

DELAY = float(os.environ.get("COMO_REQUEST_DELAY_SECONDS", "0.5"))
TIMEOUT = 30

CUSTOMER_NOT_FOUND = "4001012"
ALREADY_EXISTS = "4000000"


def headers():
    return {
        "Content-Type": "application/json",
        "X-Api-Key": API_KEY,
        "X-Branch-Id": BRANCH_ID,
        "X-Pos-Id": POS_ID,
        "X-Source-Type": SOURCE_TYPE,
        "X-Source-Name": SOURCE_NAME,
        "X-Source-Version": SOURCE_VERSION,
    }


# ── Checking the credentials work ────────────────────────────────

def verify_auth():
    """Hits Como's apiKey endpoint. Touches no customer data."""
    print(f"Checking credentials against {BASE_URL} ...\n")
    try:
        r = requests.get(f"{BASE_URL}/api/v4/apiKey",
                         headers=headers(), timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"Could not reach Como: {exc}")
        return False

    print(f"HTTP {r.status_code}")
    try:
        body = r.json()
        print(json.dumps(body, indent=2))
    except ValueError:
        print(r.text[:500])
        return False

    if r.ok and body.get("status") == "ok":
        print("\n  Credentials work.")
        if body.get("locationId"):
            print(f"  Your location ID is: {body['locationId']}")
            print(f"  Location name:       {body.get('locationName')}")
            if str(body["locationId"]) != str(BRANCH_ID):
                print(f"\n  NOTE: your .env has COMO_BRANCH_ID={BRANCH_ID}")
                print(f"        Consider setting it to {body['locationId']}")
        return True

    print("\n  Credentials rejected - check COMO_API_KEY in .env")
    return False


# ── Building the payload ─────────────────────────────────────────

def registration_data(row):
    """Only the fields we actually have. Como's update endpoint wants
    just what's changing, so nulls are left out entirely."""
    data = {}

    if row["first_name"]:
        data["firstName"] = row["first_name"]
    if row["last_name"]:
        data["lastName"] = row["last_name"]
    if row["birthday"]:
        # Como's documented date format
        data["birthday"] = row["birthday"].strftime("%d.%m.%Y")
    if row["allow_email"] is not None:
        data["allowEmail"] = row["allow_email"]

    # These two only get sent once you've been told which generic
    # field they map to.
    if NATIONALITY_FIELD and row["nationality"]:
        data[NATIONALITY_FIELD] = row["nationality"]
    if TAG_FIELD and row["tags"]:
        data[TAG_FIELD] = json.dumps(row["tags"])

    return data


# ── Talking to Como ──────────────────────────────────────────────

def call(path, payload):
    url = f"{BASE_URL}/api/v4/advanced/{path}"
    r = requests.post(url, json=payload, headers=headers(), timeout=TIMEOUT)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def error_codes(body):
    codes = set()
    if not body or body.get("status") != "error":
        return codes
    for err in body.get("errors", []) or []:
        if err.get("code"):
            codes.add(str(err["code"]))
        for cause in err.get("cause", []) or []:
            if cause.get("code"):
                codes.add(str(cause["code"]))
    return codes


def push_one(row):
    """Returns (status, detail). status is ok | failed | conflict."""
    email = row["email"]
    fields = registration_data(row)

    try:
        code, body = call("updateMember", {
            "customer": {"email": email},
            "registrationData": fields,
        })
    except requests.RequestException as exc:
        return "failed", str(exc)

    if body and body.get("status") == "ok":
        return "ok", "updated"

    codes = error_codes(body)

    if CUSTOMER_NOT_FOUND in codes:
        # Not in Como yet - create them.
        try:
            code, body = call("registration/quick",
                              {"customer": {"email": email, **fields}})
        except requests.RequestException as exc:
            return "failed", str(exc)

        if body and body.get("status") == "ok":
            return "ok", "created"

        if ALREADY_EXISTS in error_codes(body):
            # The email belongs to a different membership than expected.
            # Como's fix for this sends a 2FA code to the customer, which
            # shouldn't happen unattended - leave it for a person.
            return "conflict", json.dumps(body)[:300]

        return "failed", json.dumps(body)[:300]

    if ALREADY_EXISTS in codes:
        return "conflict", json.dumps(body)[:300]

    return "failed", json.dumps(body)[:300]


# ── Main loop ────────────────────────────────────────────────────

def run(limit=None, dry_run=False, retry_failed=False):
    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    where = "needs_push"
    if retry_failed:
        where = "(needs_push OR push_status = 'failed')"

    sql = f"SELECT * FROM master_customers WHERE {where} ORDER BY email"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    rows = cur.fetchall()

    print(f"{len(rows):,} customers to push\n")
    if not rows:
        conn.close()
        return

    if not NATIONALITY_FIELD or not TAG_FIELD:
        print("  NOTE: COMO_NATIONALITY_FIELD and/or COMO_TAG_FIELD are unset,")
        print("        so those values will NOT be sent to Como.\n")

    counts = {"ok": 0, "failed": 0, "conflict": 0}

    for i, row in enumerate(rows, 1):
        if dry_run:
            print(f"  [dry-run] {row['email']}")
            print(f"            {json.dumps(registration_data(row))}")
            continue

        status, detail = push_one(row)
        counts[status] += 1

        with conn.cursor() as up:
            up.execute(
                """
                UPDATE master_customers
                SET push_status = %s,
                    last_pushed_at = now(),
                    needs_push = %s
                WHERE email = %s
                """,
                (status if status != "ok" else "ok", status != "ok", row["email"]),
            )
        conn.commit()

        marker = {"ok": " ", "failed": "!", "conflict": "?"}[status]
        print(f"  {marker} {i:>5}/{len(rows)}  {row['email']:<40} {detail[:60]}")

        time.sleep(DELAY)

    if not dry_run:
        print(f"\n  pushed:    {counts['ok']}")
        print(f"  failed:    {counts['failed']}   (retried next run)")
        print(f"  conflicts: {counts['conflict']}   (need a human - see below)")
        if counts["conflict"]:
            print("\n  psql \"$DB\" -c \"SELECT email FROM master_customers "
                  "WHERE push_status = 'conflict';\"")

    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Push customers to Como")
    p.add_argument("--verify-auth", action="store_true",
                   help="check credentials and show your location ID, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, send nothing")
    p.add_argument("--limit", type=int, help="only push this many")
    p.add_argument("--retry-failed", action="store_true",
                   help="also retry previously failed pushes")
    args = p.parse_args()

    if args.verify_auth:
        sys.exit(0 if verify_auth() else 1)

    if not API_KEY:
        sys.exit("COMO_API_KEY is not set in .env")

    run(limit=args.limit, dry_run=args.dry_run, retry_failed=args.retry_failed)
