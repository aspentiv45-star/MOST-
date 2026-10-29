#!/usr/bin/env python3
"""Генерирует конфиг Xray для российского сервера-моста (MOST).

Берёт ссылку подключения к нидерландскому серверу из 3X-UI
(vless://, vmess:// или trojan://) и строит config.json, который
принимает клиентов на российском сервере и отправляет их трафик в Нидерланды.

Режимы:
  chain   — на RU-сервере свой VLESS + Reality (рекомендуется),
            дальше трафик идёт на NL-сервер по исходной ссылке.
  forward — простой TCP/UDP-проброс порта на NL-сервер (dokodemo-door),
            клиент использует ту же ссылку, только с IP российского сервера.

Ссылка для клиента печатается в stdout.
"""
import argparse
import base64
import json
import secrets
import sys
import uuid
from urllib.parse import parse_qs, quote, unquote, urlsplit


def die(msg):
    print(f"Ошибка: {msg}", file=sys.stderr)
    sys.exit(1)


def first(query, key, default=""):
    values = query.get(key)
    return values[0] if values else default


def parse_link(link):
    """Разбирает ссылку из 3X-UI в плоский словарь параметров."""
    link = link.strip()

    if link.startswith("vmess://"):
        raw = link[len("vmess://"):]
        raw += "=" * (-len(raw) % 4)
        try:
            j = json.loads(base64.b64decode(raw).decode())
        except Exception:
            die("не удалось разобрать vmess-ссылку")
        net = j.get("net", "tcp")
        return {
            "protocol": "vmess",
            "id": j["id"],
            "host": j["add"],
            "port": int(j["port"]),
            "type": net,
            "security": j.get("tls") or "none",
            "sni": j.get("sni", ""),
            "fp": j.get("fp", ""),
            "alpn": j.get("alpn", ""),
            "path": j.get("path", ""),
            "hosthdr": j.get("host", ""),
            "serviceName": j.get("path", "") if net == "grpc" else "",
            "mode": "",
            "scy": j.get("scy", "auto"),
            "pbk": "", "sid": "", "spx": "", "flow": "",
            "encryption": "none", "extra": "",
        }

    if not (link.startswith("vless://") or link.startswith("trojan://")):
        die("поддерживаются только ссылки vless://, vmess:// и trojan://")

    u = urlsplit(link)
    q = parse_qs(u.query)
    if not u.username or not u.hostname or not u.port:
        die("в ссылке нет UUID/пароля, адреса или порта")
    return {
        "protocol": u.scheme,
        "id": unquote(u.username),
        "host": u.hostname,
        "port": u.port,
        "type": first(q, "type", "tcp"),
        "security": first(q, "security", "none"),
        "sni": first(q, "sni"),
        "fp": first(q, "fp"),
        "alpn": first(q, "alpn"),
        "path": first(q, "path"),
        "hosthdr": first(q, "host"),
        "serviceName": first(q, "serviceName"),
        "mode": first(q, "mode"),
        "pbk": first(q, "pbk"),
        "sid": first(q, "sid"),
        "spx": first(q, "spx"),
        "flow": first(q, "flow"),
        "scy": "auto",
        # VLESS Encryption (mlkem768x25519plus...) из новых версий 3X-UI.
        "encryption": first(q, "encryption", "none"),
        "extra": first(q, "extra"),
    }


def stream_settings(p):
    net = p["type"] or "tcp"
    if net == "raw":
        net = "tcp"
    sec = p["security"] or "none"
    s = {"network": net, "security": sec}

    if sec == "reality":
        if not p["pbk"]:
            die("в ссылке Reality нет публичного ключа (pbk)")
        s["realitySettings"] = {
            "serverName": p["sni"],
            "fingerprint": p["fp"] or "chrome",
            "publicKey": p["pbk"],
            "shortId": p["sid"],
            "spiderX": p["spx"] or "/",
        }
    elif sec == "tls":
        tls = {"serverName": p["sni"] or p["host"], "fingerprint": p["fp"] or "chrome"}
        if p["alpn"]:
            tls["alpn"] = p["alpn"].split(",")
        s["tlsSettings"] = tls
    elif sec != "none":
        die(f"неизвестный тип шифрования: {sec}")

    if net == "ws":
        s["wsSettings"] = {"path": p["path"] or "/", "host": p["hosthdr"]}
    elif net == "grpc":
        s["grpcSettings"] = {"serviceName": p["serviceName"]}
    elif net == "xhttp":
        s["xhttpSettings"] = {"path": p["path"] or "/", "host": p["hosthdr"], "mode": p["mode"] or "auto"}
        if p["extra"]:
            try:
                s["xhttpSettings"]["extra"] = json.loads(p["extra"])
            except ValueError:
                die("не удалось разобрать параметр extra в ссылке")
    elif net == "httpupgrade":
        s["httpupgradeSettings"] = {"path": p["path"] or "/", "host": p["hosthdr"]}
    elif net != "tcp":
        die(f"транспорт {net} не поддерживается")
    return s


