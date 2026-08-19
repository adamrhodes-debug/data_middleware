#!/usr/bin/env python3
"""
Pushes customers from master_customers into Como.

Each brand has its own Como account, so customers are routed by their
tags: someone tagged PICKL goes to the Pickl Como, BONBIRD to Bonbird,
and so on. A customer tagged with two brands is pushed to both.

Only brands configured in .env are used - so you can run Pickl today
and add the others later by adding their keys.

Start here:
    python3 como_push.py --verify-auth      check every configured brand
    python3 como_push.py --brands           show what's configured and how
                                            many customers each would get
    python3 como_push.py --dry-run --limit 5
    python3 como_push.py --brand PICKL --limit 10
    python3 como_push.py                    push everything outstanding
"""

import argparse
import json
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests


# ── Config ───────────────────────────────────────────────────────

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
DELAY = float(os.environ.get("COMO_REQUEST_DELAY_SECONDS", "0.5"))
TIMEOUT = 30

# Brands the script knows how to look for. A brand is only used if its
# API key is present in .env, so unconfigured ones are simply skipped.
KNOWN_BRANDS = ["PICKL", "BONBIRD", "SOUTHPOUR"]

CUSTOMER_NOT_FOUND = "4001012"
ALREADY_EXISTS = "4000000"

STATE_TABLE = "como_push_state"


def brand_config(brand):
    """Read one brand's settings from .env. Returns None if not set up."""
    key = os.environ.get(f"{brand}_COMO_API_KEY", "").strip()
    if not key:
        return None
    return {
        "brand": brand,
        "api_key": key,
        "branch_id": os.environ.get(f"{brand}_COMO_BRANCH_ID", "").strip(),
        "pos_id": os.environ.get(f"{brand}_COMO_POS_ID", "1").strip(),
        "source_type": os.environ.get(f"{brand}_COMO_SOURCE_TYPE", "Backoffice").strip(),
        "source_name": os.environ.get(f"{brand}_COMO_SOURCE_NAME",
                                      "CentralCustomerSync").strip(),
        "source_version": os.environ.get(f"{brand}_COMO_SOURCE_VERSION", "1.0.0").strip(),
        "nationality_field": os.environ.get(f"{brand}_COMO_NATIONALITY_FIELD", "").strip(),
        "tag_field": os.environ.get(f"{brand}_COMO_TAG_FIELD", "").strip(),
    }


def configured_brands():
    found = {}
    for b in KNOWN_BRANDS:
        cfg = brand_config(b)
        if cfg:
            found[b] = cfg
    return found


def headers(cfg):
    return {
        "Content-Type": "application/json",
        "X-Api-Key": cfg["api_key"],
        "X-Branch-Id": cfg["branch_id"],
        "X-Pos-Id": cfg["pos_id"],
        "X-Source-Type": cfg["source_type"],
        "X-Source-Name": cfg["source_name"],
        "X-Source-Version": cfg["source_version"],
    }


# ── Credential check ─────────────────────────────────────────────

def verify_auth():
    brands = configured_brands()
    if not brands:
        print("No brands configured. Add PICKL_COMO_API_KEY (etc) to .env")
        return False

    all_ok = True
    for name, cfg in brands.items():
        print(f"\n--- {name} ---")
        try:
            r = requests.get(f"{BASE_URL}/api/v4/apiKey",
                             headers=headers(cfg), timeout=TIMEOUT)
        except requests.RequestException as exc:
            print(f"  could not reach Como: {exc}")
            all_ok = False
            continue

        try:
            body = r.json()
        except ValueError:
            print(f"  HTTP {r.status_code}: {r.text[:200]}")
            all_ok = False
            continue

        if r.ok and body.get("status") == "ok":
            print(f"  ok - key '{body.get('name')}' "
                  f"-> location {body.get('locationId')} "
                  f"({body.get('locationName')})")
            if str(body.get("locationId")) != str(cfg["branch_id"]):
                print(f"  NOTE: .env has {name}_COMO_BRANCH_ID={cfg['branch_id']}, "
                      f"Como says {body.get('locationId')}")
            # Sanity check: does the location name look like this brand?
            loc = str(body.get("locationName", "")).upper()
            if loc and name not in loc and loc not in name:
                print(f"  WARNING: this key is for '{body.get('locationName')}' "
                      f"but is configured as {name}")
        else:
            print(f"  HTTP {r.status_code}: "
                  f"{json.dumps(body.get('errors', body))[:200]}")
            all_ok = False

    return all_ok


