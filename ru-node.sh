#!/usr/bin/env bash
# MOST — настройка российского сервера. Запускать НА РОССИЙСКОМ сервере.
#
# Делает две вещи:
#  1. Туннель awg1 для нидерландского сервера: через него NL-сервер отправляет
#     трафик к российским адресам, и банки/Госуслуги видят российский IP.
#  2. (с --relay-port) «Вход через РФ»: UDP-порт этого сервера пробрасывается
#     на AmneziaWG нидерландского сервера. Клиент подключается к российскому IP.
#
#   sudo bash ru-node.sh --nl-ip 185.121.13.60 --relay-port 51234
#
# В конце печатает строку-ключ — её нужно вставить в ru-route.sh на NL-сервере.
set -euo pipefail

AWG_DIR=/etc/amnezia/amneziawg
CONF="$AWG_DIR/awg1.conf"
PORT=51821
NL_IP=""
RELAY_PORT=""
PUBLIC_IP=""

die() { echo "Ошибка: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nl-ip) NL_IP="$2"; shift 2 ;;
    --relay-port) RELAY_PORT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --ip) PUBLIC_IP="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) die "неизвестный параметр: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "запустите через sudo"
[[ -n "$NL_IP" ]] || die "укажите IP нидерландского сервера: --nl-ip 185.121.13.60"

if ! command -v awg >/dev/null || ! modinfo amneziawg >/dev/null 2>&1; then
  echo "==> Устанавливаю AmneziaWG"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq software-properties-common >/dev/null
  add-apt-repository -y ppa:amnezia/ppa >/dev/null
  apt-get update -qq
  apt-get install -y -qq "linux-headers-$(uname -r)" amneziawg amneziawg-tools >/dev/null \
    || die "не удалось установить AmneziaWG"
fi
modprobe amneziawg

if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me)"
fi
WAN="$(ip -4 route show default | awk '{print $5; exit}')"
[[ -n "$PUBLIC_IP" && -n "$WAN" ]] || die "не удалось определить внешний IP или сетевой интерфейс"

echo "==> Создаю туннель awg1 для нидерландского сервера"
RU_PRIV="$(awg genkey)"
NL_PRIV="$(awg genkey)"
NL_PUB="$(awg pubkey <<<"$NL_PRIV")"
read -r S1 S2 H1 H2 H3 H4 < <(python3 -c '
import random
r = random.SystemRandom()
s1 = r.randint(15, 150)
s2 = r.choice([x for x in range(15, 151) if x != s1 + 56])
print(s1, s2, *r.sample(range(5, 2**31 - 1), 4))')
OBFS="Jc = 4
Jmin = 40
Jmax = 70
S1 = $S1
S2 = $S2
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4"

mkdir -p "$AWG_DIR"
umask 077
cat >"$CONF" <<EOF
[Interface]
PrivateKey = $RU_PRIV
Address = 10.66.0.1/30
ListenPort = $PORT
$OBFS
PostUp = iptables -I FORWARD -i %i -j ACCEPT; iptables -I FORWARD -o %i -j ACCEPT; iptables -t nat -A POSTROUTING -s 10.66.0.2/32 -o $WAN -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT; iptables -t nat -D POSTROUTING -s 10.66.0.2/32 -o $WAN -j MASQUERADE

[Peer]
# нидерландский сервер
PublicKey = $NL_PUB
AllowedIPs = 10.66.0.2/32
EOF
umask 022

echo "net.ipv4.ip_forward=1" >/etc/sysctl.d/99-most-ru.conf
sysctl -q --system || true
systemctl enable awg-quick@awg1 >/dev/null 2>&1
systemctl restart awg-quick@awg1
systemctl is-active --quiet awg-quick@awg1 || { journalctl -u awg-quick@awg1 -n 20 --no-pager; die "awg1 не запустился"; }

UFW=0
command -v ufw >/dev/null && ufw status | grep -q "Status: active" && UFW=1
# Туннель awg1 нужен только нидерландскому серверу — открываем порт только для него.
[[ $UFW == 1 ]] && ufw allow from "$NL_IP" to any port "$PORT" proto udp comment "MOST awg1" >/dev/null

if [[ -n "$RELAY_PORT" ]]; then
  echo "==> Пробрасываю UDP $RELAY_PORT -> $NL_IP:$RELAY_PORT"
  cat >/usr/local/sbin/most-relay <<EOF
#!/bin/bash
# MOST: клиенты подключаются к этому серверу, трафик уходит на NL-сервер.
A="-p udp --dport $RELAY_PORT"
case "\$1" in
  up)
    iptables -t nat -A PREROUTING -i $WAN \$A -j DNAT --to-destination $NL_IP:$RELAY_PORT
    iptables -t nat -A POSTROUTING -o $WAN -d $NL_IP -p udp --dport $RELAY_PORT -j MASQUERADE
    iptables -I FORWARD -d $NL_IP -p udp --dport $RELAY_PORT -j ACCEPT
    iptables -I FORWARD -s $NL_IP -p udp --sport $RELAY_PORT -j ACCEPT ;;
  down)
    iptables -t nat -D PREROUTING -i $WAN \$A -j DNAT --to-destination $NL_IP:$RELAY_PORT
    iptables -t nat -D POSTROUTING -o $WAN -d $NL_IP -p udp --dport $RELAY_PORT -j MASQUERADE
    iptables -D FORWARD -d $NL_IP -p udp --dport $RELAY_PORT -j ACCEPT
    iptables -D FORWARD -s $NL_IP -p udp --sport $RELAY_PORT -j ACCEPT ;;
esac
EOF
  chmod 755 /usr/local/sbin/most-relay
  cat >/etc/systemd/system/most-relay.service <<EOF
[Unit]
Description=MOST: вход через РФ на нидерландский сервер
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/most-relay up
ExecStop=-/usr/local/sbin/most-relay down

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable most-relay >/dev/null 2>&1
  systemctl restart most-relay
  [[ $UFW == 1 ]] && ufw allow "$RELAY_PORT"/udp comment "MOST relay" >/dev/null
fi

TOKEN="$(python3 -c '
import base64, json, sys
keys = ["ru_ip", "port", "ru_pub", "nl_priv", "s1", "s2", "h1", "h2", "h3", "h4", "relay"]
print(base64.b64encode(json.dumps(dict(zip(keys, sys.argv[1:]))).encode()).decode())
' "$PUBLIC_IP" "$PORT" "$(awg pubkey <<<"$RU_PRIV")" "$NL_PRIV" "$S1" "$S2" "$H1" "$H2" "$H3" "$H4" "$RELAY_PORT")"

cat <<EOF

Готово. Если у хостинга есть свой файрвол, откройте UDP-порты: $PORT${RELAY_PORT:+ и $RELAY_PORT}.

Теперь на НИДЕРЛАНДСКОМ сервере, в папке MOST-, выполните:

sudo bash ru-route.sh '$TOKEN'
EOF
