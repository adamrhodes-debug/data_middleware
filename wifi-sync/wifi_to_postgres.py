#!/usr/bin/env python3
"""
Copies wi-fi portal guests from Firestore into Postgres.

Run it:   python3 wifi_to_postgres.py

The table is created automatically the first time. If the portal
starts collecting a new field later, the column gets added on the
next run. You never write any SQL.
"""

import json
import os
from datetime import datetime, date

import psycopg2
from google.cloud import firestore

# ── Settings ─────────────────────────────────────────────────────
# Change these if needed.

FIREBASE_PROJECT = "yolk-wifi"
FIREBASE_COLLECTION = "customers"
POSTGRES_TABLE = "wifi_guests"

DB = os.environ["DB"]   # e.g. "dbname=ingest user=ingest password=xxx host=localhost"


# ── Step 1: read everything out of Firestore ─────────────────────

def read_firestore():
    print(f"Reading '{FIREBASE_COLLECTION}' from Firestore project '{FIREBASE_PROJECT}'...")
    client = firestore.Client(project=FIREBASE_PROJECT)

    guests = []
    for doc in client.collection(FIREBASE_COLLECTION).stream():
        guest = doc.to_dict() or {}
        guest["doc_id"] = doc.id      # Firestore's own id for this guest
        guests.append(guest)

    print(f"  found {len(guests)} guests")
    return guests


# ── Step 2: work out what columns we need ────────────────────────

def column_type(value):
    """What kind of Postgres column suits this value?"""
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE PRECISION"
    if isinstance(value, datetime):
        return "TIMESTAMPTZ"
    if isinstance(value, date):
        return "DATE"
    if isinstance(value, (dict, list)):
        return "JSONB"
    return "TEXT"


def make_safe(value):
    """Postgres can't store Python dicts/lists directly - turn them into JSON."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return value


# ── Step 3: create/update the table, then write the guests ───────

def write_to_postgres(guests):
    if not guests:
        print("Nothing to write.")
        return

    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    # Create the table if this is the first run.
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {POSTGRES_TABLE} (
            doc_id      TEXT PRIMARY KEY,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # Which columns does the table have right now?
    cur.execute(f"""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = '{POSTGRES_TABLE}'
    """)
    existing = {row[0] for row in cur.fetchall()}

    # Add a column for any field we haven't seen before.
    for guest in guests:
        for field, value in guest.items():
            name = field.lower()
            if name not in existing and value is not None:
                kind = column_type(value)
                print(f"  adding new column: {name} ({kind})")
                cur.execute(f"ALTER TABLE {POSTGRES_TABLE} ADD COLUMN {name} {kind}")
                existing.add(name)

    # Write each guest. If they're already there, update them.
    print(f"Writing {len(guests)} guests to Postgres table '{POSTGRES_TABLE}'...")
    for guest in guests:
        fields = [f.lower() for f in guest if f.lower() in existing]
        values = [make_safe(guest[f]) for f in guest if f.lower() in existing]

        placeholders = ", ".join(["%s"] * len(fields))
        updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in fields if f != "doc_id")

        cur.execute(
            f"""
            INSERT INTO {POSTGRES_TABLE} ({", ".join(fields)})
            VALUES ({placeholders})
            ON CONFLICT (doc_id) DO UPDATE SET {updates}, updated_at = now()
            """,
            values,
        )

    conn.commit()

    cur.execute(f"SELECT count(*) FROM {POSTGRES_TABLE}")
    total = cur.fetchone()[0]
    print(f"  done - table now holds {total} guests")

    conn.close()


if __name__ == "__main__":
    guests = read_firestore()
    write_to_postgres(guests)
