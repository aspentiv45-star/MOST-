#!/usr/bin/env bash
# MOST — вход в Mini App через российский сервер. Запускать НА РОССИЙСКОМ (московском) сервере.
#
# Зачем: приложение работает на основном (нидерландском) сервере. С мобильного интернета с «белыми
# списками» его IP закрыт, а в режиме «⚡️ Основной» телефон шлёт запросы на IP своего VPN-сервера мимо
# туннеля — и их тоже режут. Российский IP открывается и без VPN, и в любом режиме. Здесь только nginx:
# он пересылает запросы на основной сервер, бот и данные остаются там.
#
#   sudo bash miniapp-ru-proxy.sh ru.example.duckdns.org aspentivvpn.duckdns.org
#        ^ домен, указывающий на ЭТОТ сервер     ^ домен приложения на основном сервере (miniapp-setup.sh)
#   sudo bash miniapp-ru-proxy.sh off
# Потом на основном сервере: sudo bash miniapp-setup.sh url https://ru.example.duckdns.org
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
SITE=/etc/nginx/sites-available/most-miniapp-ru

if [[ ${1:-} == off ]]; then
  rm -f "$SITE" /etc/nginx/sites-enabled/most-miniapp-ru
  systemctl reload nginx 2>/dev/null || true
  echo "Пересылка приложения выключена. На основном сервере верните адрес: sudo bash miniapp-setup.sh url https://ДОМЕН-ОСНОВНОГО"
  exit 0
fi
[[ $# -eq 2 ]] || die "использование: sudo bash miniapp-ru-proxy.sh ДОМЕН-ЭТОГО-СЕРВЕРА ДОМЕН-ОСНОВНОГО"
DOMAIN=$(echo "$1" | tr 'A-Z' 'a-z'); BACKEND=$(echo "$2" | tr 'A-Z' 'a-z')
for d in "$DOMAIN" "$BACKEND"; do [[ $d =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "«$d» не похоже на домен"; done
[[ $DOMAIN != "$BACKEND" ]] || die "нужны два разных домена: один на этот сервер, другой на основной"

echo "==> Проверяю домены"
MY_IP=$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)
DNS_IP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
echo "    IP этого сервера: ${MY_IP:-?}, $DOMAIN указывает на: ${DNS_IP:-не найден}"
[[ -n $DNS_IP ]] || die "$DOMAIN пока ни на что не указывает — поставьте в DuckDNS IP ${MY_IP:-этого сервера} и подождите пару минут"
[[ -z $MY_IP || $DNS_IP == "$MY_IP" ]] || die "$DOMAIN указывает на $DNS_IP, а должен на этот сервер ($MY_IP)"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$BACKEND/" || true)
[[ $CODE == 200 ]] || die "приложение на https://$BACKEND/ не отвечает (код «$CODE») — сначала miniapp-setup.sh на основном сервере"
echo "    приложение на основном сервере отвечает"

echo "==> Ставлю nginx и certbot"
apt-get -o DPkg::Lock::Timeout=600 install -y -qq nginx certbot python3-certbot-nginx >/dev/null
cat >"$SITE" <<NGX
# MOST: вход в Mini App через российский сервер (miniapp-ru-proxy.sh) -> https://$BACKEND
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 64k;
    location / {
        proxy_pass https://$BACKEND;
        proxy_ssl_server_name on;
        proxy_ssl_name $BACKEND;
        proxy_set_header Host $BACKEND;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
NGX
ln -sf "$SITE" /etc/nginx/sites-enabled/most-miniapp-ru
nginx -t >/dev/null 2>&1 || { nginx -t; die "ошибка в настройках nginx"; }
systemctl enable -q nginx
systemctl reload nginx || systemctl restart nginx
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp comment "MOST miniapp" >/dev/null
  ufw allow 443/tcp comment "MOST miniapp" >/dev/null
fi

echo "==> Сертификат HTTPS (Let's Encrypt)"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect -q \
  || die "не удалось получить сертификат: проверьте, что TCP-порты 80 и 443 открыты в файрволе хостинга"

CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$DOMAIN/" || true)
if [[ $CODE == 200 ]]; then echo "    https://$DOMAIN/ открывает приложение"; else echo "    ВНИМАНИЕ: https://$DOMAIN/ ответил «$CODE»"; fi
cat <<TXT

Готово. Теперь на ОСНОВНОМ сервере выполните:
  cd ~/MOST- && git pull && sudo bash miniapp-setup.sh url https://$DOMAIN
и закройте-откройте чат с ботом.
TXT