# ── Payload ──────────────────────────────────────────────────────

def registration_data(row, cfg):
    data = {}
    if row["first_name"]:
        data["firstName"] = row["first_name"]
    if row["last_name"]:
        data["lastName"] = row["last_name"]
    if row["birthday"]:
        data["birthday"] = row["birthday"].strftime("%d.%m.%Y")
    if row["allow_email"] is not None:
        data["allowEmail"] = row["allow_email"]
    if cfg["nationality_field"] and row["nationality"]:
        data[cfg["nationality_field"]] = row["nationality"]
    if cfg["tag_field"] and row["tags"]:
        data[cfg["tag_field"]] = json.dumps(row["tags"])
    return data


# ── Como calls ───────────────────────────────────────────────────

def call(cfg, path, payload):
    r = requests.post(f"{BASE_URL}/api/v4/advanced/{path}",
                      json=payload, headers=headers(cfg), timeout=TIMEOUT)
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


def push_one(row, cfg):
    email = row["email"]
    fields = registration_data(row, cfg)

    try:
        _, body = call(cfg, "updateMember", {
            "customer": {"email": email},
            "registrationData": fields,
        })
    except requests.RequestException as exc:
        return "failed", str(exc)

    if body and body.get("status") == "ok":
        return "ok", "updated"

    codes = error_codes(body)

    if CUSTOMER_NOT_FOUND in codes:
        try:
            _, body = call(cfg, "registration/quick",
                           {"customer": {"email": email, **fields}})
        except requests.RequestException as exc:
            return "failed", str(exc)

        if body and body.get("status") == "ok":
            return "ok", "created"
        if ALREADY_EXISTS in error_codes(body):
            # Email belongs to a different membership. Como's fix sends
            # a 2FA code to the customer - not something an unattended
            # job should trigger.
            return "conflict", json.dumps(body)[:200]
        return "failed", json.dumps(body)[:200]

    if ALREADY_EXISTS in codes:
        return "conflict", json.dumps(body)[:200]

    return "failed", json.dumps(body)[:200]


# ── Push state, tracked per brand ────────────────────────────────

def ensure_state_table(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                email          TEXT NOT NULL,
                brand          TEXT NOT NULL,
                status         TEXT NOT NULL,
                detail         TEXT,
                last_pushed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (email, brand)
            )
        """)
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {STATE_TABLE}_status_idx "
            f"ON {STATE_TABLE} (brand, status)")
    conn.commit()


def record(conn, email, brand, status, detail):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {STATE_TABLE} (email, brand, status, detail, last_pushed_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (email, brand) DO UPDATE SET
                status = EXCLUDED.status,
                detail = EXCLUDED.detail,
                last_pushed_at = now()
            """,
            (email, brand, status, (detail or "")[:500]),
        )
    conn.commit()


