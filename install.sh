#!/usr/bin/env bash
# MOST — установка моста на российский сервер.
# Ставит Xray, генерирует конфиг, который пересылает трафик
# на нидерландский сервер с 3X-UI, и печатает ссылку для клиента.
#
# Пример:
#   sudo bash install.sh --link 'vless://...'
#   sudo bash install.sh --link 'vless://...' --mode forward --port 8443
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
XRAY=/usr/local/bin/xray
CONFIG=/usr/local/etc/xray/config.json
CLIENT_FILE=/root/most-client.txt

LINK=""
MODE="chain"
PORT=443
SNI="ya.ru"
PUBLIC_IP=""
EXTRA=()

die() { echo "Ошибка: $*" >&2; exit 1; }

usage() {
  cat <<EOF
Использование: sudo bash install.sh [параметры]

  --link 'vless://...'   ссылка на NL-сервер из 3X-UI (если не указать — спросит)
  --mode chain|forward   chain   — свой Reality на RU-сервере (по умолчанию)
                         forward — простой проброс порта на NL-сервер
  --port N               порт на RU-сервере (по умолчанию 443)
  --sni домен            сайт для маскировки Reality (по умолчанию ya.ru)
  --ip адрес             внешний IP RU-сервера (по умолчанию определяется сам)
  --no-ru-direct         пускать и российские сайты через Нидерланды
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --link) LINK="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --sni) SNI="$2"; shift 2 ;;
    --ip) PUBLIC_IP="$2"; shift 2 ;;
    --no-ru-direct) EXTRA+=(--no-ru-direct); shift ;;
    -h|--help) usage ;;
    *) die "неизвестный параметр: $1 (см. --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ "$MODE" == "chain" || "$MODE" == "forward" ]] || die "--mode должен быть chain или forward"

if [[ -z "$LINK" ]]; then
  read -rp "Вставьте ссылку на нидерландский сервер из 3X-UI: " LINK
fi
[[ -n "$LINK" ]] || die "ссылка не указана"

echo "==> Устанавливаю зависимости"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl python3 iproute2 >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q curl python3 iproute
fi

if ss -ltnH "sport = :$PORT" | grep -q .; then
  echo "Порт $PORT уже занят:" >&2
  ss -ltnp "sport = :$PORT" >&2
  die "освободите порт или выберите другой через --port"
fi

echo "==> Устанавливаю Xray"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null
[[ -x "$XRAY" ]] || die "Xray не установился"

if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me)"
fi
[[ -n "$PUBLIC_IP" ]] || die "не удалось определить внешний IP, укажите его через --ip"

KEY_ARGS=()
if [[ "$MODE" == "chain" ]]; then
  echo "==> Проверяю сайт для маскировки $SNI (нужны TLS 1.3 и H2)"
  "$XRAY" tls ping "$SNI" 2>&1 | grep -Ei "tls|h2|alpn|version" | head -n 6 || true

  KEYS="$("$XRAY" x25519)"
  # Старые версии Xray пишут "Private key/Public key", новые — "PrivateKey/Password".
  PRIV="$(awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}' <<<"$KEYS")"
  PUB="$(awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}' <<<"$KEYS")"
  [[ -n "$PRIV" && -n "$PUB" ]] || die "не удалось сгенерировать ключи x25519"
  KEY_ARGS=(--private-key "$PRIV" --public-key "$PUB" --sni "$SNI")
fi

echo "==> Генерирую конфиг"
[[ -f "$CONFIG" ]] && cp "$CONFIG" "$CONFIG.bak.$(date +%s)"
TMP_CONFIG="$(mktemp --suffix=.json)"
CLIENT_LINK="$(python3 "$SCRIPT_DIR/gen_config.py" \
  --link "$LINK" --mode "$MODE" --port "$PORT" --public-ip "$PUBLIC_IP" \
  --out "$TMP_CONFIG" "${KEY_ARGS[@]}" "${EXTRA[@]}")"

if ! TEST_OUT="$("$XRAY" run -test -c "$TMP_CONFIG" 2>&1)"; then
  echo "$TEST_OUT" | tail -n 15 >&2
  die "Xray отверг конфиг ($TMP_CONFIG), причина выше"
fi
install -m 644 "$TMP_CONFIG" "$CONFIG"
rm -f "$TMP_CONFIG"

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null
  [[ "$MODE" == "forward" ]] && ufw allow "$PORT"/udp >/dev/null
fi

echo "==> Включаю оптимизацию сети (BBR)"
cat >/etc/sysctl.d/99-most.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl -q --system || true

systemctl enable xray >/dev/null 2>&1
systemctl restart xray
sleep 1
systemctl is-active --quiet xray || { journalctl -u xray -n 30 --no-pager; die "Xray не запустился"; }

echo "$CLIENT_LINK" >"$CLIENT_FILE"
chmod 600 "$CLIENT_FILE"

cat <<EOF

Готово. Мост работает: клиент -> $PUBLIC_IP:$PORT (Россия) -> Нидерланды.

Ссылка для клиента (сохранена в $CLIENT_FILE):

$CLIENT_LINK

Добавьте её в v2rayN / v2RayTun / Hiddify / Streisand / NekoBox.
Логи: journalctl -u xray -f
EOF
