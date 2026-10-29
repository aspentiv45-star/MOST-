#!/usr/bin/env bash
# MOST — проверка моста прямо на RU-сервере.
# Поднимает временный клиент Xray по ссылке из /root/most-client.txt,
# подключается к мосту и смотрит, с какого IP выходит трафик.
set -euo pipefail

XRAY=/usr/local/bin/xray
CLIENT_FILE=/root/most-client.txt
SOCKS_PORT=10808

die() { echo "Ошибка: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -f "$CLIENT_FILE" ]] || die "нет $CLIENT_FILE — сначала запустите install.sh"
systemctl is-active --quiet xray || die "xray не запущен: journalctl -u xray -n 30"

TMP_DIR="$(mktemp -d)"
trap 'kill "$CLIENT_PID" 2>/dev/null || true; rm -rf "$TMP_DIR"' EXIT

python3 - "$(cat "$CLIENT_FILE")" "$SOCKS_PORT" >"$TMP_DIR/client.json" <<'EOF'
import json, sys
from urllib.parse import urlsplit, parse_qs
u = urlsplit(sys.argv[1]); q = {k: v[0] for k, v in parse_qs(u.query).items()}
print(json.dumps({
    "log": {"loglevel": "warning"},
    "inbounds": [{"listen": "127.0.0.1", "port": int(sys.argv[2]), "protocol": "socks",
                  "settings": {"udp": False}}],
    "outbounds": [{"protocol": "vless",
        "settings": {"vnext": [{"address": "127.0.0.1", "port": u.port,
            "users": [{"id": u.username, "encryption": "none", "flow": q.get("flow", "")}]}]},
        "streamSettings": {"network": "tcp", "security": "reality",
            "realitySettings": {"serverName": q["sni"], "fingerprint": q.get("fp", "chrome"),
                                "publicKey": q["pbk"], "shortId": q.get("sid", "")}}}],
}))
EOF

"$XRAY" run -c "$TMP_DIR/client.json" >"$TMP_DIR/client.log" 2>&1 &
CLIENT_PID=$!
sleep 2

SERVER_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || echo "?")"
VIA_BRIDGE="$(curl -4 -fsS --max-time 20 --socks5-hostname 127.0.0.1:$SOCKS_PORT https://api.ipify.org || true)"

echo "IP российского сервера:     $SERVER_IP"
echo "IP через мост (должен быть NL): ${VIA_BRIDGE:-нет ответа}"

if [[ -z "$VIA_BRIDGE" ]]; then
  echo
  tail -n 15 "$TMP_DIR/client.log"
  journalctl -u xray -n 15 --no-pager
  die "через мост интернет не открывается — логи выше"
elif [[ "$VIA_BRIDGE" == "$SERVER_IP" ]]; then
  die "трафик выходит с российского IP, до Нидерландов не доходит"
fi
echo
echo "Работает: клиент -> Россия -> $VIA_BRIDGE"
