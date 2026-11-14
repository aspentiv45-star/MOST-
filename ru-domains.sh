#!/usr/bin/env bash
# MOST — через российский сервер только сайты на .ru, .рф и .su, всё остальное — из Нидерландов.
# Запускать НА НИДЕРЛАНДСКОМ сервере, где уже настроен ru-route.sh (туннель awg1).
#
# Как работает: DNS-запросы клиентов VPN отвечает DNS сервера (most-rudns). Когда клиент спрашивает
# адрес сайта на .ru, DNS запоминает его IP, и трафик к нему уходит через туннель в Россию.
# Адреса .ru-сайтов DNS узнаёт у российского DNS (Яндекс) через тот же туннель — так сайт отдаёт
# российский сервер, а не зарубежное зеркало. Свои домены (например, vk.com) — по одному на строку
# в /etc/most/ru-domains.txt, затем снова sudo bash ru-domains.sh.
#
#   sudo bash ru-domains.sh       — включить режим доменов
#   sudo bash ru-domains.sh off   — вернуть прежний режим (все российские IP через Россию)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

RULES=/usr/local/sbin/most-ru-route
MODEF=/etc/most/ru-mode
RUCONF=/etc/most/rudns.conf
EXTRA=/etc/most/ru-domains.txt
MAINCONF=/etc/dnsmasq.d/most-ru.conf   # для DNS с блокировкой рекламы (adblock.sh), если он стоит
UNIT=/etc/systemd/system/most-rudns.service
RU_DNS="77.88.8.8 77.88.8.1"            # Яндекс DNS — через туннель (маршрут ставит ru-rules.sh)
PORT=5354

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -f /etc/amnezia/amneziawg/awg1.conf && -x $RULES ]] || die "сначала настройте туннель в Россию: ru-route.sh"
cd "$(dirname "$0")"

switch_mode() {
  "$RULES" down || true                 # снять правила старого режима
  install -m 755 ru-rules.sh "$RULES"   # новые правила (с режимом доменов)
  echo "$1" >"$MODEF"
  systemctl restart awg-quick@awg1
}

if [[ "${1:-}" == "off" ]]; then
  switch_mode ip
  systemctl disable --now most-rudns >/dev/null 2>&1 || true
  rm -f "$UNIT" "$MAINCONF"
  systemctl daemon-reload
  systemctl is-active --quiet dnsmasq && systemctl restart dnsmasq || true
  ipset destroy most-rudom 2>/dev/null || true
  echo "Готово: через Россию снова идут все российские IP."
  exit 0
fi

echo "==> Ставлю dnsmasq и ipset"
apt-get -o DPkg::Lock::Timeout=600 install -y -qq dnsmasq-base ipset dnsutils >/dev/null
command -v dnsmasq >/dev/null || die "не удалось установить dnsmasq"

# Домены: .ru, .рф (xn--p1ai), .su и свои из ru-domains.txt.
[[ -f $EXTRA ]] || printf '%s\n' "# Домены, которые тоже идут через Россию (по одному на строку), например:" "# vk.com" "# yandex.com" >"$EXTRA"
DOMAINS="ru/xn--p1ai/su"
while read -r d; do
  d=$(echo "$d" | tr -d '[:space:]' | tr 'A-Z' 'a-z')
  [[ -z $d || $d == \#* ]] && continue
  [[ $d =~ ^[a-z0-9.-]+$ ]] || { echo "    пропускаю «$d»: не похоже на домен"; continue; }
  DOMAINS="$DOMAINS/$d"
done <"$EXTRA"

dns_lines() {
  echo "ipset=/$DOMAINS/most-rudom"
  for s in $RU_DNS; do echo "server=/$DOMAINS/$s"; done
}

echo "==> DNS для клиентов (порт $PORT)"
{
  echo "# MOST: DNS для клиентов VPN — запоминает адреса сайтов на .ru (ru-domains.sh)."
  echo "port=$PORT"
  echo "interface=awg0"
  echo "interface=wgr0"
  echo "bind-dynamic"
  echo "no-resolv"
  echo "no-hosts"
  echo "server=1.1.1.1"
  echo "server=8.8.8.8"
  echo "cache-size=10000"
  echo "domain-needed"
  echo "bogus-priv"
  dns_lines
} >"$RUCONF"
dnsmasq --test --conf-file="$RUCONF" >/dev/null 2>&1 || die "ошибка в настройках DNS ($RUCONF)"

cat >"$UNIT" <<EOF
[Unit]
Description=MOST: DNS клиентов VPN — сайты на .ru через Россию
After=network-online.target awg-quick@awg0.service
Wants=network-online.target
[Service]
ExecStartPre=/usr/sbin/ipset create most-rudom hash:ip timeout 86400 -exist
ExecStart=/usr/sbin/dnsmasq -k --conf-file=$RUCONF --pid-file=
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

# DNS с блокировкой рекламы (если стоит) тоже должен запоминать адреса .ru.
if [[ -d /etc/dnsmasq.d ]] && systemctl list-unit-files dnsmasq.service >/dev/null 2>&1; then
  { echo "# MOST: сайты на .ru через Россию (ru-domains.sh)"; dns_lines; } >"$MAINCONF"
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  for i in awg0 wgr0; do ufw allow in on "$i" to any port $PORT comment "MOST ru DNS" >/dev/null; done
fi

echo "==> Включаю режим доменов"
switch_mode domains
systemctl daemon-reload
systemctl enable -q most-rudns
systemctl restart most-rudns
systemctl is-active --quiet dnsmasq && systemctl restart dnsmasq || true
sleep 2
systemctl is-active --quiet most-rudns || { journalctl -u most-rudns -n 20 --no-pager; die "DNS не запустился"; }

echo "==> Проверка"
AWG_IP=$(ip -4 -o addr show dev awg0 | awk '{print $4}' | cut -d/ -f1 | head -1)
A=$(dig +short +time=3 @"$AWG_IP" -p $PORT gosuslugi.ru 2>/dev/null | grep -E '^[0-9.]+$' | head -1 || true)
B=$(dig +short +time=3 @"$AWG_IP" -p $PORT example.com 2>/dev/null | grep -E '^[0-9.]+$' | head -1 || true)
echo "    gosuslugi.ru -> ${A:-нет ответа}, example.com -> ${B:-нет ответа}"
if [[ -n $A ]] && ipset test most-rudom "$A" >/dev/null 2>&1; then
  echo "    адрес gosuslugi.ru запомнен — пойдёт через Россию"
else
  echo "    ВНИМАНИЕ: адрес .ru не запомнился — пришлите вывод: journalctl -u most-rudns -n 30"
fi

cat <<EOF

Готово: сайты на .ru, .рф и .su идут через российский сервер, всё остальное — из Нидерландов.
Конфиги клиентов менять не нужно. Проверка с телефона с включённым VPN:
  2ip.ru     — Россия
  ipinfo.io  — Нидерланды
Добавить свои домены: sudo nano $EXTRA, затем sudo bash ru-domains.sh
Вернуть прежний режим: sudo bash ru-domains.sh off
EOF
