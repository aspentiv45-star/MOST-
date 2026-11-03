#!/usr/bin/env bash
# MOST — блокировка рекламы для клиентов VPN. Запускать на ОСНОВНОМ сервере.
#
# Ставит DNS-сервер dnsmasq на адресе VPN-сервера (10.8.0.1), который отвечает «пусто» на домены
# рекламы и слежки (фильтр AdGuard DNS — в нём и российские рекламные сети). Список обновляется
# раз в сутки. Бот начинает выдавать конфиги с DNS = 10.8.0.1; клиент может выключить блокировку
# у своего конфига кнопкой «🛡 Реклама».
#
#   sudo bash adblock.sh        — включить
#   sudo bash adblock.sh off    — выключить (конфиги снова с обычным DNS)
set -euo pipefail

# Ubuntu после установки пакетов спрашивает (needrestart), какие службы перезапустить; в тихом режиме
# вопрос не виден и установка висит вечно — пусть перезапускает сама и ни о чём не спрашивает.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -f /etc/most/bot.env ]] || die "это не основной сервер MOST (нет /etc/most/bot.env)"

ENV=/etc/most/bot.env
CONF=/etc/dnsmasq.d/most.conf
LIST=/var/lib/most/adblock.conf
UPDATE=/usr/local/sbin/most-adblock-update
DNS_IP="$(awk -F' *= *' '$1=="Address"{split($2,a,"/"); print a[1]; exit}' /etc/amnezia/amneziawg/awg0.conf)"
[[ -n "$DNS_IP" ]] || die "не нашёл адрес VPN-сервера в awg0.conf"

if [[ "${1:-}" == "off" ]]; then
  systemctl disable --now most-adblock-update.timer dnsmasq 2>/dev/null || true
  rm -f "$CONF" /etc/systemd/system/most-adblock-update.{service,timer}
  systemctl daemon-reload
  sed -i '/^ADBLOCK_DNS=/d' "$ENV"
  systemctl restart most-bot
  echo "Блокировка рекламы выключена. Клиентам нужно скачать конфиги заново, чтобы вернуть обычный DNS."
  exit 0
fi

echo "==> Устанавливаю dnsmasq"
export DEBIAN_FRONTEND=noninteractive
# Пакет при установке пытается занять порт 53 на всех адресах — не даём ему стартовать до настройки.
mkdir -p /etc/dnsmasq.d /var/lib/most
# Не делать dnsmasq системным DNS самого сервера: он слушает только интерфейсы VPN.
touch /etc/default/dnsmasq
sed -i '/^DNSMASQ_EXCEPT=/d; /^IGNORE_RESOLVCONF=/d' /etc/default/dnsmasq
printf 'DNSMASQ_EXCEPT="lo"\nIGNORE_RESOLVCONF=yes\n' >>/etc/default/dnsmasq
[[ -f "$LIST" ]] || : >"$LIST"
cat >"$CONF" <<EOF
# MOST: DNS для клиентов VPN с блокировкой рекламы.
# Только интерфейсы VPN — для интернета этот DNS закрыт.
interface=awg0
interface=wgr0
except-interface=lo
bind-dynamic
no-resolv
no-hosts
server=1.1.1.1
server=8.8.8.8
cache-size=10000
domain-needed
bogus-priv
conf-file=$LIST
EOF
apt-get -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold -o Dpkg::Options::=--force-confdef \
  install -y -qq dnsmasq curl >/dev/null

cat >"$UPDATE" <<'EOF'
#!/bin/bash
# MOST: обновить список рекламных доменов (фильтр AdGuard DNS) для dnsmasq.
LIST=/var/lib/most/adblock.conf
URL=https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt
tmp=$(mktemp); raw=$(mktemp)
# «||домен^» — блокировать домен и поддомены; «@@||домен^» — исключение, такие не блокируем.
if curl -fsSL --max-time 60 "$URL" -o "$raw" \
   && sed -nE 's/^\|\|([a-z0-9._-]+)\^$/\1/p' "$raw" | sort -u \
      | grep -vxF -f <(sed -nE 's/^@@\|\|([a-z0-9._-]+)\^.*$/\1/p' "$raw"; echo .) \
      | sed 's|.*|address=/&/#|' >"$tmp" \
   && [[ $(wc -l <"$tmp") -gt 10000 ]]; then
  install -m 644 "$tmp" "$LIST"
  systemctl restart dnsmasq
  logger -t most-adblock "список обновлён: $(wc -l <"$LIST") доменов"
else
  logger -t most-adblock "не удалось скачать список, оставляю прежний"
fi
rm -f "$tmp" "$raw"
EOF
chmod 755 "$UPDATE"

cat >/etc/systemd/system/most-adblock-update.service <<EOF
[Unit]
Description=MOST: обновить список рекламных доменов
After=network-online.target
[Service]
Type=oneshot
ExecStart=$UPDATE
EOF
cat >/etc/systemd/system/most-adblock-update.timer <<'EOF'
[Unit]
Description=MOST: обновлять список рекламных доменов раз в сутки
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload

echo "==> Скачиваю список рекламных доменов"
"$UPDATE"
echo "    доменов в списке: $(wc -l <"$LIST")"
systemctl enable dnsmasq most-adblock-update.timer >/dev/null 2>&1
systemctl restart dnsmasq
systemctl start most-adblock-update.timer
systemctl is-active --quiet dnsmasq || { journalctl -u dnsmasq -n 20 --no-pager; die "dnsmasq не запустился"; }

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  for i in awg0 wgr0; do
    ufw allow in on "$i" to any port 53 comment "MOST DNS" >/dev/null
  done
fi

# Проверка: рекламный домен должен вернуть 0.0.0.0, обычный — настоящий адрес.
AD="$(dig +short @"$DNS_IP" doubleclick.net 2>/dev/null | head -1 || true)"
OK="$(dig +short @"$DNS_IP" example.com 2>/dev/null | head -1 || true)"
echo "    проверка: doubleclick.net -> ${AD:-?} (должно быть 0.0.0.0), example.com -> ${OK:-?}"

sed -i '/^ADBLOCK_DNS=/d' "$ENV"
echo "ADBLOCK_DNS=$DNS_IP" >>"$ENV"
systemctl restart most-bot

cat <<EOF

Готово: реклама блокируется для конфигов с DNS $DNS_IP.
Новые файлы конфигов бот выдаёт уже с ним; у кого старый файл — пусть скачают заново
(«📄 Мои конфиги» → «⬇️ Скачать»). Выключить у своего конфига — кнопка «🛡 Реклама».
Выключить для всех: sudo bash adblock.sh off
EOF
