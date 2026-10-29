#!/usr/bin/env bash
# MOST — правила TPROXY: весь трафик клиентов AmneziaWG (awg0) уходит в Xray,
# а оттуда — на NL-сервер. Ставится в /usr/local/sbin/most-awg-rules
# и вызывается из most-awg.service (up при старте, down при остановке).
set -uo pipefail

IFACE="${MOST_AWG_IFACE:-awg0}"
PORT="${MOST_AWG_PORT:-12345}"
MARK=0x4d
TABLE=233
CHAIN=MOST_AWG

down() {
  iptables -t mangle -D PREROUTING -i "$IFACE" -j "$CHAIN" 2>/dev/null
  iptables -t mangle -F "$CHAIN" 2>/dev/null
  iptables -t mangle -X "$CHAIN" 2>/dev/null
  ip rule del fwmark "$MARK" table "$TABLE" 2>/dev/null
  ip route flush table "$TABLE" 2>/dev/null
  return 0
}

up() {
  down
  ip rule add fwmark "$MARK" table "$TABLE"
  ip route add local 0.0.0.0/0 dev lo table "$TABLE"

  iptables -t mangle -N "$CHAIN"
  # Локальные и служебные адреса (включая сам сервер и DNS AmneziaWG) — мимо Xray.
  iptables -t mangle -A "$CHAIN" -m addrtype --dst-type LOCAL -j RETURN
  for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 \
             172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
    iptables -t mangle -A "$CHAIN" -d "$net" -j RETURN
  done
  iptables -t mangle -A "$CHAIN" -p tcp -j TPROXY --on-port "$PORT" --tproxy-mark "$MARK"
  iptables -t mangle -A "$CHAIN" -p udp -j TPROXY --on-port "$PORT" --tproxy-mark "$MARK"
  iptables -t mangle -A PREROUTING -i "$IFACE" -j "$CHAIN"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "Использование: $0 up|down" >&2; exit 1 ;;
esac
