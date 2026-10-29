#!/usr/bin/env bash
# MOST — удаление бота и AmneziaWG. Ключи и конфиги сохраняются в /root/most-backup.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Запустите через sudo" >&2; exit 1; }

BACKUP=/root/most-backup-$(date +%s)
mkdir -p "$BACKUP"
cp -a /etc/amnezia/amneziawg /var/lib/most /etc/most "$BACKUP"/ 2>/dev/null || true

systemctl disable --now most-bot most-abuse awg-quick@awg0 awg-quick@wgr0 2>/dev/null || true
rm -rf /opt/most /etc/most /var/lib/most /etc/systemd/system/most-bot.service /etc/systemd/system/most-abuse.service /usr/local/sbin/most-abuse /etc/sysctl.d/99-most.conf
rm -f /etc/amnezia/amneziawg/awg0.conf /etc/amnezia/amneziawg/wgr0.conf
systemctl daemon-reload
echo "Удалено. Резервная копия ключей и конфигов: $BACKUP"
