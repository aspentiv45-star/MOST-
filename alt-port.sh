#!/usr/bin/env bash
# MOST — дополнительный UDP-порт для AmneziaWG на основном сервере.
# Пакеты, пришедшие на этот порт, попадают в AmneziaWG (awg0) так же, как на основной.
# Нужен, когда UDP на обычный порт основного сервера режут по дороге из России, а на 443
# (его использует QUIC — YouTube, Google) пропускают. На входе: relay.sh --to IP:ПОРТ=443.
#
#   sudo bash alt-port.sh 443     — принимать AmneziaWG ещё и на UDP 443
#   sudo bash alt-port.sh off     — убрать
set -euo pipefail

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"

RULES=/usr/local/sbin/most-altport
UNIT=/etc/systemd/system/most-altport.service

if [[ "${1:-}" == "off" ]]; then
  systemctl disable --now most-altport 2>/dev/null || true
  rm -f "$RULES" "$UNIT"
  systemctl daemon-reload
  echo "Дополнительный порт убран."
  exit 0
fi

ALT="${1:-}"
[[ "$ALT" =~ ^[0-9]+$ ]] || die "использование: sudo bash alt-port.sh 443 | off"
AWG_PORT="$(awk -F' *= *' '$1=="ListenPort"{print $2; exit}' /etc/amnezia/amneziawg/awg0.conf)"
[[ -n "$AWG_PORT" ]] || die "не нашёл порт AmneziaWG в /etc/amnezia/amneziawg/awg0.conf"

# Снимаем правила прошлой установки и ручные правила с тем же действием.
systemctl stop most-altport 2>/dev/null || true
while iptables -t nat -D PREROUTING -p udp --dport "$ALT" -j REDIRECT --to-ports "$AWG_PORT" 2>/dev/null; do :; done

cat >"$RULES" <<EOF
#!/bin/bash
# MOST: UDP $ALT -> AmneziaWG ($AWG_PORT)
case "\$1" in
  up)   iptables -t nat -I PREROUTING -p udp --dport $ALT -j REDIRECT --to-ports $AWG_PORT ;;
  down) while iptables -t nat -D PREROUTING -p udp --dport $ALT -j REDIRECT --to-ports $AWG_PORT 2>/dev/null; do :; done ;;
esac
EOF
chmod 755 "$RULES"

cat >"$UNIT" <<EOF
[Unit]
Description=MOST: AmneziaWG также на UDP $ALT
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$RULES up
ExecStop=-$RULES down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable most-altport >/dev/null 2>&1
systemctl restart most-altport

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$ALT"/udp comment "MOST AmneziaWG alt" >/dev/null
fi

echo "Готово: AmneziaWG принимает подключения и на UDP $ALT (сохранится после перезагрузки)."
echo "На входе: sudo bash relay.sh --to ЭТОТ_IP:$AWG_PORT=$ALT,ПОРТ_РОУТЕРОВ"
