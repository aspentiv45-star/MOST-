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
trap 'kill "${CLIENT_PID:-}" 2>/dev/null || true; rm -rf "$TMP_DIR"' EXIT

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

# Второй клиент: идёт на NL-сервер напрямую, минуя мост, — так видно,
# какой участок сломан.
python3 - /usr/local/etc/xray/config.json $((SOCKS_PORT + 1)) >"$TMP_DIR/direct.json" <<'EOF2'
import json, sys
cfg = json.load(open(sys.argv[1]))
out = next(o for o in cfg["outbounds"] if o.get("tag") == "to-nl")
print(json.dumps({
    "log": {"loglevel": "debug"},
    "inbounds": [{"listen": "127.0.0.1", "port": int(sys.argv[2]), "protocol": "socks"}],
    "outbounds": [out],
}))
EOF2

"$XRAY" run -c "$TMP_DIR/client.json" >"$TMP_DIR/client.log" 2>&1 &
CLIENT_PID=$!
"$XRAY" run -c "$TMP_DIR/direct.json" >"$TMP_DIR/direct.log" 2>&1 &
DIRECT_PID=$!
trap 'kill "${CLIENT_PID:-}" "${DIRECT_PID:-}" 2>/dev/null || true; rm -rf "$TMP_DIR"' EXIT
sleep 2

NL_HOST="$(python3 -c 'import json;c=json.load(open("/usr/local/etc/xray/config.json"));o=next(o for o in c["outbounds"] if o.get("tag")=="to-nl");v=o["settings"]["vnext"][0] if "vnext" in o["settings"] else o["settings"]["servers"][0];print(v["address"],v["port"])')"
read -r NL_ADDR NL_PORT <<<"$NL_HOST"

SERVER_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || echo "?")"
if timeout 5 bash -c "echo > /dev/tcp/$NL_ADDR/$NL_PORT" 2>/dev/null; then TCP_NL="есть"; else TCP_NL="НЕТ"; fi
VIA_NL="$(curl -4 -fsS --max-time 20 --socks5-hostname 127.0.0.1:$((SOCKS_PORT + 1)) https://api.ipify.org || true)"
VIA_BRIDGE="$(curl -4 -fsS --max-time 20 --socks5-hostname 127.0.0.1:$SOCKS_PORT https://api.ipify.org || true)"

echo "IP российского сервера:                 $SERVER_IP"
echo "1) TCP до NL $NL_ADDR:$NL_PORT:        $TCP_NL"
echo "2) Xray RU -> NL напрямую, выход с IP:  ${VIA_NL:-нет ответа}"
echo "3) Через весь мост, выход с IP:         ${VIA_BRIDGE:-нет ответа}"
echo "Слушает ли мост порт:"
ss -ltnp | grep xray || echo "   xray ничего не слушает!"

if [[ "$TCP_NL" == "НЕТ" ]]; then
  die "RU-сервер не может соединиться с NL-сервером: порт закрыт или заблокирован"
elif [[ -z "$VIA_NL" ]]; then
  echo; grep -Ei "fail|error|reject|warn" "$TMP_DIR/direct.log" | tail -n 20
  die "до NL достучались, но NL не принимает подключение — ошибки выше"
elif [[ -z "$VIA_BRIDGE" ]]; then
  echo; tail -n 15 "$TMP_DIR/client.log"; journalctl -u xray -n 20 --no-pager
  die "NL работает, но сам мост на RU-сервере не пропускает — логи выше"
elif [[ "$VIA_BRIDGE" == "$SERVER_IP" ]]; then
  die "трафик выходит с российского IP, до Нидерландов не доходит"
fi
echo
echo "На сервере всё работает: клиент -> Россия -> $VIA_BRIDGE"
echo "Если на телефоне не работает — порт моста закрыт снаружи (файрвол хостинга)."
