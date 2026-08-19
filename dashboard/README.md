# Integrations dashboard

A web view over the customer data pipeline: what's running, what's
clean, what's been pushed to Como, and what needs a decision.

## What's on it

| Tab | For |
|---|---|
| **Status** | Did anything break? Backfill progress, recent runs, source breakdowns |
| **Data quality** | Tag distribution, field completeness, customers with no brand tag |
| **Consent** | Who you can email, by source and brand |
| **Como push** | Per-brand progress, plus CSV export in Como's format |
| **Conflicts** | Records Como rejected, with a way to clear them for retry |
| **Lookup** | Find a customer, see every source they came from and what would be sent |
| **Run** | Trigger any loader and watch its output live |
| **Registry** | Edit source mappings, blocked domains and country tags without SQL |

The strip across the top is always visible: sources → master → Como,
with live counts. Amber means something wants your attention.

---

## Setup

### 1. Install

```bash
cd ~/integrations/dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. Configure

```bash
nano ~/integrations/dashboard/.env
```

```
DB=dbname=ingest user=ingest password=yolk2026db host=localhost

# 127.0.0.1 means nothing is reachable from outside the VM.
DASH_HOST=127.0.0.1
DASH_PORT=8000

# Where the loader folders live
INTEGRATIONS_DIR=/home/apps/integrations

# Only needed if you expose it beyond localhost
DASH_USER=
DASH_PASS=
```

```bash
chmod 600 ~/integrations/dashboard/.env
```

The Como brand keys are read from the environment for the "is this
brand set up" indicator. If you want that to work, either add the
`PICKL_COMO_API_KEY` line here too, or start the dashboard from a
shell where it's already set.

### 3. Start it

```bash
cd ~/integrations/dashboard
venv/bin/python3 app.py
```

It prints `Dashboard on http://127.0.0.1:8000`.

### 4. Reach it from your Mac

Nothing is exposed to the internet, so tunnel to it — the same way
DBeaver reaches Postgres:

```bash
ssh -i ~/.ssh/google_compute_engine -L 8000:localhost:8000 adam.rhodes@34.14.60.191
```

Leave that running, then open **http://localhost:8000** in your browser.

---

## Keeping it running

Right now it stops when you close the terminal. To run it as a service:

```bash
sudo tee /etc/systemd/system/integrations-dashboard.service > /dev/null <<'EOF'
[Unit]
Description=Integrations dashboard
After=network.target postgresql.service

[Service]
User=apps
WorkingDirectory=/home/apps/integrations/dashboard
ExecStart=/home/apps/integrations/dashboard/venv/bin/python3 app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now integrations-dashboard
sudo systemctl status integrations-dashboard
```

---

## Putting it on integrations.yolkbrands.com

Only do this once TLS and a password are in place — the dashboard
shows customer names, emails and birthdays.

1. Point the subdomain's DNS A record at the VM's IP.
2. Set a password in `.env`:
   ```
   DASH_USER=adam
   DASH_PASS=something-long-and-random
   ```
   Leave `DASH_HOST=127.0.0.1` — nginx will front it.
3. Add an nginx site:
   ```nginx
   server {
       server_name integrations.yolkbrands.com;
       location / {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```
4. Get a certificate:
   ```bash
   sudo certbot --nginx -d integrations.yolkbrands.com
   ```

The app refuses to start on a non-localhost host without `DASH_PASS`
set, so you can't accidentally publish it unprotected.

---

## Notes

- **Jobs are whitelisted.** Only the ones in the `JOBS` dict in
  `app.py` can be triggered, so a malformed request can't run
  arbitrary commands.
- **Registry edits are live.** Changing a source's tag expression
  takes effect on the next "Rebuild master table" run, not
  immediately.
- **Run output is kept in memory** for the current process plus a
  copy in the `dashboard_runs` table. Restarting loses the live
  buffer but not the history.
- **The export** produces Como's exact CSV column names, with tags
  as a JSON array — useful as a fallback if the API path is ever
  unavailable.
