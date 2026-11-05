#!/usr/bin/env bash
# MOST — VLESS Reality для клиентов (Happ, v2rayNG, Hiddify, Streisand). Запускать на ОСНОВНОМ сервере.
#
# Ставит Xray на TCP 443 и 2053 (игровой ключ; маскировка под обычный HTTPS чужого сайта). Бот продаёт
# «🔗 Подписку VLESS (Happ)» отдельно от AmneziaWG — со своим сроком; после оплаты присылает текст для Happ:
# маршрутизация (российское — без VPN) и три ключа: основной, обход белых списков (через вход), игровой.
# Российские сайты, как и у AmneziaWG, идут через российский выход (если стоит ru-route.sh).
#
#   sudo bash vless.sh [--sni www.microsoft.com] [--relay-port 8443]   — включить
#   sudo bash vless.sh off                                              — выключить
set -euo pipefail

# Ubuntu после установки пакетов спрашивает (needrestart), какие службы перезапустить; в тихом режиме
# вопрос не виден и установка висит вечно — пусть перезапускает сама и ни о чём не спрашивает.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

die() { echo "Ошибка: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "запустите через sudo"
ENV=/etc/most/bot.env
[[ -f $ENV ]] || die "это не основной сервер MOST (нет $ENV)"

ROUTE=/usr/local/sbin/most-vless-route
ROUTE_UNIT=/etc/systemd/system/most-vless-route.service
DROPIN=/etc/systemd/system/xray.service.d/most.conf

if [[ "${1:-}" == "off" ]]; then
  systemctl disable --now xray most-vless-route 2>/dev/null || true
  rm -f "$ROUTE" "$ROUTE_UNIT" "$DROPIN"
  systemctl daemon-reload
  sed -i '/^VLESS_/d' "$ENV"
  systemctl restart most-bot
  echo "VLESS выключен. Ключи клиентов больше не работают; AmneziaWG работает как раньше."
  exit 0
fi

SNI="www.microsoft.com"
RELAY_PORT=8443
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sni) SNI="$2"; shift 2 ;;
    --relay-port) RELAY_PORT="$2"; shift 2 ;;
    *) die "неизвестный параметр $1" ;;
  esac
done
old() { awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,""); print; exit}' "$ENV"; }

echo "==> Устанавливаю Xray"
apt-get -o DPkg::Lock::Timeout=600 install -y -qq curl unzip openssl >/dev/null
if ! command -v xray >/dev/null; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null \
    || die "не удалось установить Xray (нет доступа к github.com?)"
fi

# Ключи сервера — один раз; при повторном запуске прежние (иначе у клиентов перестанут работать ссылки).
PRIV="$(old VLESS_PRIV)"; PUB="$(old VLESS_PUB)"; SID="$(old VLESS_SID)"
if [[ -z "$PRIV" || -z "$PUB" ]]; then
  KEYS="$(xray x25519)"
  # Старые версии пишут «Private key/Public key», новые — «PrivateKey/Password».
  PRIV="$(awk -F': *' 'NR==1{print $2}' <<<"$KEYS")"
  PUB="$(awk -F': *' 'NR==2{print $2}' <<<"$KEYS")"
  SID="$(openssl rand -hex 8)"
fi
[[ -n "$PRIV" && -n "$PUB" ]] || die "не удалось создать ключи Xray"

# Отдельный пользователь: по нему трафик Xray к российским адресам уходит через российский выход.
id most-xray >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin most-xray
mkdir -p "$(dirname "$DROPIN")"
cat >"$DROPIN" <<'EOF'
[Service]
User=most-xray
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
EOF

cat >"$ROUTE" <<'EOF'
#!/bin/bash
# MOST: Xray (пользователь most-xray) -> российские IP (ipset most-ru из ru-route.sh) -> туннель awg1.
MARK=0x65
case "$1" in
  up)   OP=-A ;;
  down) OP=-D ;;
  *) exit 1 ;;
esac
ipset list most-ru >/dev/null 2>&1 && ip link show awg1 >/dev/null 2>&1 || exit 0
iptables -t mangle $OP OUTPUT -m owner --uid-owner most-xray -m set --match-set most-ru dst -j MARK --set-mark $MARK
iptables -t mangle $OP POSTROUTING -o awg1 -p tcp --tcp-flags SYN,RST SYN -m owner --uid-owner most-xray \
  -j TCPMSS --set-mss 1240
exit 0
EOF
chmod 755 "$ROUTE"
cat >"$ROUTE_UNIT" <<EOF
[Unit]
Description=MOST: VLESS -> российские сайты через российский выход
After=network-online.target most-ru-route.service awg-quick@awg1.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$ROUTE up
ExecStop=-$ROUTE down
[Install]
WantedBy=multi-user.target
EOF

sed -i '/^VLESS_/d' "$ENV"
cat >>"$ENV" <<EOF
VLESS_GAME_PORT=2053
SUB_PORT=2096
VLESS_PRIV=$PRIV
VLESS_PUB=$PUB
VLESS_SID=$SID
VLESS_SNI=$SNI
VLESS_PORT=443
EOF
RELAY_HOST="$(old RELAY_HOST)"
[[ -n "$RELAY_HOST" ]] && echo "VLESS_RELAY_PORT=$RELAY_PORT" >>"$ENV"

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 443/tcp comment "MOST vless" >/dev/null
  ufw allow 2053/tcp comment "MOST vless game" >/dev/null
  ufw allow 2096/tcp comment "MOST vless subscription" >/dev/null
fi

systemctl daemon-reload
systemctl enable most-vless-route xray >/dev/null 2>&1
systemctl restart most-vless-route
# Конфиг Xray с ключами клиентов пишет бот при запуске.
systemctl restart most-bot
sleep 5
systemctl restart xray
sleep 2
systemctl is-active --quiet xray || { journalctl -u xray -n 20 --no-pager; die "Xray не запустился"; }

MY_IP="$(old ENDPOINT_HOST)"
cat <<EOF

Готово: VLESS Reality на TCP 443 и 2053 (игровой; маскировка под $SNI).
В боте появилась покупка «🔗 Подписка VLESS (Happ)» — после оплаты клиент получает текст для Happ
ссылку-подписку (порт 2096): Happ сам добавит три режима и маршрутизацию.
Если у хостинга есть свой файрвол — откройте TCP 443, 2053 и 2096.
EOF
if [[ -n "$RELAY_HOST" ]]; then
  cat <<EOF

Для ссылки «через Москву» на ВХОДЕ ($RELAY_HOST), в папке MOST-, добавьте TCP-порт к пересылке
(к уже настроенным портам допишите ,t$RELAY_PORT=443,t2096=2096 — ключ через Москву и подписка):

  sudo bash relay.sh --to $MY_IP:\$(sed -n 's/^# MOST: .*порты: //p' /usr/local/sbin/most-relay | sed 's/,t[0-9=]*//g'),t$RELAY_PORT=443,t2096=2096
EOF
fi
