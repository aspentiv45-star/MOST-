"""Раздельный туннель: российские сети идут мимо VPN, всё остальное — через сервер.

В конфиг клиента попадает AllowedIPs = всё адресное пространство минус
российские сети и локальные адреса. Точный список российских сетей —
это десятки тысяч префиксов, поэтому он укрупняется до блоков, пока
список для туннеля не уложится в разумный размер.
"""
import ipaddress
import json
import urllib.request

SOURCES = (
    "https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone",
    "https://stat.ripe.net/data/country-resource-list/data.json?resource=RU&v4_format=prefix",
)

# Локальные и служебные сети: всегда мимо VPN, чтобы работали домашний роутер и принтеры.
LOCAL = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
         "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/3")

MAX_ADDR = 2**32 - 1


def _parse(body):
    body = body.strip()
    if body.startswith("{"):
        prefixes = json.loads(body)["data"]["resources"]["ipv4"]
    else:
        prefixes = body.splitlines()
    nets = []
    for p in prefixes:
        p = p.strip()
        if not p or p.startswith("#"):
            continue
        try:
            nets.append(ipaddress.IPv4Network(p, strict=False))
        except ValueError:
            # RIPEstat иногда отдаёт диапазоны «a-b» вместо префиксов.
            if "-" in p:
                a, b = (ipaddress.IPv4Address(x.strip()) for x in p.split("-"))
                nets.extend(ipaddress.summarize_address_range(a, b))
    return nets


def fetch_ru_networks(timeout=60):
    """Скачивает российские IPv4-сети. Бросает исключение, если ни один источник не ответил."""
    last_error = None
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "most-bot"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                nets = _parse(resp.read().decode())
            if len(nets) > 1000:
                return nets
            last_error = ValueError(f"{url}: подозрительно мало сетей ({len(nets)})")
        except Exception as e:
            last_error = e
    raise RuntimeError(f"не удалось скачать список российских сетей: {last_error}")


def _complement(direct):
    """Всё IPv4-пространство минус direct, в виде минимального списка префиксов."""
    out = []
    cur = 0
    for net in ipaddress.collapse_addresses(direct):
        start, end = int(net.network_address), int(net.broadcast_address)
        if start > cur:
            out.extend(ipaddress.summarize_address_range(
                ipaddress.IPv4Address(cur), ipaddress.IPv4Address(start - 1)))
        cur = max(cur, end + 1)
    if cur <= MAX_ADDR:
        out.extend(ipaddress.summarize_address_range(
            ipaddress.IPv4Address(cur), ipaddress.IPv4Address(MAX_ADDR)))
    return out


def _gaps(direct):
    """Промежутки IPv4-пространства между сетями direct: список (начало, конец)."""
    gaps, cur = [], 0
    for net in ipaddress.collapse_addresses(direct):
        start, end = int(net.network_address), int(net.broadcast_address)
        if start > cur:
            gaps.append((cur, start - 1))
        cur = max(cur, end + 1)
    if cur <= MAX_ADDR:
        gaps.append((cur, MAX_ADDR))
    return gaps


def _cidrs(start, end):
    return list(ipaddress.summarize_address_range(ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)))


def tunnel_networks(ru_nets, limit=500):
    """Сети, которые идут через VPN: всё, кроме российских и локальных.

    Российские сети никогда не попадают в туннель. Чтобы список уложился
    в limit префиксов, самые мелкие зарубежные промежутки между российскими
    сетями отдаются «напрямую» — это доли процента адресного пространства.
    """
    local = [ipaddress.IPv4Network(n) for n in LOCAL]
    gaps = [(s, e, len(_cidrs(s, e))) for s, e in _gaps(list(ru_nets) + local)]
    # Минимальный размер промежутка, при котором список ещё укладывается в limit.
    sizes = sorted({e - s + 1 for s, e, _ in gaps})
    lo, hi = 0, len(sizes) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if sum(n for s, e, n in gaps if e - s + 1 >= sizes[mid]) <= limit:
            hi = mid
        else:
            lo = mid + 1
    keep = [(s, e) for s, e, _ in gaps if e - s + 1 >= sizes[lo]]
    return [net for s, e in keep for net in _cidrs(s, e)]


def _report():
    """Проверка на реальном списке: python3 split.py"""
    import bisect
    import socket

    ru = list(ipaddress.collapse_addresses(fetch_ru_networks()))
    ru_starts = [int(n.network_address) for n in ru]
    local = sum(ipaddress.IPv4Network(n).num_addresses for n in LOCAL)
    foreign = 2**32 - sum(n.num_addresses for n in ru) - local
    print(f"Российских сетей: {len(ru)}")

    def is_ru(ip):
        i = bisect.bisect_right(ru_starts, ip) - 1
        return i >= 0 and ip <= int(ru[i].broadcast_address)

    sites = ["youtube.com", "instagram.com", "web.telegram.org", "x.com", "discord.com", "chatgpt.com",
             "facebook.com", "linkedin.com", "gosuslugi.ru", "sberbank.ru", "vk.com", "ya.ru",
             "ozon.ru", "wildberries.ru", "tbank.ru", "avito.ru", "mail.ru", "kinopoisk.ru"]
    ips = {}
    for site in sites:
        try:
            ips[site] = int(ipaddress.IPv4Address(socket.gethostbyname(site)))
        except OSError:
            pass

    for limit in (500, 1000, 2000):
        tun = list(ipaddress.collapse_addresses(tunnel_networks(ru, limit)))
        starts = [int(n.network_address) for n in tun]

        def via_vpn(ip):
            i = bisect.bisect_right(starts, ip) - 1
            return i >= 0 and ip <= int(tun[i].broadcast_address)

        direct_foreign = foreign - sum(n.num_addresses for n in tun)
        print(f"\nlimit {limit}: сетей в конфиге {len(tun)}, "
              f"зарубежных адресов напрямую {100 * direct_foreign / foreign:.2f}%")
        for site, ip in ips.items():
            route = "VPN" if via_vpn(ip) else "напрямую"
            print(f"  {site:18} {str(ipaddress.IPv4Address(ip)):16} {'РФ ' if is_ru(ip) else 'зар'} -> {route}")


if __name__ == "__main__":
    _report()
