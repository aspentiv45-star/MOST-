#!/usr/bin/env bash
# MOST — «вход» для обхода белых списков. Запускать НА СЕРВЕРЕ-ВХОДЕ
# (российский сервер, IP которого пропускают операторы, например Yandex Cloud).
#
# Клиент подключается к IP этого сервера, а он пересылает UDP-трафик
# на AmneziaWG основного сервера. Ключей и конфигов здесь нет.
#
#   sudo bash relay.sh --to 185.121.13.60:51234
#   sudo bash relay.sh --to 185.121.13.60:51234,51235   — несколько портов (телефоны и роутеры)
#   sudo bash relay.sh --to 185.121.13.60:51234=443,51235 — клиенты приходят на 51234, а дальше
#        трафик идёт на порт 443 основного сервера (там его принимает alt-port.sh): если UDP
#        на обычный порт основного сервера режут по дороге, на 443 (как QUIC) часто пропускают.
#   sudo bash relay.sh --to 185.121.13.60:51234=443,51235,t8443=443 — «t» перед портом: TCP, а не UDP
#        (для VLESS из vless.sh: клиенты приходят на TCP 8443, дальше — TCP 443 основного сервера).
#   sudo bash relay.sh off
set -euo pipefail

# Ubuntu после установки пакетов спрашивает (needrestart), какие службы перезапустить; в тихом режиме
# вопрос не виден и установка висит вечно — пусть перезапускает сама и ни о чём не спрашивает.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

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
[[ "$DEST_IP" =~ ^[0-9.]+$ && "$PORTS" =~ ^t?[0-9]+(=[0-9]+)?(,t?[0-9]+(=[0-9]+)?)*$ ]] \
  || die "формат: --to 185.121.13.60:51234 или 185.121.13.60:51234,51235 или 185.121.13.60:51234=443,51235,t8443=443"

WAN="$(ip -4 route show default | awk '{print $5; exit}')"
[[ -n "$WAN" ]] || die "не нашёл сетевой интерфейс с выходом в интернет"

echo "==> Включаю пересылку $PORTS -> $DEST_IP (t — TCP, остальное — UDP)"
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
for SPEC in ${PORTS//,/ }; do
  PR=udp; [[ \$SPEC == t* ]] && PR=tcp && SPEC=\${SPEC#t}
  P=\${SPEC%%=*}; R=\${SPEC#*=}   # порт на входе = порт на основном сервере (по умолчанию тот же)
  iptables -t nat \$OP PREROUTING -i $WAN -p \$PR --dport \$P -j DNAT --to-destination $DEST_IP:\$R
  iptables -t nat \$OP POSTROUTING -o $WAN -d $DEST_IP -p \$PR --dport \$R -j MASQUERADE
  iptables \$OPI FORWARD -d $DEST_IP -p \$PR --dport \$R -j ACCEPT
  iptables \$OPI FORWARD -s $DEST_IP -p \$PR --sport \$R -j ACCEPT
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
  for SPEC in ${PORTS//,/ }; do
    if [[ $SPEC == t* ]]; then SPEC=${SPEC#t}; ufw allow "${SPEC%%=*}"/tcp comment "MOST relay" >/dev/null
    else ufw allow "${SPEC%%=*}"/udp comment "MOST relay" >/dev/null; fi
  done
fi

PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || echo "IP_ЭТОГО_СЕРВЕРА")"
cat <<EOF

Готово. Вход: $PUBLIC_IP -> $DEST_IP, порты $PORTS (t — TCP, остальное — UDP)

Если это Yandex Cloud или другое облако с группами безопасности —
разрешите в них входящие порты $PORTS (t — TCP, остальное — UDP).

Теперь на ОСНОВНОМ сервере, в папке MOST-, выполните:

  sudo bash install.sh --relay $PUBLIC_IP

(остальные параметры можно не повторять — они сохранены)
EOF
