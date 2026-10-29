#!/usr/bin/env bash
# MOST — российские сайты через российский сервер. Запускать НА НИДЕРЛАНДСКОМ сервере.
#
# Клиенты пускают в VPN весь трафик (так работает в любом приложении, в том
# числе в AmneziaVPN). Трафик к российским IP сервер отправляет через туннель
# awg1 на российский сервер, остальное — из Нидерландов. Так банки, Госуслуги
# и маркетплейсы видят российский IP. Telegram, Meta, Google и X — всегда из NL.
#
#   sudo bash ru-route.sh 'СТРОКА-КЛЮЧ из ru-node.sh'
#   sudo bash ru-route.sh off      — отключить
set -euo pipefail

AWG_DIR=/etc/amnezia/amneziawg
CONF="$AWG_DIR/awg1.conf"
RULES=/usr/local/sbin/most-ru-route
LIST=/var/lib/most/ru.zone

die() { echo "Ошибка: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ $# -eq 1 ]] || die "использование: sudo bash ru-route.sh 'СТРОКА-КЛЮЧ' (её печатает ru-node.sh)"

if [[ "$1" == "off" ]]; then
  systemctl disable --now most-ru-update.timer awg-quick@awg1 2>/dev/null || true
  rm -f /etc/systemd/system/most-ru-update.{service,timer} "$CONF"
  systemctl daemon-reload
  # Возвращаем раздельный туннель в конфигах клиентов.
  sed -i '/^SPLIT_RU=/d' /etc/most/bot.env 2>/dev/null && systemctl restart most-bot || true
  echo "Отключено. Клиентам нужно заново скачать конфиги в боте."
  exit 0
fi

eval "$(python3 -c '
import base64, json, shlex, sys
d = json.loads(base64.b64decode(sys.argv[1]))
for k, v in d.items():
    print(f"{k.upper()}={shlex.quote(str(v))}")
' "$1")" || die "строка-ключ повреждена — скопируйте её из вывода ru-node.sh целиком"

[[ -f /etc/most/bot.env && -f /opt/most/split.py ]] || die "сначала обновите MOST: git pull && sudo bash install.sh"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq ipset curl >/dev/null

echo "==> Скрипт маршрутизации"
cat >"$RULES" <<'EOF'
#!/bin/bash
# MOST: клиенты awg0 -> российские IP (ipset most-ru) -> туннель awg1.
SET=most-ru
LIST=/var/lib/most/ru.zone
FORCE=/var/lib/most/force.zone
MARK=0x65
TABLE=101

load_set() {
  [[ -s $LIST ]] || return 1
  ipset create $SET hash:net -exist
  ipset create $SET-new hash:net -exist
  ipset flush $SET-new
  sed 's/^/add most-ru-new /' "$LIST" | ipset restore -exist
  # Telegram, Meta, Google, X: исключения внутри российских сетей — идут из Нидерландов.
  [[ -s $FORCE ]] && sed 's/^/add most-ru-new /; s/$/ nomatch/' "$FORCE" | ipset restore -exist
  ipset swap $SET-new $SET
  ipset destroy $SET-new
}

case "$1" in
  up)
    load_set
    ip route replace default dev awg1 table $TABLE
    ip rule show | grep -q "fwmark $MARK" || ip rule add fwmark $MARK table $TABLE
    sysctl -q -w net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.awg1.rp_filter=2
    iptables -t mangle -A PREROUTING -i awg0 -m set --match-set $SET dst -j MARK --set-mark $MARK
    iptables -t mangle -A FORWARD -o awg1 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
    iptables -t nat -A POSTROUTING -o awg1 -j MASQUERADE ;;
  down)
    iptables -t mangle -D PREROUTING -i awg0 -m set --match-set $SET dst -j MARK --set-mark $MARK
    iptables -t mangle -D FORWARD -o awg1 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
    iptables -t nat -D POSTROUTING -o awg1 -j MASQUERADE
    ip rule del fwmark $MARK table $TABLE
    ip route flush table $TABLE ;;
  update)
    # Список российских сетей; проверяем, что он не пустой и похож на правду.
    tmp=$(mktemp)
    if python3 /opt/most/split.py --ru >"$tmp" && [[ $(wc -l <"$tmp") -gt 1000 ]]; then
      install -m 644 "$tmp" "$LIST"
    fi
    python3 /opt/most/split.py --force >"$tmp" && install -m 644 "$tmp" "$FORCE"
    rm -f "$tmp"
    if ip link show awg1 >/dev/null 2>&1; then load_set; fi ;;
esac
EOF
chmod 755 "$RULES"

echo "==> Загружаю список российских сетей"
mkdir -p /var/lib/most
"$RULES" update
[[ -s "$LIST" ]] || die "не удалось скачать список российских сетей"
echo "    сетей в списке: $(wc -l <"$LIST")"

echo "==> Поднимаю туннель до российского сервера $RU_IP"
umask 077
cat >"$CONF" <<EOF
[Interface]
PrivateKey = $NL_PRIV
Address = 10.66.0.2/30
Table = off
Jc = 4
Jmin = 40
Jmax = 70
S1 = $S1
S2 = $S2
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4
PostUp = $RULES up
PostDown = $RULES down

[Peer]
PublicKey = $RU_PUB
Endpoint = $RU_IP:$PORT
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF
umask 022
systemctl enable awg-quick@awg1 >/dev/null 2>&1
systemctl restart awg-quick@awg1
systemctl is-active --quiet awg-quick@awg1 || { journalctl -u awg-quick@awg1 -n 20 --no-pager; die "awg1 не запустился"; }

cat >/etc/systemd/system/most-ru-update.service <<EOF
[Unit]
Description=MOST: обновить список российских сетей
[Service]
Type=oneshot
ExecStart=$RULES update
EOF
cat >/etc/systemd/system/most-ru-update.timer <<EOF
[Unit]
Description=MOST: обновлять список российских сетей раз в сутки
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now most-ru-update.timer >/dev/null 2>&1

echo "==> Проверяю туннель"
sleep 3
if ping -c 2 -W 3 10.66.0.1 >/dev/null 2>&1; then
  echo "    туннель до российского сервера работает"
else
  echo "    ВНИМАНИЕ: российский сервер не отвечает через туннель."
  echo "    Проверьте, что UDP-порт $PORT открыт в файрволе хостинга российского сервера."
fi

echo "==> Конфиги клиентов: весь трафик в VPN (работает в любом приложении)"
sed -i '/^SPLIT_RU=/d' /etc/most/bot.env
echo "SPLIT_RU=0" >>/etc/most/bot.env
systemctl restart most-bot

cat <<EOF

Готово. Российские адреса идут через $RU_IP, остальное — из Нидерландов.

Клиентам нужно один раз скачать конфиг заново: «📄 Мои конфиги» → «⬇️ Скачать».
Проверка с телефона с включённым VPN:
  ipinfo.io      — должны быть Нидерланды
  2ip.ru         — должна быть Россия ($RU_IP)
EOF
