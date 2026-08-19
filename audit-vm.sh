#!/usr/bin/env bash
# ==============================================================
# VM audit — READ ONLY
#
# Changes nothing. Installs nothing. Starts/stops nothing.
# Every command here only reads state.
#
# Run:  bash audit-vm.sh > vm-audit.txt 2>&1
# Then read vm-audit.txt (or paste it back for review).
#
# Some sections need sudo to show process names/owners. Without
# sudo it still works, just with less detail.
# ==============================================================

section() { printf '\n\n═══ %s ═══\n\n' "$1"; }

section "SYSTEM"
hostnamectl 2>/dev/null || uname -a
echo
echo "Uptime:"; uptime
echo
echo "CPU/RAM:"; nproc; free -h

section "DISK SPACE"
df -h -x tmpfs -x devtmpfs
echo
echo "Largest directories under /opt, /var, /srv, /home:"
sudo du -xh --max-depth=2 /opt /var /srv /home 2>/dev/null | sort -rh | head -25

section "RUNNING SERVICES (systemd)"
systemctl list-units --type=service --state=running --no-pager

section "ENABLED AT BOOT"
systemctl list-unit-files --type=service --state=enabled --no-pager

section "TIMERS (scheduled jobs)"
systemctl list-timers --all --no-pager

section "LISTENING PORTS"
# Shows what's bound where, and which process owns it.
sudo ss -tulpn 2>/dev/null || ss -tuln

section "DOCKER / CONTAINERS"
if command -v docker >/dev/null 2>&1; then
    echo "Docker present."
    sudo docker ps -a 2>/dev/null
    echo; echo "Compose projects:"
    sudo docker compose ls 2>/dev/null
else
    echo "No docker binary."
fi
if command -v podman >/dev/null 2>&1; then echo "Podman present:"; podman ps -a; fi

section "WEB SERVERS"
for s in nginx apache2 httpd caddy; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${s}.service"; then
        echo "--- $s: $(systemctl is-active $s 2>/dev/null) / $(systemctl is-enabled $s 2>/dev/null)"
    fi
done
echo
echo "nginx sites (if any):"
ls -la /etc/nginx/sites-enabled/ 2>/dev/null || echo "  none"
echo "apache sites (if any):"
ls -la /etc/apache2/sites-enabled/ 2>/dev/null || echo "  none"

section "DATABASES ALREADY INSTALLED"
for s in postgresql mysql mariadb mongod redis-server; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${s}"; then
        echo "--- $s: $(systemctl is-active $s 2>/dev/null) / $(systemctl is-enabled $s 2>/dev/null)"
    fi
done

echo
echo "PostgreSQL detail (this matters most — we may be sharing it):"
if command -v psql >/dev/null 2>&1; then
    psql --version
    echo
    echo "Clusters:"
    pg_lsclusters 2>/dev/null
    echo
    echo "Existing databases:"
    sudo -u postgres psql -c "\l" 2>/dev/null || echo "  (couldn't connect as postgres)"
    echo
    echo "Existing roles:"
    sudo -u postgres psql -c "\du" 2>/dev/null || echo "  (couldn't connect as postgres)"
    echo
    echo "Listening on:"
    sudo -u postgres psql -tAc "SHOW listen_addresses; SHOW port;" 2>/dev/null
else
    echo "  psql not installed — Postgres is not present."
fi

section "PYTHON"
python3 --version 2>/dev/null
echo "Python binaries:"; ls /usr/bin/python3* 2>/dev/null
echo
echo "Existing virtualenvs found under /opt, /srv, /home:"
sudo find /opt /srv /home -maxdepth 4 -name "pyvenv.cfg" 2>/dev/null | head -20
echo
echo "System-wide pip packages (if any — a sign of past installs):"
pip3 list --user 2>/dev/null | head -20

section "NODE / OTHER RUNTIMES"
command -v node >/dev/null && echo "node: $(node --version)"
command -v npm  >/dev/null && echo "npm:  $(npm --version)"
command -v php  >/dev/null && echo "php:  $(php --version | head -1)"
command -v java >/dev/null && echo "java: $(java -version 2>&1 | head -1)"

section "CRON JOBS"
echo "--- Root crontab:"; sudo crontab -l 2>/dev/null || echo "  none"
echo
echo "--- Per-user crontabs:"
for u in $(cut -f1 -d: /etc/passwd); do
    entries=$(sudo crontab -u "$u" -l 2>/dev/null | grep -v '^#' | grep -v '^$')
    [ -n "$entries" ] && { echo "  [$u]"; echo "$entries" | sed 's/^/    /'; }
done
echo
echo "--- System cron directories:"
ls -la /etc/cron.d/ /etc/cron.daily/ /etc/cron.hourly/ 2>/dev/null

section "USERS & SERVICE ACCOUNTS"
echo "Human/service accounts (UID >= 1000 or with a shell):"
awk -F: '($3>=1000 && $3<65534) || $7 ~ /(bash|sh|zsh)$/ {printf "  %-20s uid=%-6s shell=%s\n", $1, $3, $7}' /etc/passwd
echo
echo "Does a 'como-sync' user already exist?"
id como-sync 2>/dev/null || echo "  no — good, name is free"

section "NAME COLLISION CHECK (for this project)"
for p in /opt/como-sync /etc/como-sync /var/log/como-sync; do
    [ -e "$p" ] && echo "  EXISTS: $p" || echo "  free:   $p"
done
for u in como-sync.service como-sync.timer; do
    [ -e "/etc/systemd/system/$u" ] && echo "  EXISTS: /etc/systemd/system/$u" || echo "  free:   /etc/systemd/system/$u"
done

section "FIREWALL"
sudo ufw status verbose 2>/dev/null || echo "  ufw not in use"
echo
sudo iptables -L -n 2>/dev/null | head -30 || echo "  (couldn't read iptables)"
echo
command -v firewall-cmd >/dev/null && sudo firewall-cmd --list-all 2>/dev/null

section "RECENT SERVICE FAILURES"
systemctl list-units --state=failed --no-pager

section "PACKAGE MANAGER STATE"
echo "Pending upgrades:"
apt list --upgradable 2>/dev/null | head -20
echo
echo "Reboot required?"
[ -f /var/run/reboot-required ] && cat /var/run/reboot-required || echo "  no"

section "AUDIT COMPLETE"
echo "Nothing was modified. Review the output above before installing anything."
