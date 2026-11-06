#!/usr/bin/env bash
# MOST — запасные порты туннеля для российских сайтов. Запускать НА РОССИЙСКОМ сервере (Москва).
#
# Направляет все UDP-порты 1024–9999, на которые приходит ОСНОВНОЙ сервер, в туннель awg1.
# Тогда основной сервер (бот) сам переключает туннель на другой порт, если текущий начали резать, —
# менять что-то здесь больше не нужно. Другим адресам эти порты закрыты.
#
#   sudo bash ru-ports.sh IP_ОСНОВНОГО_СЕРВЕРА
#   sudo bash ru-ports.sh off
set -euo pipefail
die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"

RULES=/usr/local/sbin/most-ru-ports
UNIT=/etc/systemd/system/most-ru-ports.service
CONF=/etc/amnezia/amneziawg/awg1.conf

if [[ "${1:-}" == "off" ]]; then
  systemctl disable --now most-ru-ports 2>/dev/null || true
  rm -f "$RULES" "$UNIT"
  systemctl daemon-reload
  echo "Запасные порты отключены: туннель принимает только свой порт."
  exit 0
fi

NL="${1:-}"
[[ "$NL" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "использование: sudo bash ru-ports.sh IP_ОСНОВНОГО_СЕРВЕРА"
[[ -f $CONF ]] || die "нет $CONF — сначала ru-node.sh"

systemctl stop most-ru-ports 2>/dev/null || true
cat >"$RULES" <<EOF
#!/bin/bash
# MOST: UDP 1024-9999 от $NL -> порт туннеля awg1
PORT=\$(awk -F' *= *' '\$1=="ListenPort"{print \$2; exit}' $CONF)
[[ -n "\$PORT" ]] || exit 1
case "\$1" in
  up)   iptables -t nat -C PREROUTING -s $NL -p udp --dport 1024:9999 -j REDIRECT --to-ports \$PORT 2>/dev/null \\
          || iptables -t nat -I PREROUTING -s $NL -p udp --dport 1024:9999 -j REDIRECT --to-ports \$PORT ;;
  down) iptables -t nat -D PREROUTING -s $NL -p udp --dport 1024:9999 -j REDIRECT --to-ports \$PORT 2>/dev/null || true ;;
esac
exit 0
EOF
chmod 755 "$RULES"
cat >"$UNIT" <<EOF
[Unit]
Description=MOST: запасные порты туннеля для основного сервера
After=network-online.target awg-quick@awg1.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$RULES up
ExecStop=-$RULES down
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now most-ru-ports >/dev/null 2>&1
systemctl restart most-ru-ports

# После перенаправления порт пакета уже равен порту туннеля — файрвол пропускает его по прежнему правилу.
PORT="$(awk -F' *= *' '$1=="ListenPort"{print $2; exit}' "$CONF")"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow from "$NL" to any port "$PORT" proto udp comment "MOST awg1" >/dev/null
fi

cat <<EOF

Готово: основной сервер ($NL) может подключать туннель на любой UDP-порт 1024–9999 этого сервера.

Теперь на ОСНОВНОМ сервере выполните:

  sudo sed -i '/^TUN_PORT_RANGE=/d' /etc/most/bot.env && echo 'TUN_PORT_RANGE=1024-9999' | sudo tee -a /etc/most/bot.env && sudo systemctl restart most-bot

Бот раз в 10 минут проверяет потери в туннеле и при потерях пишет вам с кнопкой «🔄 Подобрать другой порт».
EOF
