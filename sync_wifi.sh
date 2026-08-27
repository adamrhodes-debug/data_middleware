#!/usr/bin/env bash
# =================================================================
# Wi-fi sync, then push new guests to Como.
#
#   portal -> firestore -> [sync] -> central DB -> [trigger] -> master
#                                              -> [push] -> Como
#
# Run it by hand whenever you want to pull wi-fi and update Como:
#     bash ~/integrations/sync_wifi.sh
#
# What it does, in order:
#   1. Pulls wi-fi guests from Firestore into wifi_guests.
#      New guests merge into master_customers automatically via the
#      trigger (auto_merge_wifi_guests.sql).
#   2. Pushes to Como. como_push.py only touches brands whose keys are
#      in como-sync/.env (Pickl today), and only people not already
#      confirmed in Como - so it sends the new guests and skips
#      everyone already done.
#
# If step 1 fails, step 2 does NOT run - we never push against a
# half-loaded table.
# =================================================================

set -euo pipefail

BASE="$HOME/integrations"
WIFI_DIR="$BASE/wifi-sync"
COMO_DIR="$BASE/como-sync"

# Pace the Como calls under Como's 500/min ceiling. Matches what you
# used for the backlog.
RATE=300

log() { printf '\n=== %s ===\n' "$1"; }

# --- Sanity: the pieces we depend on are actually there ------------
[ -x "$WIFI_DIR/venv/bin/python3" ] || { echo "Missing $WIFI_DIR/venv"; exit 1; }
[ -x "$COMO_DIR/venv/bin/python3" ] || { echo "Missing $COMO_DIR/venv"; exit 1; }


# --- 1. Pull wi-fi from Firestore ---------------------------------
log "Wi-fi sync (Firestore -> wifi_guests)"
cd "$WIFI_DIR"
if ! venv/bin/python3 wifi_to_postgres.py; then
    echo
    echo "Wi-fi sync failed - NOT pushing to Como. Nothing was sent."
    echo "Fix the sync and run this again."
    exit 1
fi


# --- 2. Push new guests to Como -----------------------------------
# No --brand: como_push.py pushes only configured brands (Pickl now,
# Bonbird/Southpour automatically once their keys are added). Only
# people not already 'ok'/'exists' in como_push_state are sent, so
# this is naturally just the new arrivals.
log "Push to Como (new, configured-brand guests only)"
cd "$COMO_DIR"
venv/bin/python3 como_push.py --rate "$RATE"


log "Done"
echo "Wi-fi synced and new guests pushed. Bonbird/Southpour guests (if"
echo "any) are safely in the database and will push once their keys exist."
