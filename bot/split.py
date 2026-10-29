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


def tunnel_networks(ru_nets, limit=500, threshold=0.1):
    """Сети, которые идут через VPN.

    Российские сети укрупняются до блоков /16, /15, ...: блок считается
    российским, если российских адресов в нём не меньше threshold.
    Берётся самое мелкое укрупнение, при котором список не длиннее limit.
    """
    ru_nets = list(ipaddress.collapse_addresses(ru_nets))
    local = [ipaddress.IPv4Network(n) for n in LOCAL]
    for gran in range(16, 5, -1):
        size = 2 ** (32 - gran)
        cover = {}
        for net in ru_nets:
            first = int(net.network_address) >> (32 - gran)
            if net.prefixlen >= gran:
                cover[first] = cover.get(first, 0) + net.num_addresses
            else:
                for block in range(first, first + net.num_addresses // size):
                    cover[block] = size
        direct = [ipaddress.IPv4Network((block << (32 - gran), gran))
                  for block, count in cover.items() if count >= threshold * size]
        tunnel = _complement(direct + local)
        if len(tunnel) <= limit:
            return tunnel
    return _complement(local)
