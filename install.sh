#!/usr/bin/env bash
# MOST — установка AmneziaWG и Telegram-бота, который выдаёт конфиги.
# Запускать на сервере, через который пойдёт трафик (Ubuntu 22.04 / 24.04).
#
#   sudo bash install.sh --token 123456:ABC... --admin 111111111
#
# Повторный запуск безопасен: ключи и выданные конфиги сохраняются,
# обновляются только код бота и его настройки.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IFACE=awg0
AWG_DIR=/etc/amnezia/amneziawg
AWG_CONF="$AWG_DIR/$IFACE.conf"
SUBNET=10.8.0
BOT_DIR=/opt/most
ENV_FILE=/etc/most/bot.env

TOKEN=""
ADMIN=""
PORT=""
PUBLIC_IP=""
MAX_PEERS=3

die() { echo "Ошибка: $*" >&2; exit 1; }

usage() {
  cat <<EOF
Использование: sudo bash install.sh --token ТОКЕН --admin ВАШ_ID [параметры]

  --token ТОКЕН    токен бота от @BotFather
  --admin ID       ваш Telegram ID (узнать: @userinfobot); несколько — через запятую
  --port N         UDP-порт AmneziaWG (по умолчанию случайный 40000–60000)
  --ip АДРЕС       внешний IP сервера (по умолчанию определяется сам)
  --max N          сколько конфигов может взять один человек (по умолчанию 3)
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token) TOKEN="$2"; shift 2 ;;
    --admin) ADMIN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --ip) PUBLIC_IP="$2"; shift 2 ;;
    --max) MAX_PEERS="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) die "неизвестный параметр: $1 (см. --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -n "$TOKEN" ]] || read -rp "Токен бота от @BotFather: " TOKEN
[[ -n "$ADMIN" ]] || read -rp "Ваш Telegram ID (узнать у @userinfobot): " ADMIN
[[ "$TOKEN" == *:* ]] || die "токен выглядит неправильно (должен быть вида 123456:ABC...)"
[[ "$ADMIN" =~ ^[0-9,\ ]+$ ]] || die "Telegram ID — это только цифры"

echo "==> Устанавливаю пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq software-properties-common python3-venv curl iptables >/dev/null

if ! command -v awg >/dev/null || ! modinfo amneziawg >/dev/null 2>&1; then
  echo "==> Устанавливаю AmneziaWG (модуль ядра собирается пару минут)"
  add-apt-repository -y ppa:amnezia/ppa >/dev/null
  apt-get update -qq
  apt-get install -y -qq "linux-headers-$(uname -r)" amneziawg amneziawg-tools >/dev/null \
    || die "не удалось установить AmneziaWG — пришлите вывод: apt-get install amneziawg"
fi
modprobe amneziawg || die "модуль amneziawg не загрузился — пришлите вывод: dmesg | tail -20"

if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me)"
fi
[[ -n "$PUBLIC_IP" ]] || die "не удалось определить внешний IP, укажите его через --ip"

if [[ -f "$AWG_CONF" ]]; then
  echo "==> $AWG_CONF уже есть — оставляю ключи и клиентов как есть"
  PORT="$(awk -F' *= *' '$1=="ListenPort"{print $2; exit}' "$AWG_CONF")"
else
  echo "==> Создаю конфиг сервера"
  [[ -n "$PORT" ]] || PORT=$((RANDOM % 20000 + 40000))
  WAN="$(ip -4 route show default | awk '{print $5; exit}')"
  [[ -n "$WAN" ]] || die "не нашёл сетевой интерфейс с выходом в интернет"
  PRIV="$(awg genkey)"
  # Параметры обфускации AmneziaWG: случайные для каждого сервера.
  read -r JC JMIN JMAX S1 S2 H1 H2 H3 H4 < <(python3 - <<'EOF'
import random
r = random.SystemRandom()
s1 = r.randint(15, 150)
s2 = r.choice([x for x in range(15, 151) if x != s1 + 56])
h = r.sample(range(5, 2**31 - 1), 4)
print(r.randint(4, 8), r.randint(40, 70), r.randint(700, 1200), s1, s2, *h)
EOF
)
  mkdir -p "$AWG_DIR"
  umask 077
  cat >"$AWG_CONF" <<EOF
[Interface]
PrivateKey = $PRIV
Address = $SUBNET.1/24
ListenPort = $PORT
Jc = $JC
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4
PostUp = iptables -I FORWARD -i %i -j ACCEPT; iptables -I FORWARD -o %i -j ACCEPT; iptables -t nat -A POSTROUTING -s $SUBNET.0/24 -o $WAN -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT; iptables -t nat -D POSTROUTING -s $SUBNET.0/24 -o $WAN -j MASQUERADE
EOF
  umask 022
fi

echo "==> Включаю маршрутизацию"
cat >/etc/sysctl.d/99-most.conf <<EOF
net.ipv4.ip_forward=1
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl -q --system || true

systemctl enable "awg-quick@$IFACE" >/dev/null 2>&1
systemctl restart "awg-quick@$IFACE"
systemctl is-active --quiet "awg-quick@$IFACE" \
  || { journalctl -u "awg-quick@$IFACE" -n 20 --no-pager; die "AmneziaWG не запустился"; }

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/udp comment "MOST AmneziaWG" >/dev/null
fi

echo "==> Устанавливаю бота"
mkdir -p "$BOT_DIR" /etc/most /var/lib/most
install -m 644 "$SCRIPT_DIR/bot/bot.py" "$SCRIPT_DIR/bot/requirements.txt" "$BOT_DIR/"
[[ -d "$BOT_DIR/venv" ]] || python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install -q --upgrade pip
"$BOT_DIR/venv/bin/pip" install -q -r "$BOT_DIR/requirements.txt"

umask 077
cat >"$ENV_FILE" <<EOF
BOT_TOKEN=$TOKEN
ADMIN_IDS=$ADMIN
ENDPOINT_HOST=$PUBLIC_IP
AWG_IFACE=$IFACE
AWG_CONF=$AWG_CONF
STATE_FILE=/var/lib/most/state.json
MAX_PEERS=$MAX_PEERS
EOF
umask 022

cat >/etc/systemd/system/most-bot.service <<EOF
[Unit]
Description=MOST: Telegram-бот AmneziaWG
After=network-online.target awg-quick@$IFACE.service
Wants=network-online.target

[Service]
EnvironmentFile=$ENV_FILE
ExecStart=$BOT_DIR/venv/bin/python $BOT_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable most-bot >/dev/null 2>&1
systemctl restart most-bot
sleep 3
systemctl is-active --quiet most-bot || { journalctl -u most-bot -n 30 --no-pager; die "бот не запустился"; }

cat <<EOF

Готово.
  AmneziaWG: $PUBLIC_IP:$PORT/udp
  Бот запущен. Откройте его в Telegram и нажмите /start.

Если у хостинга есть свой файрвол — откройте в нём UDP-порт $PORT.
Логи бота:  journalctl -u most-bot -f
Клиенты:    awg show
EOF
