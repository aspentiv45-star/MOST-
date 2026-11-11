#!/usr/bin/env bash
# MOST — сайт для платёжного сервиса (оферта, цены, возврат, политика ПДн, контакты).
# Запускать на сервере, где будет сайт (лучше московском: российские пользователи и модераторы
# платёжного сервиса открывают его без проблем).
#
#   sudo bash site-setup.sh        — первый раз создаст /etc/most/site.env: впишите туда данные и запустите снова
#   sudo bash site-setup.sh        — собрать сайт, поставить nginx и сертификат HTTPS
#
# Личные данные (ФИО, ИНН, адрес, телефон) хранятся только на сервере, в /etc/most/site.env,
# а не в репозитории. Перед запуском домен должен указывать на IP этого сервера (A-запись).
set -euo pipefail

# Ubuntu после установки пакетов спрашивает (needrestart), какие службы перезапустить; в тихом режиме
# вопрос не виден и установка висит вечно — пусть перезапускает сама и ни о чём не спрашивает.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
cd "$(dirname "$0")"

ENV=/etc/most/site.env
WWW=/var/www/most

if [[ ! -f "$ENV" ]]; then
  mkdir -p /etc/most
  cat >"$ENV" <<'EOF'
# Данные для сайта. Заполните все строки и снова запустите: sudo bash site-setup.sh
DOMAIN=most-vpn.ru
BRAND=aspentiv VPN
# ФИО как в паспорте (самозанятый) или «ИП Иванов Иван Иванович»
NAME=Фамилия Имя Отчество
INN=000000000000
# Только для ИП; самозанятым оставьте пустым — на сайте будет «плательщик НПД (самозанятый)»
OGRNIP=
# Адрес для корреспонденции (не «до востребования»)
ADDRESS=000000, г. Город, ул. Улица, д. 1, кв. 1
PHONE=+7 900 000-00-00
EMAIL=support@most-vpn.ru
BOT=aspentivVPNbot
PRICE=100
PRICE3=249
PRICE12=960
ROUTER_PRICE=500
TOPUP_MIN=150
STARS_1M=99
STARS_3M=229
STARS_12M=899
DAYS=30
TRIAL_DAYS=3
# Дата редакции оферты; пусто — сегодняшняя
OFFER_DATE=
EOF
  chmod 600 "$ENV"
  echo "Создан $ENV — впишите туда свои данные (nano $ENV) и запустите скрипт снова."
  exit 0
fi

grep -qE '000000000000|Фамилия Имя Отчество' "$ENV" && die "в $ENV остались примеры — впишите свои данные"

echo "==> Собираю сайт"
mkdir -p "$WWW"
python3 - "$ENV" site "$WWW" <<'PYEOF'
import datetime, html, pathlib, re, sys
env_path, src, dst = sys.argv[1:]
values = {}
for line in open(env_path, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
ogrnip = values.get("OGRNIP", "").strip()
values["LEGAL"] = (f"ОГРНИП {ogrnip}" if ogrnip
                   else "плательщик налога на профессиональный доход (самозанятый)")
# Для старых site.env без новых строк — значения как в боте по умолчанию
for k, v in {"PRICE3": "249", "PRICE12": "960", "TOPUP_MIN": "150",
             "STARS_1M": "99", "STARS_3M": "229", "STARS_12M": "899"}.items():
    values.setdefault(k, v)
if not values.get("OFFER_DATE"):
    values["OFFER_DATE"] = datetime.date.today().strftime("%d.%m.%Y")
src = pathlib.Path(src)
header = (src / "_header.html").read_text(encoding="utf-8")
footer = (src / "_footer.html").read_text(encoding="utf-8")
esc = {k: html.escape(v) for k, v in values.items()}
for page in src.glob("*.html"):
    if page.name.startswith("_"):
        continue
    text = page.read_text(encoding="utf-8")
    text = re.sub(r"\{\{HEADER\|([^}]*)\}\}", lambda m: header.replace("{{TITLE}}", m.group(1)), text)
    text = text.replace("{{FOOTER}}", footer)
    text = re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda m: esc.get(m.group(1), m.group(0)), text)
    left = re.findall(r"\{\{[A-Z0-9_|]+\}\}", text)
    if left:
        sys.exit(f"в {page.name} не заполнено: {', '.join(sorted(set(left)))}")
    (pathlib.Path(dst) / page.name).write_text(text, encoding="utf-8")
(pathlib.Path(dst) / "style.css").write_text((src / "style.css").read_text(encoding="utf-8"), encoding="utf-8")
print("    страниц:", len(list(pathlib.Path(dst).glob("*.html"))))
PYEOF

DOMAIN="$(awk -F= '$1=="DOMAIN"{print $2}' "$ENV" | tr -d ' ')"
EMAIL="$(awk -F= '$1=="EMAIL"{print $2}' "$ENV" | tr -d ' ')"
[[ -n "$DOMAIN" ]] || die "в $ENV не указан DOMAIN"

echo "==> Устанавливаю nginx и certbot"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq nginx certbot python3-certbot-nginx >/dev/null

cat >/etc/nginx/sites-available/most <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN www.$DOMAIN;
    root $WWW;
    index index.html;
    location / { try_files \$uri \$uri/ =404; }
}
EOF
ln -sf /etc/nginx/sites-available/most /etc/nginx/sites-enabled/most
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null
systemctl enable --now nginx >/dev/null 2>&1
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp comment "MOST site" >/dev/null
  ufw allow 443/tcp comment "MOST site" >/dev/null
fi

echo "==> Получаю сертификат HTTPS для $DOMAIN"
MY_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
if [[ "$DNS_IP" != "$MY_IP" ]]; then
  echo "    ВНИМАНИЕ: $DOMAIN указывает на ${DNS_IP:-ничего}, а у сервера IP ${MY_IP:-?}."
  echo "    Пропишите у регистратора A-запись $DOMAIN -> $MY_IP, подождите 10–30 минут и запустите скрипт снова."
  exit 1
fi
DOMAINS=(-d "$DOMAIN")
[[ "$(getent ahostsv4 "www.$DOMAIN" | awk 'NR==1{print $1}')" == "$MY_IP" ]] && DOMAINS+=(-d "www.$DOMAIN")
certbot --nginx "${DOMAINS[@]}" --non-interactive --agree-tos -m "$EMAIL" --redirect >/dev/null \
  || die "сертификат не получен — проверьте, что порт 80 открыт в файрволе хостинга"

cat <<EOF

Готово: https://$DOMAIN
Страницы: оферта https://$DOMAIN/offer.html, возврат /refund.html, политика ПДн /privacy.html, контакты /contacts.html.
Сертификат обновляется сам. Изменили данные или тексты — снова: sudo bash site-setup.sh
EOF
