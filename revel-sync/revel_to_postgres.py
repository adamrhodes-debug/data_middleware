#!/usr/bin/env python3
"""
Copies customers from Revel Systems into Postgres.

Run it:   python3 revel_to_postgres.py
          python3 revel_to_postgres.py --brand pickl
          python3 revel_to_postgres.py --dry-run

Handles all three brands (Southpour, Pickl, Bonbird). Each customer is
tagged with which brand they came from.

Email quality is scored using the same rules as the wi-fi portal's
validate-email.php, so both sources agree on what counts as junk.
Rejected rows are still stored, flagged with the reason - nothing is
thrown away, you just filter on email_ok when you use the data.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, date, timedelta

import psycopg2
import requests

# ── Settings ─────────────────────────────────────────────────────

TABLE = "revel_customers"
PAGE_SIZE = 1000         # confirmed working; 10x fewer calls than 100
DELAY_BETWEEN_CALLS = 1.0  # seconds - be gentle, the API is rate limited
MAX_RETRIES = 5
STATE_TABLE = "revel_sync_state"

# Rejected emails matching these reasons are thrown away rather than
# stored. Everything else is kept with email_ok = false so you can
# review it. Options: internal_domain, disposable, placeholder,
# bad_format, missing, gibberish
DISCARD_REASONS = {"internal_domain"}

DB = os.environ["DB"]

# Brands are read from the .env file next to this script.
BRANDS = ["SOUTHPOUR", "PICKL", "BONBIRD"]


def load_env():
    """Read the .env file sitting next to this script."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        sys.exit(f"Missing {path} - see the setup notes.")
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ[key.strip()] = value.strip()


# ── Email quality checks (ported from validate-email.php) ────────

DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "grr.la", "sharklasers.com",
    "spam4.me", "tempmail.com", "temp-mail.org", "temp-mail.io",
    "tempmail.net", "tempmailo.com", "tempinbox.com", "10minutemail.com",
    "10minutemail.net", "20minutemail.com", "trashmail.com", "trashmail.me",
    "dispostable.com", "maildrop.cc", "fakeinbox.com", "mailnull.com",
    "spamgourmet.com", "discard.email", "discardmail.com", "getnada.com",
    "nada.email", "throwawaymail.com", "mailnesia.com", "yopmail.com",
    "yopmail.fr", "mytrashmail.com", "mailcatch.com", "emailondeck.com",
    "harakirimail.com", "mintemail.com", "mohmal.com", "mytemp.email",
    "spam.la", "tempemail.com", "tempemail.net", "wegwerfmail.de",
    "zoemail.com", "mvrht.com", "anonbox.net", "burnermail.io", "33mail.com",
    "tempr.email", "1secmail.com", "1secmail.net", "1secmail.org",
    "dropmail.me", "emailfake.com", "fakemail.net", "mail-temp.com",
    "tempmail.plus", "minuteinbox.com", "mail7.io", "mailsac.com",
}

# Internal/partner domains - not real customers, so never stored.
EXCLUDED_DOMAINS = {
    "grubtech.com",
    "yolkbrands.com",
}

# Obvious placeholder addresses staff type in to get past a required field.
PLACEHOLDERS = {
    "test@test.com", "a@a.com", "no@no.com", "none@none.com",
    "noemail@noemail.com", "n/a@n/a.com", "x@x.com", "abc@abc.com",
    "test@gmail.com", "noemail@gmail.com", "na@na.com", "asd@asd.com",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
CONSONANTS = "bcdfghjklmnpqrstvwxz"


def gibberish_score(local):
    """Same scored approach as the PHP validator: several independent
    signals must agree before something is called junk."""
    word = re.sub(r"[._\-+].*$", "", local)
    word = re.sub(r"\d+$", "", word)
    n = len(word)
    if n < 6:
        return 0, []          # too short to judge fairly

    score, signals = 0, []

    if re.search(r"(.)\1{2,}", word):
        score += 2; signals.append("repeat_char")
    if re.match(r"^(.{1,2})\1{2,}", word):
        score += 3; signals.append("repeat_unit")

    vowels = len(re.findall(r"[aeiouy]", word))
    if vowels == 0:
        score += 3; signals.append("no_vowels")
    elif vowels / n < 0.15:
        score += 2; signals.append("low_vowels")

    if re.search(rf"[{CONSONANTS}]{{5,}}", word):
        score += 2; signals.append("consonant_run")

    pairs = [(word[i] in CONSONANTS and word[i + 1] in CONSONANTS) for i in range(n - 1)]
    if pairs and sum(pairs) / len(pairs) > 0.6:
        score += 2; signals.append("cc_density")

    diversity = len(set(word)) / n
    if diversity <= 0.4:
        score += 3; signals.append("low_diversity")
    elif diversity <= 0.5:
        score += 2; signals.append("mid_diversity")

    bigrams = [word[i:i + 2] for i in range(n - 1)]
    counts = {b: bigrams.count(b) for b in set(bigrams)}
    repeated = sum(1 for c in counts.values() if c >= 2)
    if repeated >= 2:
        score += 2; signals.append("repeat_bigrams")
    elif repeated == 1 and max(counts.values()) >= 3:
        score += 2; signals.append("repeat_bigram_x3")

    return score, signals


def check_email(raw):
    """Returns (cleaned_email, is_ok, reason)."""
    if not raw or not str(raw).strip():
        return None, False, "missing"

    email = str(raw).strip().lower()

    if not EMAIL_RE.match(email):
        return email, False, "bad_format"
    if email in PLACEHOLDERS:
        return email, False, "placeholder"

    local, _, domain = email.partition("@")

    if domain in EXCLUDED_DOMAINS:
        return email, False, "internal_domain"

    if domain in DISPOSABLE:
        return email, False, "disposable"

    score, signals = gibberish_score(local)
    if score >= 5:
        return email, False, "gibberish:" + ",".join(signals)

    return email, True, None


# ── Talking to Revel ─────────────────────────────────────────────

def naive(dt):
    """Revel returns timestamps with no timezone, but Postgres hands
    back timezone-aware ones. Strip the timezone so they compare."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def api_get(url, headers, params):
    """One API call, with retries and rate-limit backoff.
    Returns parsed JSON or None."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=90)
        except requests.RequestException as exc:
            print(f"    network error: {exc} - retrying")
            time.sleep(5 * (attempt + 1))
            continue

        if resp.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"    RATE LIMITED - waiting {wait}s")
            time.sleep(wait)
            continue

        if resp.status_code == 401:
            print("    AUTH FAILED - check the API key and secret")
            return None

        if not resp.ok:
            print(f"    HTTP {resp.status_code}: {resp.text[:200]}")
            time.sleep(5 * (attempt + 1))
            continue

        return resp.json()

    return None


