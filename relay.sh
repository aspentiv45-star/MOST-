#!/usr/bin/env bash
# MOST — «вход» для обхода белых списков. Запускать НА СЕРВЕРЕ-ВХОДЕ
# (российский сервер, IP которого пропускают операторы, например Yandex Cloud).
#
# Клиент подключается к IP этого сервера, а он пересылает UDP-трафик
# на AmneziaWG основного сервера. Ключей и конфигов здесь нет.
#
#   sudo bash relay.sh --to 185.121.13.60:51234
#   sudo bash relay.sh --to 185.121.13.60:51234,51235   — несколько портов (телефоны и роутеры)
#   sudo bash relay.sh off
set -euo pipefail

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"

RULES=/usr/local/sbin/most-relay
UNIT=/etc/systemd/system/most-relay.service

if [[ "${1:-}" == "off" ]]; then
  systemctl disable --now most-relay 2>/dev/null || true
  rm -f "$RULES" "$UNIT"
  systemctl daemon-reload
  echo "Вход отключён."
  exit 0
fi

[[ "${1:-}" == "--to" && -n "${2:-}" ]] || die "использование: sudo bash relay.sh --to IP_ОСНОВНОГО_СЕРВЕРА:ПОРТ[,ПОРТ2]"
TARGET="$2"
DEST_IP="${TARGET%:*}"
PORTS="${TARGET##*:}"
[[ "$DEST_IP" =~ ^[0-9.]+$ && "$PORTS" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "формат: --to 185.121.13.60:51234 или 185.121.13.60:51234,51235"

WAN="$(ip -4 route show default | awk '{print $5; exit}')"
[[ -n "$WAN" ]] || die "не нашёл сетевой интерфейс с выходом в интернет"

echo "==> Включаю пересылку UDP $PORTS -> $DEST_IP"
echo "net.ipv4.ip_forward=1" >/etc/sysctl.d/99-most-relay.conf
sysctl -q --system || true

# Снимаем правила прошлой установки, прежде чем записать новые.
systemctl stop most-relay 2>/dev/null || true
[[ -x "$RULES" ]] && "$RULES" down 2>/dev/null || true

cat >"$RULES" <<EOF
#!/bin/bash
# MOST: клиенты -> этот сервер:ПОРТ/udp -> $DEST_IP:ПОРТ, порты: $PORTS
case "\$1" in
  up)   OP=-A; OPI=-I ;;
  down) OP=-D; OPI=-D ;;
  *) exit 1 ;;
esac
for P in ${PORTS//,/ }; do
  iptables -t nat \$OP PREROUTING -i $WAN -p udp --dport \$P -j DNAT --to-destination $DEST_IP:\$P
  iptables -t nat \$OP POSTROUTING -o $WAN -d $DEST_IP -p udp --dport \$P -j MASQUERADE
  iptables \$OPI FORWARD -d $DEST_IP -p udp --dport \$P -j ACCEPT
  iptables \$OPI FORWARD -s $DEST_IP -p udp --sport \$P -j ACCEPT
done
EOF
chmod 755 "$RULES"

cat >"$UNIT" <<EOF
[Unit]
Description=MOST: вход для клиентов -> $DEST_IP ($PORTS)
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
systemctl enable most-relay >/dev/null 2>&1
systemctl restart most-relay

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  for P in ${PORTS//,/ }; do
    ufw allow "$P"/udp comment "MOST relay" >/dev/null
  done
fi

PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || echo "IP_ЭТОГО_СЕРВЕРА")"
cat <<EOF

Готово. Вход: $PUBLIC_IP -> $DEST_IP, UDP-порты $PORTS

Если это Yandex Cloud или другое облако с группами безопасности —
разрешите в них входящие UDP-порты $PORTS.

Теперь на ОСНОВНОМ сервере, в папке MOST-, выполните:

  sudo bash install.sh --relay $PUBLIC_IP

(остальные параметры можно не повторять — они сохранены)
EOF
