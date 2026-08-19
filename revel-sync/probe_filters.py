"""
Tests which server-side filters Revel accepts, and how much each one
cuts the result set. Uses limit=1 so each test costs one tiny call.

Run:  venv/bin/python3 probe_filters.py
"""

import os
import time

import requests

for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()

BRAND = os.environ.get("PROBE_BRAND", "PICKL")
sub = os.environ[f"{BRAND}_SUBDOMAIN"]
key = os.environ[f"{BRAND}_API_KEY"]
sec = os.environ[f"{BRAND}_SECRET"]

URL = f"https://{sub}.revelup.com/resources/Customer/"
HEADERS = {"API-AUTHENTICATION": f"{key}:{sec}"}


def count(label, params):
    """Ask for 1 record and read total_count from the meta block."""
    p = {"limit": 1, "format": "json"}
    p.update(params)
    try:
        r = requests.get(URL, headers=HEADERS, params=p, timeout=60)
    except requests.RequestException as exc:
        print(f"  {label:<46} network error: {exc}")
        return None

    if r.status_code == 400:
        print(f"  {label:<46} NOT SUPPORTED (400)")
        return None
    if not r.ok:
        print(f"  {label:<46} HTTP {r.status_code}")
        return None

    try:
        total = r.json()["meta"]["total_count"]
    except (KeyError, ValueError):
        print(f"  {label:<46} unexpected response")
        return None

    print(f"  {label:<46} {total:>10,}")
    time.sleep(1)
    return total


print(f"\nProbing {BRAND} ({sub})\n")
print(f"  {'filter':<46} {'records':>10}")
print("  " + "-" * 58)

baseline = count("(no filter)", {})

print("\n-- consent --")
count("email_opt_in=true", {"email_opt_in": "true"})
count("ok_to_email=true", {"ok_to_email": "true"})

print("\n-- has a real email --")
count("email__contains=@", {"email__contains": "@"})
count("email__isnull=false", {"email__isnull": "false"})
count("email__gt=''", {"email__gt": ""})

print("\n-- exclude junk domains --")
count("email__contains=@ + not grubtech", {"email__contains": "@", "email__icontains": "@"})

print("\n-- status --")
count("deleted=false", {"deleted": "false"})
count("active=true", {"active": "true"})

print("\n-- incremental cursor --")
count("updated_date__gte=2026-08-01", {"updated_date__gte": "2026-08-01T00:00:00"})
count("updated_date__gte=2026-01-01", {"updated_date__gte": "2026-01-01T00:00:00"})

print("\n-- combined (the one that matters) --")
combo = count(
    "opt_in + has email + not deleted",
    {"email_opt_in": "true", "email__contains": "@", "deleted": "false"},
)

print("\n-- ordering (needed for safe pagination) --")
count("order_by=updated_date", {"order_by": "updated_date"})

print("\n-- page size ceiling --")
for size in (100, 500, 1000):
    try:
        r = requests.get(URL, headers=HEADERS,
                         params={"limit": size, "format": "json"}, timeout=90)
        got = len(r.json().get("objects", [])) if r.ok else 0
        print(f"  limit={size:<40} returned {got}")
    except requests.RequestException as exc:
        print(f"  limit={size:<40} error: {exc}")
    time.sleep(1)

if baseline and combo:
    print(f"\n  Filtering cuts {baseline:,} down to {combo:,} "
          f"({100 * combo / baseline:.1f}%)")
