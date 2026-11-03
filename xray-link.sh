#!/usr/bin/env bash
# MOST — канал «вход в России → основной сервер» через VLESS Reality.
#
# Трафик AmneziaWG, который вход пересылает за границу, ТСПУ распознаёт и режет.
# Этот скрипт заворачивает его в соединение, неотличимое от обычного HTTPS
# (TCP 443, настоящий сертификат чужого сайта). Клиенты по-прежнему подключаются
# к входу по AmneziaWG — их конфиги менять не нужно.
#
#   На ОСНОВНОМ сервере:  sudo bash xray-link.sh server [--sni www.microsoft.com]
#                         (в конце напечатает команду для входа)
#   На ВХОДЕ:             sudo bash xray-link.sh client 'СТРОКА-КЛЮЧ'
#   Отключить на входе:   sudo bash xray-link.sh off   (вернётся обычная пересылка most-relay)
set -euo pipefail

# Ubuntu после установки пакетов спрашивает (needrestart), какие службы перезапустить; в тихом режиме
# вопрос не виден и установка висит вечно — пусть перезапускает сама и ни о чём не спрашивает.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"

CONF=/usr/local/etc/xray/config.json

install_xray() {
  if ! command -v xray >/dev/null; then
    echo "==> Устанавливаю Xray"
    apt-get install -y -qq curl unzip >/dev/null
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null \
      || die "не удалось установить Xray (нет доступа к github.com?)"
  fi
  mkdir -p "$(dirname "$CONF")"
}

restart_xray() {
  xray run -test -config "$CONF" >/dev/null || { xray run -test -config "$CONF"; die "конфиг Xray не прошёл проверку"; }
  systemctl enable xray >/dev/null 2>&1
  systemctl restart xray
  sleep 1
  systemctl is-active --quiet xray || { journalctl -u xray -n 20 --no-pager; die "Xray не запустился"; }
}

case "${1:-}" in
server)
  SNI="www.microsoft.com"
  [[ "${2:-}" == "--sni" && -n "${3:-}" ]] && SNI="$3"
  [[ -f /etc/most/bot.env ]] || die "это не основной сервер MOST (нет /etc/most/bot.env)"
  AWG_PORT="$(awk -F' *= *' '$1=="ListenPort"{print $2; exit}' /etc/amnezia/amneziawg/awg0.conf)"
  R_PORT="$(awk -F' *= *' '$1=="ListenPort"{print $2; exit}' /etc/amnezia/amneziawg/wgr0.conf 2>/dev/null || true)"
  NL_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org)"
  install_xray

  KEYS="$(xray x25519)"
  # Старые версии пишут «Private key/Public key», новые — «PrivateKey/Password».
  PRIV="$(awk -F': *' 'NR==1{print $2}' <<<"$KEYS")"
  PUB="$(awk -F': *' 'NR==2{print $2}' <<<"$KEYS")"
  UUID="$(xray uuid)"
  SID="$(openssl rand -hex 8)"

  cat >"$CONF" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{
    "listen": "0.0.0.0", "port": 443, "protocol": "vless",
    "settings": {"clients": [{"id": "$UUID"}], "decryption": "none"},
    "streamSettings": {
      "network": "tcp", "security": "reality",
      "realitySettings": {"target": "$SNI:443", "serverNames": ["$SNI"],
                          "privateKey": "$PRIV", "shortIds": ["$SID"]}
    }
  }],
  "outbounds": [{"protocol": "freedom"}]
}
EOF
  chmod 644 "$CONF"
  restart_xray
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 443/tcp comment "MOST xray" >/dev/null
  fi

  KEY="$(python3 -c 'import base64,json,sys; print(base64.b64encode(json.dumps(dict(zip(
    ["ip","uuid","pub","sid","sni","ports"], sys.argv[1:]))).encode()).decode())' \
    "$NL_IP" "$UUID" "$PUB" "$SID" "$SNI" "$AWG_PORT${R_PORT:+,$R_PORT}")"
  cat <<EOF

Готово: основной сервер принимает канал VLESS Reality на TCP 443 (маскировка под $SNI).
Если у хостинга есть свой файрвол — откройте в нём TCP-порт 443.

Теперь на ВХОДЕ (российском сервере), в папке MOST-, выполните:

sudo bash xray-link.sh client '$KEY'

Строка секретная: никому не пересылайте.
EOF
  ;;

client)
  [[ -n "${2:-}" ]] || die "использование: sudo bash xray-link.sh client 'СТРОКА-КЛЮЧ'"
  eval "$(python3 -c '
import base64, json, shlex, sys
d = json.loads(base64.b64decode(sys.argv[1]))
for k, v in d.items():
    print(f"{k.upper()}={shlex.quote(str(v))}")
' "$2")" || die "строка-ключ повреждена — скопируйте её целиком"
  install_xray

  # Каждый UDP-порт AmneziaWG на входе уходит по каналу на тот же порт основного сервера.
  INBOUNDS=""
  for P in ${PORTS//,/ }; do
    INBOUNDS+="${INBOUNDS:+,}
    {\"listen\": \"0.0.0.0\", \"port\": $P, \"protocol\": \"dokodemo-door\",
     \"settings\": {\"address\": \"127.0.0.1\", \"port\": $P, \"network\": \"udp\"}}"
  done
  cat >"$CONF" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [$INBOUNDS
  ],
  "outbounds": [{
    "protocol": "vless",
    "settings": {"vnext": [{"address": "$IP", "port": 443,
                            "users": [{"id": "$UUID", "encryption": "none"}]}]},
    "streamSettings": {
      "network": "tcp", "security": "reality",
      "realitySettings": {"serverName": "$SNI", "fingerprint": "chrome",
                          "publicKey": "$PUB", "shortId": "$SID"}
    }
  }]
}
EOF
  chmod 644 "$CONF"

  # Обычная пересылка (most-relay) перехватила бы эти порты раньше Xray.
  systemctl disable --now most-relay 2>/dev/null || true
  restart_xray
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    for P in ${PORTS//,/ }; do ufw allow "$P"/udp comment "MOST relay" >/dev/null; done
  fi
  command -v conntrack >/dev/null && conntrack -F >/dev/null 2>&1 || true

  echo
  echo "Готово: клиенты -> этот сервер (UDP $PORTS) -> VLESS Reality -> $IP."
  echo "Клиентам ничего менять не нужно. Проверьте подключение с телефона."
  echo "Вернуть обычную пересылку: sudo bash xray-link.sh off"
  ;;

off)
  systemctl disable --now xray 2>/dev/null || true
  systemctl enable --now most-relay 2>/dev/null || true
  command -v conntrack >/dev/null && conntrack -F >/dev/null 2>&1 || true
  echo "Канал Xray выключен, снова работает обычная пересылка most-relay."
  ;;

*)
  die "использование: sudo bash xray-link.sh server | client 'СТРОКА-КЛЮЧ' | off"
  ;;
esac
