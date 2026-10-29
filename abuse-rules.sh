#!/bin/bash
# MOST — защита от жалоб хостинга: клиентам AmneziaWG запрещены торренты
# и отправка почты напрямую (порт 25, через него рассылают спам).
# Ставится в /usr/local/sbin/most-abuse и вызывается из most-abuse.service.
#
# Зашифрованный торрент-трафик так не поймать полностью, но обычные клиенты
# (qBittorrent, µTorrent, Transmission) с настройками по умолчанию блокируются.

IFACES="${MOST_AWG_IFACES:-awg0 wgr0}"
CHAIN=MOST_ABUSE

# Строки, которые встречаются только в протоколе BitTorrent, трекерах и DHT.
SIGNATURES=(
  "BitTorrent protocol"
  "peer_id="
  "info_hash"
  "/announce"
  ".torrent"
  "magnet:?"
  "d1:ad2:id20:"
  "get_peers"
  "announce_peer"
  "find_node"
)

down() {
  for iface in $IFACES awg0; do
    while iptables -D FORWARD -i "$iface" -j "$CHAIN" 2>/dev/null; do :; done
  done
  iptables -F "$CHAIN" 2>/dev/null
  iptables -X "$CHAIN" 2>/dev/null
  return 0
}

up() {
  down
  iptables -N "$CHAIN"
  iptables -A "$CHAIN" -p tcp --dport 25 -j REJECT --reject-with tcp-reset
  for sig in "${SIGNATURES[@]}"; do
    iptables -A "$CHAIN" -m string --algo bm --string "$sig" -j DROP
  done
  for iface in $IFACES; do
    iptables -I FORWARD 1 -i "$iface" -j "$CHAIN"
  done
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "Использование: $0 up|down" >&2; exit 1 ;;
esac
