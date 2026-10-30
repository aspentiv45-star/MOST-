#!/bin/bash
# MOST — защита от жалоб хостинга: клиентам AmneziaWG запрещены торренты
# и отправка почты напрямую (порт 25, через него рассылают спам).
# Заодно ограничивает размер TCP-сегментов (MSS) под туннель с MTU 1280:
# без этого на мобильном интернете крупные страницы и загрузки зависают.
# Ставится в /usr/local/sbin/most-abuse и вызывается из most-abuse.service.
#
# Зашифрованный торрент-трафик так не поймать полностью, но обычные клиенты
# (qBittorrent, µTorrent, Transmission) с настройками по умолчанию блокируются.

IFACES="${MOST_AWG_IFACES:-awg0 wgr0}"
CHAIN=MOST_ABUSE
MSS_CHAIN=MOST_MSS

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
  while iptables -t mangle -D FORWARD -j "$MSS_CHAIN" 2>/dev/null; do :; done
  iptables -t mangle -F "$MSS_CHAIN" 2>/dev/null
  iptables -t mangle -X "$MSS_CHAIN" 2>/dev/null
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

  # MTU 1280 минус 40 байт заголовков IPv4 и TCP.
  iptables -t mangle -N "$MSS_CHAIN"
  for iface in $IFACES; do
    for dir in -i -o; do
      iptables -t mangle -A "$MSS_CHAIN" "$dir" "$iface" -p tcp --tcp-flags SYN,RST SYN \
        -m tcpmss --mss 1241:65535 -j TCPMSS --set-mss 1240
    done
  done
  iptables -t mangle -A FORWARD -j "$MSS_CHAIN"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "Использование: $0 up|down" >&2; exit 1 ;;
esac
