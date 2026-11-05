#!/usr/bin/env bash
# MOST — https для ссылок-подписок VLESS (Happ не принимает http). Запускать НА ВХОДЕ (московском сервере).
#
# Своего домена не нужно: имя вида 80-87-192-62.sslip.io всегда указывает на IP этого сервера,
# а Let's Encrypt выдаёт на него настоящий сертификат. nginx принимает https://ИМЯ/sub/КОД и передаёт
# запрос боту на основной сервер (порт 2096). Свой домен — вторым параметром.
#
#   sudo bash sub-https.sh IP_ОСНОВНОГО_СЕРВЕРА [свой.домен]
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
NL="${1:-}"
[[ "$NL" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "использование: sudo bash sub-https.sh IP_ОСНОВНОГО_СЕРВЕРА [домен]"
SUB_PORT=2096

MY_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org)" || die "не узнал IP этого сервера"
HOST="${2:-${MY_IP//./-}.sslip.io}"

echo "==> nginx и certbot"
apt-get -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold install -y -qq nginx certbot python3-certbot-nginx >/dev/null

cat >/etc/nginx/sites-available/most-sub <<EOF
# MOST: ссылки-подписки VLESS -> бот на основном сервере.
server {
    listen 80;
    listen [::]:80;
    server_name $HOST;
    location /sub/ {
        proxy_pass http://$NL:$SUB_PORT;
        proxy_set_header User-Agent \$http_user_agent;
        proxy_connect_timeout 10s;
        proxy_read_timeout 20s;
        proxy_buffer_size 16k;
        proxy_buffers 8 16k;
    }
    location / { return 404; }
}
EOF
ln -sf /etc/nginx/sites-available/most-sub /etc/nginx/sites-enabled/most-sub
nginx -t >/dev/null || { nginx -t; die "nginx не принял конфиг"; }
systemctl enable --now nginx >/dev/null 2>&1
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp comment "MOST sub" >/dev/null
  ufw allow 443/tcp comment "MOST sub" >/dev/null
fi

echo "==> Сертификат HTTPS для $HOST"
certbot --nginx -d "$HOST" --non-interactive --agree-tos --register-unsafely-without-email --redirect >/dev/null \
  || die "сертификат не получен — проверьте, что TCP 80 открыт (в том числе в панели хостинга)"

echo "==> Проверка"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "https://$HOST/sub/test" || true)"
echo "    https://$HOST/sub/test -> $code (404 — правильно: такого кода подписки нет, бот ответил)"

cat <<EOF

Готово: подписки отдаются по https://$HOST/sub/…  Сертификат обновляется сам.

Теперь на ОСНОВНОМ сервере выполните:

  sudo sed -i '/^SUB_BASE=/d' /etc/most/bot.env && echo 'SUB_BASE=https://$HOST' | sudo tee -a /etc/most/bot.env && sudo systemctl restart most-bot

После этого бот выдаёт ссылки https://$HOST/sub/КОД (у тех, кто уже купил, — по «🔑 Получить ключи»).
EOF