def outbound_to_nl(p):
    if p["protocol"] == "vless":
        user = {"id": p["id"], "encryption": p["encryption"]}
        if p["flow"]:
            user["flow"] = p["flow"]
        settings = {"vnext": [{"address": p["host"], "port": p["port"], "users": [user]}]}
    elif p["protocol"] == "vmess":
        settings = {"vnext": [{"address": p["host"], "port": p["port"],
                               "users": [{"id": p["id"], "security": p["scy"]}]}]}
    else:
        settings = {"servers": [{"address": p["host"], "port": p["port"], "password": p["id"]}]}
    return {"tag": "to-nl", "protocol": p["protocol"], "settings": settings,
            "streamSettings": stream_settings(p)}


def routing(ru_direct):
    rules = [{"type": "field", "ip": ["geoip:private"], "outboundTag": "block"}]
    if ru_direct:
        # Российские сайты открываем напрямую с RU-сервера, остальное — через NL.
        rules.append({"type": "field", "domain": ["domain:ru", "domain:su", "domain:xn--p1ai"],
                      "outboundTag": "direct"})
        rules.append({"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"})
    return {"domainStrategy": "AsIs", "rules": rules}


def build_chain(p, args):
    client_id = args.client_id or str(uuid.uuid4())
    short_id = args.short_id or secrets.token_hex(8)
    inbound = {
        "tag": "in",
        "listen": "0.0.0.0",
        "port": args.port,
        "protocol": "vless",
        "settings": {"clients": [{"id": client_id, "flow": "xtls-rprx-vision"}], "decryption": "none"},
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "target": f"{args.sni}:443",
                "xver": 0,
                "serverNames": [args.sni],
                "privateKey": args.private_key,
                "shortIds": [short_id],
            },
        },
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True},
    }
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": [
            outbound_to_nl(p),
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": routing(args.ru_direct),
    }
    client_link = (
        f"vless://{client_id}@{host_for_link(args.public_ip)}:{args.port}"
        f"?type=tcp&security=reality&pbk={args.public_key}&fp=chrome"
        f"&sni={args.sni}&sid={short_id}&flow=xtls-rprx-vision#{quote(args.name)}"
    )
    return config, client_link


def build_forward(p, args, original_link):
    inbound = {
        "tag": "in",
        "listen": "0.0.0.0",
        "port": args.port,
        "protocol": "dokodemo-door",
        "settings": {"address": p["host"], "port": p["port"], "network": "tcp,udp"},
    }
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [inbound],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }
    return config, rewrite_address(original_link, p, args)


def rewrite_address(link, p, args):
    """Та же ссылка, что из 3X-UI, но с адресом и портом российского сервера."""
    link = link.strip()
    if p["protocol"] == "vmess":
        raw = link[len("vmess://"):]
        raw += "=" * (-len(raw) % 4)
        j = json.loads(base64.b64decode(raw).decode())
        j["add"], j["port"], j["ps"] = args.public_ip, str(args.port), args.name
        return "vmess://" + base64.b64encode(json.dumps(j, ensure_ascii=False).encode()).decode()
    u = urlsplit(link)
    netloc = f"{u.username}@{host_for_link(args.public_ip)}:{args.port}"
    query = u.query
    # Без явного sni клиент подставит IP российского сервера, и TLS сломается.
    if p["security"] == "tls" and not p["sni"]:
        query += f"&sni={p['host']}"
    return u._replace(netloc=netloc, query=query, fragment=quote(args.name)).geturl()


def host_for_link(host):
    return f"[{host}]" if ":" in host else host


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--link", required=True, help="ссылка на NL-сервер из 3X-UI")
    ap.add_argument("--mode", choices=["chain", "forward"], default="chain")
    ap.add_argument("--port", type=int, default=443, help="порт на российском сервере")
    ap.add_argument("--public-ip", required=True, help="внешний IP российского сервера")
    ap.add_argument("--sni", default="ya.ru", help="сайт для маскировки Reality (режим chain)")
    ap.add_argument("--private-key", help="приватный ключ x25519 (режим chain)")
    ap.add_argument("--public-key", help="публичный ключ x25519 (режим chain)")
    ap.add_argument("--client-id", help="UUID клиента (по умолчанию генерируется)")
    ap.add_argument("--short-id", help="shortId Reality (по умолчанию генерируется)")
    ap.add_argument("--no-ru-direct", dest="ru_direct", action="store_false",
                    help="пускать и российские сайты через NL")
    ap.add_argument("--name", default="MOST", help="название подключения в клиенте")
    ap.add_argument("--out", required=True, help="куда записать config.json")
    args = ap.parse_args()

    p = parse_link(args.link)
    if args.mode == "chain":
        if not (args.private_key and args.public_key):
            die("для режима chain нужны --private-key и --public-key")
        config, client_link = build_chain(p, args)
    else:
        config, client_link = build_forward(p, args, args.link)

    with open(args.out, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(client_link)


if __name__ == "__main__":
    main()
