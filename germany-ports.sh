#!/usr/bin/env bash
# Дополнительные внешние порты для существующего немецкого awg-de.
set -euo pipefail
[[ $EUID == 0 ]] || { echo "Запустите через sudo" >&2; exit 1; }
cd "$(dirname "$0")"

chain_rule() {
  local op=$1 alt=$2
  iptables -w 5 -t nat "$op" PREROUTING -m addrtype --dst-type LOCAL -p udp --dport "$alt" \
    -m comment --comment most-germany-altport -j REDIRECT --to-ports 51820
}

case "${1:-install}" in
  up)
    [[ "$(awg show awg-de listen-port)" == 51820 ]] || { echo "Ожидался awg-de на 51820/udp" >&2; exit 1; }
    for alt in 8054 443; do
      listeners=$(ss -H -lun "sport = :$alt")
      [[ -z "$listeners" ]] || { echo "Порт $alt/udp уже занят: $listeners" >&2; exit 1; }
    done
    for alt in 8054 443; do
      chain_rule -C "$alt" 2>/dev/null || chain_rule -I "$alt"
    done
    ;;
  down)
    for alt in 8054 443; do
      while chain_rule -C "$alt" 2>/dev/null; do chain_rule -D "$alt"; done
    done
    ;;
  install)
    [[ "$(awg show awg-de listen-port)" == 51820 ]] || { echo "Проверьте интерфейс awg-de" >&2; exit 1; }
    systemctl is-active --quiet most-germany-expire.timer
    install -m 0755 germany-ports.sh /usr/local/sbin/most-germany-ports
    cat >/etc/systemd/system/most-germany-ports.service <<'EOF'
[Unit]
Description=MOST Germany UDP 8054 and 443
Requires=awg-quick@awg-de.service
After=awg-quick@awg-de.service
PartOf=awg-quick@awg-de.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/most-germany-ports up
ExecStop=/usr/local/sbin/most-germany-ports down

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now most-germany-ports.service
    /usr/local/sbin/most-germany-ports up
    # После REDIRECT входной фильтр видит конечный порт 51820.
    if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
      ufw allow 51820/udp
    fi
    helper=$(mktemp /usr/local/sbin/.most-germany-peer-sync.XXXXXX)
    trap 'rm -f "$helper"' EXIT
    install -m 0755 germany-peer-sync.py "$helper"
    mv -f "$helper" /usr/local/sbin/most-germany-peer-sync
    echo "Готово. Разрешите 8054/UDP и 443/UDP в файрволе хостинга."
    ;;
  *) echo "Ожидалось install, up или down" >&2; exit 2 ;;
esac
