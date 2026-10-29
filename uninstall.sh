#!/usr/bin/env bash
# MOST — удаление моста с российского сервера.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Запустите через sudo" >&2; exit 1; }

systemctl stop xray 2>/dev/null || true
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ remove --purge
rm -f /root/most-client.txt /etc/sysctl.d/99-most.conf
echo "Мост удалён."
