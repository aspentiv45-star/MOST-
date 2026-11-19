#!/usr/bin/env bash
# MOST — Telegram Mini App aspentiv: HTTPS-адрес для приложения внутри бота. Запускать на ОСНОВНОМ сервере (где бот).
#
#   sudo bash miniapp-setup.sh app.aspentiv.ru     — домен (A-запись должна указывать на IP этого сервера)
#   sudo bash miniapp-setup.sh off                 — убрать приложение (кнопка «Меню» снова со списком команд)
#   sudo bash miniapp-setup.sh url https://ru.example.duckdns.org — открывать приложение по другому адресу
#        (вход через российский сервер, miniapp-ru-proxy.sh: работает без VPN и в любом режиме)
#
# Ставит nginx и бесплатный сертификат Let's Encrypt, nginx пересылает запросы боту (127.0.0.1:8090),
# бот получает MINIAPP_URL и делает кнопку «Меню» кнопкой открытия приложения.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ $# -ge 1 ]] || die "использование: sudo bash miniapp-setup.sh ДОМЕН   (или off, или url https://АДРЕС)"
[[ $1 == url && $# -eq 2 ]] && set -- "$2"
ENV=/etc/most/bot.env
[[ -f $ENV ]] || die "это не основной сервер (нет $ENV)"
cd "$(dirname "$0")"
SITE=/etc/nginx/sites-available/most-miniapp
PORT=$(awk -F= '$1=="MINIAPP_PORT"{print $2}' "$ENV"); PORT=${PORT:-8090}

if [[ $1 == off ]]; then
  rm -f "$SITE" /etc/nginx/sites-enabled/most-miniapp
  systemctl reload nginx 2>/dev/null || true
  sed -i '/^MINIAPP_URL=/d' "$ENV"
  systemctl restart most-bot
  echo "Приложение выключено."
  exit 0
fi

if [[ $1 == url ]]; then
  # Адрес, по которому клиенты открывают приложение (например, вход через российский сервер: miniapp-ru-proxy.sh).
  die "использование: sudo bash miniapp-setup.sh url https://ДОМЕН"
fi
if [[ $1 == https://* ]]; then
  URL=${1%/}
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$URL/" || true)
  [[ $CODE == 200 ]] || die "$URL/ не открывается (код «$CODE»)"
  sed -i '/^MINIAPP_URL=/d' "$ENV"
  echo "MINIAPP_URL=$URL" >>"$ENV"
  systemctl restart most-bot
  echo "Готово: приложение открывается по адресу $URL. Закройте и снова откройте чат с ботом."
  exit 0
fi

DOMAIN=$(echo "$1" | tr 'A-Z' 'a-z')
[[ $DOMAIN =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "«$1» не похоже на домен"

echo "==> Проверяю, что $DOMAIN указывает на этот сервер"
MY_IP=$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)
DNS_IP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
echo "    IP сервера: ${MY_IP:-?}, домен указывает на: ${DNS_IP:-не найден}"
[[ -n $DNS_IP ]] || die "домен $DOMAIN пока не указывает ни на какой IP — добавьте A-запись на ${MY_IP:-IP сервера} и подождите 5–15 минут"
[[ -z $MY_IP || $DNS_IP == "$MY_IP" ]] || die "домен указывает на $DNS_IP, а не на этот сервер ($MY_IP) — поправьте A-запись"

echo "==> Обновляю бота (Mini App и пакет aiohttp)"
install -m 644 bot/*.py bot/miniapp.html bot/requirements.txt /opt/most/
/opt/most/venv/bin/pip install -q -r /opt/most/requirements.txt

echo "==> Ставлю nginx и certbot"
apt-get -o DPkg::Lock::Timeout=600 install -y -qq nginx certbot python3-certbot-nginx >/dev/null
cat >"$SITE" <<NGX
# MOST: Telegram Mini App aspentiv (miniapp-setup.sh)
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 64k;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
NGX
ln -sf "$SITE" /etc/nginx/sites-enabled/most-miniapp
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

sed -i '/^MINIAPP_URL=/d' "$ENV"
echo "MINIAPP_URL=https://$DOMAIN" >>"$ENV"
systemctl restart most-bot
sleep 4

echo "==> Проверка"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$DOMAIN/" || true)
if [[ $CODE == 200 ]]; then
  echo "    https://$DOMAIN/ отвечает — приложение работает"
else
  echo "    ВНИМАНИЕ: https://$DOMAIN/ ответил «$CODE». Логи бота: journalctl -u most-bot -n 40"
fi
cat <<TXT

Готово: в боте кнопка «Меню» (слева от поля ввода) теперь открывает приложение aspentiv.
Если у клиента осталась старая кнопка — пусть закроет и снова откроет чат с ботом.
Обновлять приложение: git pull && sudo cp bot/*.py bot/miniapp.html /opt/most/ && sudo systemctl restart most-bot
Выключить: sudo bash miniapp-setup.sh off
TXT