def fetch_customers(brand, conn, max_calls, dry_run=False):
    """Pull customers for one brand.

    First run does a full backfill, saving its position after every
    page - so if it stops (rate limit, network, Ctrl-C) the next run
    carries on from there rather than starting over.

    Once the backfill is finished, later runs only ask for customers
    changed since the last run, which is far cheaper.
    """
    subdomain = os.environ[f"{brand}_SUBDOMAIN"]
    api_key = os.environ[f"{brand}_API_KEY"]
    secret = os.environ[f"{brand}_SECRET"]
    name = os.environ.get(f"{brand}_NAME", brand.title())

    url = f"https://{subdomain}.revelup.com/resources/Customer/"
    headers = {"API-AUTHENTICATION": f"{api_key}:{secret}"}

    offset, watermark, done = read_state(conn, name)
    watermark = naive(watermark)

    print(f"\n--- {name} ({subdomain}) ---")

    params = {"limit": PAGE_SIZE, "format": "json", "email__contains": "@"}

    if done and watermark:
        # Incremental: only what changed since last time.
        # Overlap by a day so nothing slips through the gap.
        since = (watermark - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        params["updated_date__gte"] = since
        offset = 0
        print(f"  incremental - customers updated since {since}")
    else:
        if offset:
            print(f"  resuming backfill from record {offset:,}")
        else:
            print("  starting full backfill")

    customers = []
    calls = 0
    newest = watermark

    while True:
        if calls >= max_calls:
            print(f"  stopping - hit the {max_calls} call limit for this run")
            break

        params["offset"] = offset
        data = api_get(url, headers, params)
        calls += 1

        if data is None:
            print("  giving up on this page")
            break

        page = data.get("objects", [])
        total = data.get("meta", {}).get("total_count")

        for c in page:
            c["_brand"] = name
            stamp = c.get("updated_date")
            if stamp:
                try:
                    parsed = naive(datetime.fromisoformat(str(stamp)))
                    if newest is None or parsed > newest:
                        newest = parsed
                except ValueError:
                    pass

        customers.extend(page)
        offset += len(page)

        pct = f" ({100 * offset / total:.1f}%)" if total else ""
        print(f"  {offset:,}" + (f" of {total:,}{pct}" if total else "") +
              f"  [{calls} calls]")

        if not dry_run:
            # Save progress AND write this page now, so an interrupted
            # run keeps everything it already fetched.
            write_to_postgres(customers, conn, quiet=True)
            customers = []
            save_state(conn, name, offset, newest, complete=False)

        if len(page) < PAGE_SIZE:
            # Reached the end.
            if not dry_run:
                save_state(conn, name, 0, newest, complete=True)
                print("  backfill complete - future runs will be incremental")
            break

        if dry_run:
            print("  (dry run - stopping after one page)")
            break

        time.sleep(DELAY_BETWEEN_CALLS)

    print(f"  used {calls} API calls")
    return customers


# ── Remembering where we got to ───────────────────────────────────

def ensure_state_table(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                brand              TEXT PRIMARY KEY,
                backfill_offset    BIGINT NOT NULL DEFAULT 0,
                backfill_complete  BOOLEAN NOT NULL DEFAULT false,
                watermark          TIMESTAMPTZ,
                last_run_at        TIMESTAMPTZ
            )
        """)
    conn.commit()


def read_state(conn, brand):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT backfill_offset, watermark, backfill_complete "
            f"FROM {STATE_TABLE} WHERE brand = %s", (brand,))
        row = cur.fetchone()
    return row if row else (0, None, False)


def save_state(conn, brand, offset, watermark, complete):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {STATE_TABLE}
                (brand, backfill_offset, backfill_complete, watermark, last_run_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (brand) DO UPDATE SET
                backfill_offset = EXCLUDED.backfill_offset,
                backfill_complete = EXCLUDED.backfill_complete,
                watermark = COALESCE(EXCLUDED.watermark, {STATE_TABLE}.watermark),
                last_run_at = now()
            """,
            (brand, offset, complete, watermark),
        )
    conn.commit()