def pending_for(conn, brand, limit=None, retry_failed=False):
    """Customers tagged with this brand that still need pushing:
    never pushed, or changed since, or previously failed."""
    ok_clause = "s.status = 'ok' AND NOT m.needs_push"
    if retry_failed:
        ok_clause = "s.status = 'ok' AND NOT m.needs_push"

    sql = f"""
        SELECT m.*
        FROM master_customers m
        LEFT JOIN {STATE_TABLE} s
               ON s.email = m.email AND s.brand = %s
        WHERE %s = ANY(m.tags)
          AND (s.email IS NULL
               OR m.needs_push
               OR s.status = 'failed'
               {"" if retry_failed else "AND s.status <> 'conflict'"})
        ORDER BY m.email
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (brand, brand))
        return cur.fetchall()


# ── Reporting ────────────────────────────────────────────────────

def show_brands(conn):
    brands = configured_brands()
    print("\nConfigured in .env:")
    for b in KNOWN_BRANDS:
        mark = "yes" if b in brands else " no"
        print(f"  {b:<12} {mark}")

    print("\nCustomers per brand tag in master_customers:")
    with conn.cursor() as cur:
        for b in KNOWN_BRANDS:
            cur.execute("SELECT count(*) FROM master_customers WHERE %s = ANY(tags)", (b,))
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT count(*) FROM {STATE_TABLE} WHERE brand = %s AND status = 'ok'",
                (b,))
            done = cur.fetchone()[0]
            state = "configured" if b in brands else "NOT configured"
            print(f"  {b:<12} {total:>8,} tagged   {done:>8,} pushed   ({state})")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM master_customers "
            "WHERE NOT (tags && %s)", (KNOWN_BRANDS,))
        orphan = cur.fetchone()[0]
    if orphan:
        print(f"\n  {orphan:,} customers carry no brand tag - they won't be "
              f"pushed anywhere.")
        print("  psql \"$DB\" -c \"SELECT email, tags FROM master_customers "
              "WHERE NOT (tags && ARRAY['PICKL','BONBIRD','SOUTHPOUR']) LIMIT 10;\"")


# ── Main ─────────────────────────────────────────────────────────

def run(only_brand=None, limit=None, dry_run=False, retry_failed=False):
    brands = configured_brands()
    if only_brand:
        only_brand = only_brand.upper()
        if only_brand not in brands:
            sys.exit(f"{only_brand} isn't configured in .env")
        brands = {only_brand: brands[only_brand]}

    if not brands:
        sys.exit("No brands configured. Add PICKL_COMO_API_KEY (etc) to .env")

    conn = psycopg2.connect(DB)
    ensure_state_table(conn)

    for name, cfg in brands.items():
        rows = pending_for(conn, name, limit=limit, retry_failed=retry_failed)
        print(f"\n=== {name}: {len(rows):,} to push ===")

        if not cfg["nationality_field"] or not cfg["tag_field"]:
            print("  (Nationality/Tag fields unset - those values won't be sent)")

        counts = {"ok": 0, "failed": 0, "conflict": 0}

        for i, row in enumerate(rows, 1):
            if dry_run:
                print(f"  [dry-run] {row['email']}")
                print(f"            {json.dumps(registration_data(row, cfg))}")
                continue

            status, detail = push_one(row, cfg)
            counts[status] += 1
            record(conn, row["email"], name, status, detail)

            marker = {"ok": " ", "failed": "!", "conflict": "?"}[status]
            print(f"  {marker} {i:>6}/{len(rows)}  {row['email']:<38} {detail[:50]}")
            time.sleep(DELAY)

        if not dry_run and rows:
            print(f"  pushed {counts['ok']}, failed {counts['failed']}, "
                  f"conflicts {counts['conflict']}")

    # Clear needs_push only where every brand tag has been pushed.
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE master_customers m
                SET needs_push = false
                WHERE m.needs_push
                  AND NOT EXISTS (
                      SELECT 1 FROM unnest(m.tags) AS t
                      WHERE t = ANY(%s)
                        AND NOT EXISTS (
                            SELECT 1 FROM {STATE_TABLE} s
                            WHERE s.email = m.email AND s.brand = t
                              AND s.status = 'ok'
                        )
                  )
            """, (list(configured_brands().keys()),))
        conn.commit()

    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Push customers to Como, per brand")
    p.add_argument("--verify-auth", action="store_true",
                   help="check every configured brand's credentials")
    p.add_argument("--brands", action="store_true",
                   help="show configured brands and customer counts")
    p.add_argument("--brand", help="push only this brand")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, send nothing")
    p.add_argument("--limit", type=int, help="only push this many per brand")
    p.add_argument("--retry-failed", action="store_true",
                   help="also retry conflicts")
    args = p.parse_args()

    if args.verify_auth:
        sys.exit(0 if verify_auth() else 1)

    if args.brands:
        c = psycopg2.connect(DB)
        ensure_state_table(c)
        show_brands(c)
        c.close()
        sys.exit(0)

    run(only_brand=args.brand, limit=args.limit,
        dry_run=args.dry_run, retry_failed=args.retry_failed)