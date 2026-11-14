#!/bin/bash
# MOST: какой трафик клиентов (awg0/wgr0) уходит через туннель awg1 на российский сервер.
# Ставится в /usr/local/sbin/most-ru-route скриптами ru-route.sh и ru-domains.sh, вызывается из awg1.conf.
#
# Режим — в /etc/most/ru-mode:
#   ip      (по умолчанию) — все российские IP (список ru.zone), кроме Telegram, Meta, Google и X;
#   domains — только сайты на .ru, .рф и .su (и домены из /etc/most/ru-domains.txt): их адреса
#             запоминает DNS сервера most-rudns, всё остальное идёт из Нидерландов.
LIST=/var/lib/most/ru.zone
FORCE=/var/lib/most/force.zone
MARK=0x65
TABLE=101
IFACES="awg0 wgr0"  # телефоны (AmneziaWG) и обычные роутеры (WireGuard)
MODE=$(cat /etc/most/ru-mode 2>/dev/null || echo ip)
RUDNS_PORT=5354
RU_DNS="77.88.8.8 77.88.8.1"  # Яндекс DNS, к нему обращается most-rudns за адресами .ru
# Публичные DNS с шифрованием (DoH/DoT): браузер сам переходит на них и обходит наш DNS,
# тогда сервер не узнает, что сайт на .ru. Закрываем — браузер возвращается к обычному DNS.
DOH_IPS="1.1.1.1 1.0.0.1 8.8.8.8 8.8.4.4 9.9.9.9 149.112.112.112 94.140.14.14 94.140.15.15"

if [[ $MODE == domains ]]; then SET=most-rudom; else SET=most-ru; fi

load_set() {
  if [[ $MODE == domains ]]; then
    # Адреса добавляет DNS при ответе; хранятся сутки после последнего запроса.
    ipset create most-rudom hash:ip timeout 86400 -exist
    return 0
  fi
  [[ -s $LIST ]] || return 1
  ipset create most-ru hash:net -exist
  ipset create most-ru-new hash:net -exist
  ipset flush most-ru-new
  sed 's/^/add most-ru-new /' "$LIST" | ipset restore -exist
  # Telegram, Meta, Google, X: исключения внутри российских сетей — идут из Нидерландов.
  [[ -s $FORCE ]] && sed 's/^/add most-ru-new /; s/$/ nomatch/' "$FORCE" | ipset restore -exist
  ipset swap most-ru-new most-ru
  ipset destroy most-ru-new
}

# Режим доменов: DNS-запросы клиентов — в наш DNS (most-rudns), шифрованный DNS публичных серверов закрыт.
dns_rules() {
  local op=$1 i ip proto
  for i in $IFACES; do
    ip link show "$i" >/dev/null 2>&1 || continue
    ip=$(ip -4 -o addr show dev "$i" | awk '{print $4}' | cut -d/ -f1 | head -1)
    for proto in udp tcp; do
      # К DNS самого сервера (блокировка рекламы) запросы идут как есть: он тоже запоминает адреса .ru.
      iptables -t nat "$op" PREROUTING -i "$i" -p $proto --dport 53 ! -d "$ip" -j REDIRECT --to-ports $RUDNS_PORT
    done
    for d in $DOH_IPS; do
      iptables "$op" FORWARD -i "$i" -d "$d" -p tcp -m multiport --dports 443,853 -j REJECT --reject-with tcp-reset
      iptables "$op" FORWARD -i "$i" -d "$d" -p udp --dport 443 -j REJECT
    done
  done
}

case "$1" in
  up)
    load_set
    ip route replace default dev awg1 table $TABLE
    ip rule show | grep -q "fwmark $MARK" || ip rule add fwmark $MARK table $TABLE
    sysctl -q -w net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.awg1.rp_filter=2
    for i in $IFACES; do iptables -t mangle -A PREROUTING -i $i -m set --match-set $SET dst -j MARK --set-mark $MARK; done
    iptables -t mangle -A FORWARD -o awg1 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
    iptables -t nat -A POSTROUTING -o awg1 -j MASQUERADE
    if [[ $MODE == domains ]]; then
      dns_rules -I
      # Российский DNS (адреса .ru-сайтов) — через туннель, чтобы ответил российскими адресами.
      for d in $RU_DNS; do ip route replace "$d/32" dev awg1; done
    fi
    true ;;
  down)
    # Снимаем правила обоих режимов: режим могли переключить, пока туннель работал.
    for s in most-ru most-rudom; do
      for i in $IFACES; do
        while iptables -t mangle -D PREROUTING -i $i -m set --match-set $s dst -j MARK --set-mark $MARK 2>/dev/null; do :; done
      done
    done
    iptables -t mangle -D FORWARD -o awg1 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null
    iptables -t nat -D POSTROUTING -o awg1 -j MASQUERADE 2>/dev/null
    dns_rules -D 2>/dev/null
    ip rule del fwmark $MARK table $TABLE 2>/dev/null
    ip route flush table $TABLE 2>/dev/null
    true ;;
  update)
    # Список российских сетей; проверяем, что он не пустой и похож на правду.
    tmp=$(mktemp)
    if python3 /opt/most/split.py --ru >"$tmp" && [[ $(wc -l <"$tmp") -gt 1000 ]]; then
      install -m 644 "$tmp" "$LIST"
    fi
    python3 /opt/most/split.py --force >"$tmp" && install -m 644 "$tmp" "$FORCE"
    rm -f "$tmp"
    if ip link show awg1 >/dev/null 2>&1; then load_set; fi ;;
esac