# ── Writing to Postgres ──────────────────────────────────────────

def safe(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                revel_id     TEXT PRIMARY KEY,
                brand        TEXT,
                first_name   TEXT,
                last_name    TEXT,
                email        TEXT,
                email_ok     BOOLEAN,
                email_opt_in BOOLEAN,
                updated_date TIMESTAMPTZ,
                email_reason TEXT,
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {TABLE}_email_ok_idx ON {TABLE} (email_ok)")
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {TABLE}_opt_in_idx ON {TABLE} (email_opt_in)")
    conn.commit()


def write_to_postgres(customers, conn, quiet=False):
    if not customers:
        return {"ok": 0, "rejected": 0}

    stats = {"ok": 0, "rejected": 0, "discarded": 0}
    reasons = {}

    with conn.cursor() as cur:
        for c in customers:
            revel_id = str(c.get("resource_uri") or c.get("id") or "").strip()
            if not revel_id:
                continue

            email, ok, reason = check_email(c.get("email"))

            # Some rejects aren't worth storing at all - drop them
            # rather than filling the table with rows you'll never use.
            # Add more reasons here to discard those too, e.g.
            # "disposable" or "gibberish".
            if reason and reason.split(":")[0] in DISCARD_REASONS:
                stats["discarded"] = stats.get("discarded", 0) + 1
                continue

            stats["ok" if ok else "rejected"] += 1
            if reason:
                key = reason.split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1

            stamp = c.get("updated_date")
            try:
                stamp = datetime.fromisoformat(str(stamp)) if stamp else None
            except ValueError:
                stamp = None

            cur.execute(
                f"""
                INSERT INTO {TABLE}
                    (revel_id, brand, first_name, last_name,
                     email, email_ok, email_opt_in, updated_date, email_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (revel_id) DO UPDATE SET
                    brand = EXCLUDED.brand,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    email = EXCLUDED.email,
                    email_ok = EXCLUDED.email_ok,
                    email_opt_in = EXCLUDED.email_opt_in,
                    updated_date = EXCLUDED.updated_date,
                    email_reason = EXCLUDED.email_reason,
                    updated_at = now()
                """,
                (
                    revel_id,
                    c.get("_brand"),
                    c.get("first_name"),
                    c.get("last_name"),
                    email,
                    ok,
                    bool(c.get("email_opt_in")),
                    stamp,
                    reason,
                ),
            )
    conn.commit()

    if not quiet:
        print(f"\n  usable emails: {stats['ok']}    "
              f"rejected: {stats['rejected']}    "
              f"discarded: {stats.get('discarded', 0)}")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"      {reason:<16} {count}")

    return stats


def summarise(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {TABLE}")
        total = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {TABLE} WHERE email_ok")
        good = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {TABLE} WHERE email_ok AND email_opt_in")
        mailable = cur.fetchone()[0]
        cur.execute(
            f"SELECT email_reason, count(*) FROM {TABLE} "
            f"WHERE NOT email_ok GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
        reasons = cur.fetchall()

    print(f"\n  {TABLE}: {total:,} rows")
    print(f"    usable email:        {good:,}")
    print(f"    usable AND opted in: {mailable:,}")
    if reasons:
        print("    rejected because:")
        for reason, count in reasons:
            print(f"      {str(reason).split(':')[0]:<16} {count:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pull Revel customers into Postgres")
    parser.add_argument("--brand", help="just one brand, e.g. pickl")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch one page per brand, write nothing")
    parser.add_argument("--max-calls", type=int, default=800,
                        help="stop after this many API calls per brand "
                             "(default 800, keeps you under a 10k/day limit "
                             "across 3 brands)")
    parser.add_argument("--restart", action="store_true",
                        help="forget saved progress and backfill from scratch")
    args = parser.parse_args()

    load_env()
    brands = [args.brand.upper()] if args.brand else BRANDS

    conn = psycopg2.connect(DB)
    ensure_state_table(conn)
    ensure_table(conn)

    if args.restart:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {STATE_TABLE}")
        conn.commit()
        print("Progress cleared - starting from scratch.")

    for brand in brands:
        sample = fetch_customers(brand, conn, args.max_calls, dry_run=args.dry_run)

        if args.dry_run and sample:
            print("\n  Email check on this sample:")
            for c in sample[:15]:
                email, ok, reason = check_email(c.get("email"))
                print(f"    [{'keep' if ok else 'drop'}] {str(email):<38} "
                      f"{reason or ''}")

    if not args.dry_run:
        summarise(conn)

    conn.close()
