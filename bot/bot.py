#!/usr/bin/env python3
"""MOST — два Telegram-бота для продажи доступа к AmneziaWG.

Каждый конфиг — отдельная подписка: купить новый или продлить имеющийся
можно только оплатой, которую одобряет администратор.

Клиентский бот: покупка и продление конфигов по скриншоту оплаты.
Админ-бот: одобрение оплат, клиенты, выдача конфигов, статистика.

Оба бота работают в одном процессе на сервере с AmneziaWG (awg0).
В /etc/amnezia/amneziawg/awg0.conf попадают только оплаченные конфиги,
изменения применяются через `awg syncconf` без перезапуска туннеля.
"""
import asyncio
import io
import ipaddress
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import qrcode
import split
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
IFACE = os.environ.get("AWG_IFACE", "awg0")
CONF = os.environ.get("AWG_CONF", f"/etc/amnezia/amneziawg/{IFACE}.conf")
# Отдельный интерфейс с чистым WireGuard (без маскировки) — для обычных роутеров.
# Если его конфига нет, роутеры подключаются к основному AmneziaWG.
ROUTER_IFACE = os.environ.get("ROUTER_IFACE", "wgr0")
ROUTER_CONF = os.environ.get("ROUTER_CONF", f"/etc/amnezia/amneziawg/{ROUTER_IFACE}.conf")
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/most/state.json")
ENDPOINT_HOST = os.environ["ENDPOINT_HOST"]
RELAY_HOST = os.environ.get("RELAY_HOST", "")
# Со входом в РФ клиенты получают один конфиг через него: прямое подключение к
# зарубежному серверу российские провайдеры часто режут. SEND_DIRECT=1 — выдавать и прямой.
SEND_DIRECT = os.environ.get("SEND_DIRECT", "0") == "1"
CLIENT_DNS = os.environ.get("CLIENT_DNS", "1.1.1.1, 1.0.0.1")
MAX_PEERS = int(os.environ.get("MAX_PEERS", "5"))
PRICE = int(os.environ.get("PRICE", "200"))
ROUTER_PRICE = int(os.environ.get("ROUTER_PRICE", "500"))
DAYS = int(os.environ.get("DAYS", "30"))
PAY_FILE = os.environ.get("PAY_FILE", "/etc/most/pay.txt")
# approve — купить может только тот, кого администратор пустил; open — любой.
ACCESS = os.environ.get("ACCESS", "open")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
# Раздельный туннель: российские сайты и приложения — напрямую с IP устройства.
SPLIT_RU = os.environ.get("SPLIT_RU", "1") == "1"
SPLIT_FILE = os.environ.get("SPLIT_FILE", "/var/lib/most/split.txt")
SPLIT_LIMIT = int(os.environ.get("SPLIT_LIMIT", "2000"))
# Свои сети «всегда через VPN»: по одной на строку, например 1.2.3.0/24.
FORCE_FILE = os.environ.get("FORCE_FILE", "/etc/most/force_vpn.txt")
FULL_TUNNEL = "0.0.0.0/0, ::/0"

DAY = 86400
# Цель оплаты: новый конфиг для устройства или для роутера; иначе — публичный ключ продлеваемого конфига.
NEW = "new"
NEW_ROUTER = "new-router"          # роутер на чистом WireGuard
NEW_ROUTER_AWG = "new-router-awg"  # роутер с AmneziaWG (Keenetic, OpenWrt)
KIND_BY_TARGET = {NEW: "device", NEW_ROUTER: "router", NEW_ROUTER_AWG: "router-awg"}
ROUTER_KINDS = ("router", "router-awg")

# Параметры обфускации AmneziaWG: клиенту они нужны точно такими же, как у сервера.
OBFS_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
             "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")

BTN_BUY = "➕ Купить конфиг"
BTN_LIST = "📄 Мои конфиги"
BTN_HELP = "❓ Как подключить"
KEYBOARD = ReplyKeyboardMarkup([[BTN_BUY], [BTN_LIST, BTN_HELP]], resize_keyboard=True)

HELP_TEXT = (
    "Как подключиться — коротко:\n\n"
    "1. Установите AmneziaVPN или AmneziaWG (App Store / Google Play / amnezia.org).\n"
    f"2. Нажмите «{BTN_BUY}», оплатите и пришлите скриншот перевода.\n"
    "3. После подтверждения бот пришлёт файл конфига.\n"
    "4. В приложении: «+» → импорт из файла (на iPhone: нажмите на файл → «Поделиться» → Amnezia).\n"
    "5. Включите туннель и проверьте на ipinfo.io — должны быть Нидерланды.\n\n"
    + ("Российские сайты и приложения работают напрямую, VPN выключать не нужно.\n\n" if SPLIT_RU else "")
    + ("Если конфиг с «-nl» в названии не подключается — пользуйтесь основным.\n\n"
       if RELAY_HOST and SEND_DIRECT else "")
    + f"Цены на {DAYS} дней: конфиг для себя (одно устройство) — {PRICE} ₽, для роутера (весь дом) — {ROUTER_PRICE} ₽.\n"
    "Для второго устройства купите ещё один конфиг.\n"
    f"Продлить конфиг: «{BTN_LIST}» → «🔄 Продлить».\n\n"
    "👇 Подробные инструкции для вашего устройства:"
)

# Подробные инструкции для клиентов — открываются кнопками из «❓ Как подключить».
GUIDES = {
    "what": (
        "🛒 Что купить?\n\n"
        f"📱 «Для себя» — {PRICE} ₽\n"
        "Телефон, планшет или компьютер. Один конфиг — одно устройство. "
        "Для двух телефонов купите два конфига.\n\n"
        f"📡 «Роутер AmneziaWG» — {ROUTER_PRICE} ₽\n"
        "Для роутеров Keenetic (KeeneticOS 4.2 и новее) и OpenWrt. VPN работает сразу на всех устройствах дома. "
        "Самый надёжный вариант для роутера: соединение замаскировано.\n\n"
        f"📡 «Роутер WireGuard» — {ROUTER_PRICE} ₽\n"
        "Для любого другого роутера, где в настройках есть «VPN-клиент» → «WireGuard» "
        "(TP-Link, Asus, Xiaomi, MikroTik и др.). Без маскировки — у части провайдеров может не работать.\n\n"
        "Не уверены — берите «Для себя»: работает у всех."
    ),
    "ios": (
        "🍏 iPhone и iPad\n\n"
        "1. Установите из App Store приложение AmneziaWG (или AmneziaVPN).\n"
        f"2. В боте: «{BTN_LIST}» → «⬇️ Скачать». Придёт файл vpn1.conf.\n"
        "3. Нажмите на файл → значок «Поделиться» (квадрат со стрелкой) → выберите AmneziaWG.\n"
        "   Если AmneziaWG нет в списке: «Сохранить в Файлы», затем в AmneziaWG «+» → «Создать из файла или архива» → выберите vpn1.conf.\n"
        "4. iPhone спросит «Разрешить добавление конфигураций VPN?» → «Разрешить», подтвердите код или Face ID.\n"
        "5. Включите переключатель напротив vpn1.\n"
        "6. Проверка: откройте ipinfo.io — должны быть Нидерланды (Netherlands).\n\n"
        "❗ Включайте только один конфиг и выключите другие VPN-приложения."
    ),
    "android": (
        "🤖 Android\n\n"
        "1. Установите из Google Play приложение AmneziaWG (или AmneziaVPN).\n"
        f"2. В боте: «{BTN_LIST}» → «⬇️ Скачать». Придёт файл vpn1.conf.\n"
        "3. Нажмите на файл → «⋮» → «Сохранить в загрузки» (или «Сохранить в музыку/файлы»).\n"
        "4. В AmneziaWG нажмите «+» → «Импорт из файла» → папка «Загрузки» → vpn1.conf.\n"
        "   Вместо шагов 3–4 можно нажать «+» → «Сканировать QR-код» и навести камеру на QR из бота (если он пришёл).\n"
        "5. Включите переключатель напротив vpn1, на вопрос о VPN-подключении нажмите «ОК».\n"
        "6. Проверка: откройте ipinfo.io — должны быть Нидерланды (Netherlands).\n\n"
        "❗ Включайте только один конфиг и выключите другие VPN-приложения."
    ),
    "pc": (
        "💻 Компьютер (Windows, macOS, Linux)\n\n"
        "1. Скачайте AmneziaVPN с сайта amnezia.org и установите.\n"
        f"2. В боте (Telegram на компьютере): «{BTN_LIST}» → «⬇️ Скачать» → сохраните vpn1.conf.\n"
        "3. В AmneziaVPN: «+» (или «Добавить сервер») → «Файл с настройками» → выберите vpn1.conf.\n"
        "4. Нажмите «Подключиться».\n"
        "5. Проверка: откройте ipinfo.io — должны быть Нидерланды (Netherlands).\n\n"
        "❗ Один конфиг — одно устройство. Для компьютера и телефона нужны два конфига."
    ),
    "router-awg": (
        "📡 Роутер AmneziaWG (Keenetic, OpenWrt)\n\n"
        "Keenetic (KeeneticOS 4.2 и новее):\n"
        "1. Откройте my.keenetic.net → «Интернет» → «Другие подключения» → «WireGuard» → «Загрузить из файла» → rtr1.conf.\n"
        "2. Включите у подключения «Использовать для выхода в интернет».\n"
        "3. Откройте my.keenetic.net/a и выполните команду, которую бот прислал вместе с конфигом "
        "(начинается с «interface Wireguard0 wireguard asc»), затем «system configuration save».\n"
        "4. Выключите и включите подключение.\n\n"
        "OpenWrt:\n"
        "1. System → Software: установите kmod-amneziawg, amneziawg-tools, luci-proto-amneziawg.\n"
        "2. Network → Interfaces → Add new interface → протокол AmneziaWG → «Import configuration» → rtr1.conf.\n"
        "3. Добавьте интерфейс в зону wan и включите его.\n\n"
        "Проверка: с любого устройства дома ipinfo.io — Нидерланды."
    ),
    "router-wg": (
        "📡 Роутер WireGuard (TP-Link, Asus, Xiaomi, MikroTik и др.)\n\n"
        "1. Откройте настройки роутера → «VPN-клиент» (или «VPN», «Другие подключения») → «WireGuard».\n"
        "2. Загрузите файл rtr1.conf. Если роутер не умеет загружать файл — впишите поля вручную: "
        "бот прислал их все вместе с конфигом (ключи, адрес, порт).\n"
        "3. «Разрешённые IP-адреса»: 0.0.0.0/0, «Ключ PSK» оставьте пустым.\n"
        "4. Сохраните, включите подключение и выберите его для выхода в интернет.\n\n"
        "Проверка: с любого устройства дома ipinfo.io — Нидерланды.\n\n"
        "❗ Если не подключается — ваш провайдер режет обычный WireGuard. Тогда нужен роутер с AmneziaWG "
        "(Keenetic, OpenWrt) или конфиг «Для себя» на каждое устройство."
    ),
    "trouble": (
        "🆘 Не работает?\n\n"
        f"1. Проверьте срок: «{BTN_LIST}» — у конфига должен быть 🟢. Если ⚪️ — продлите.\n"
        "2. Выключите и включите VPN. Выключите другие VPN-приложения.\n"
        f"3. Скачайте конфиг заново («{BTN_LIST}» → «⬇️ Скачать»), удалите старый в приложении и импортируйте новый.\n"
        "4. Попробуйте другую сеть: Wi-Fi вместо мобильного интернета или наоборот.\n"
        "5. Один конфиг нельзя включать на двух устройствах одновременно — будут обрывы.\n"
        "6. Торренты через VPN заблокированы — это не ошибка.\n\n"
        "Не помогло — напишите администратору, приложите скриншот ошибки."
    ),
}

HELP_BUTTONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🛒 Что купить?", callback_data="help:what")],
    [InlineKeyboardButton("🍏 iPhone", callback_data="help:ios"),
     InlineKeyboardButton("🤖 Android", callback_data="help:android")],
    [InlineKeyboardButton("💻 Компьютер", callback_data="help:pc")],
    [InlineKeyboardButton("📡 Роутер AmneziaWG", callback_data="help:router-awg"),
     InlineKeyboardButton("📡 Роутер WireGuard", callback_data="help:router-wg")],
    [InlineKeyboardButton("🆘 Не работает", callback_data="help:trouble")],
])

log = logging.getLogger("most")
lock = asyncio.Lock()
USER_BOT = None
ADMIN_BOT = None


def run(cmd, data=None):
    return subprocess.run(cmd, input=data, capture_output=True, text=True, check=True).stdout.strip()


def fmt_date(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


# ---------- состояние ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    state.setdefault("users", {})
    state.setdefault("peers", {})
    state.setdefault("payments", {})
    state.setdefault("next_payment", 1)
    # Переход со старой схемы «подписка на человека»: срок переносится на его конфиги.
    for p in state["peers"].values():
        if "until" not in p:
            p["until"] = state["users"].get(p["user"], {}).get("until", 0)
            p["warned"] = 0
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE))
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def get_user(state, user):
    u = state["users"].setdefault(str(user.id), {"name": ""})
    u["name"] = f"{user.full_name} (@{user.username})" if user.username else (user.full_name or str(user.id))
    return u


def user_name(state, uid):
    return state["users"].get(str(uid), {}).get("name", str(uid))


def is_admin(uid):
    return int(uid) in ADMINS


def is_approved(state, uid):
    """Может ли человек покупать. Кто уже платил или получал конфиги — пущен автоматически."""
    if ACCESS == "open" or is_admin(uid):
        return True
    u = state["users"].get(str(uid), {})
    if "approved" in u:
        return u["approved"]
    return any(p["user"] == str(uid) for p in state["peers"].values()) or any(
        p["user"] == str(uid) and p["status"] == "approved" for p in state["payments"].values())


def closed_text(state, uid):
    if state["users"].get(str(uid), {}).get("approved") is False:
        return "Администратор отклонил запрос доступа."
    return "Доступ по приглашению. Запрос отправлен администратору — бот напишет, когда вас пустят."


def peer_active(peer):
    return is_admin(peer["user"]) or peer["until"] > time.time()


def user_peers(state, uid):
    return {pub: p for pub, p in state["peers"].items() if p["user"] == str(uid)}


def peer_status(peer):
    if is_admin(peer["user"]):
        return "🟢 без срока (администратор)"
    if peer["until"] > time.time():
        return f"🟢 до {fmt_date(peer['until'])}"
    if peer["until"]:
        return f"⚪️ закончился {fmt_date(peer['until'])}"
    return "⚪️ не оплачен"


# ---------- конфиг сервера ----------

def interface_section(conf=CONF):
    """Часть конфига интерфейса до первого [Peer] — её бот никогда не меняет."""
    with open(conf) as f:
        lines = f.read().splitlines()
    head = []
    for line in lines:
        if line.strip() == "[Peer]":
            break
        head.append(line)
    while head and not head[-1].strip():
        head.pop()
    return head


def server_params(conf=CONF):
    params = {}
    for line in interface_section(conf):
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            params[key.strip()] = value.strip()
    return params


def plain_routers():
    return os.path.exists(ROUTER_CONF)


def peer_iface(peer):
    """Интерфейс и его конфиг, к которому подключается этот конфиг клиента."""
    if peer.get("kind") == "router" and plain_routers():
        return ROUTER_IFACE, ROUTER_CONF
    return IFACE, CONF


def write_server_conf(state):
    """Пересобирает конфиги интерфейсов: в них только оплаченные конфиги."""
    ifaces = [(IFACE, CONF)] + ([(ROUTER_IFACE, ROUTER_CONF)] if plain_routers() else [])
    # Роутеры, выданные до появления отдельного интерфейса, переезжают в его подсеть.
    for p in state["peers"].values():
        conf = peer_iface(p)[1]
        net = ipaddress.ip_interface(server_params(conf)["Address"].split(",")[0].strip()).network
        if ipaddress.ip_address(p["ip"]) not in net:
            p["ip"] = next_free_ip(state, server_params(conf))
    for iface, conf in ifaces:
        out = interface_section(conf)
        for pub, p in state["peers"].items():
            if peer_active(p) and peer_iface(p)[0] == iface:
                out += ["", "[Peer]", f"# {p['user']} {p['name']}", f"PublicKey = {pub}", f"AllowedIPs = {p['ip']}/32"]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(conf))
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, conf)

        stripped = run(["awg-quick", "strip", iface])
        with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
            f.write(stripped + "\n")
            f.flush()
            run(["awg", "syncconf", iface, f.name])


def apply_or_rollback(state, old_state):
    """Применяет конфиг сервера; при ошибке возвращает прежний и пробрасывает её дальше."""
    try:
        write_server_conf(state)
    except Exception:
        try:
            write_server_conf(old_state)
        except Exception:
            pass
        raise


def next_free_ip(state, params):
    iface = ipaddress.ip_interface(params["Address"].split(",")[0].strip())
    used = {p["ip"] for p in state["peers"].values()} | {str(iface.ip)}
    for host in iface.network.hosts():
        if str(host) not in used:
            return str(host)
    raise RuntimeError("в подсети закончились адреса")


def is_router(state, target):
    if target in (NEW_ROUTER, NEW_ROUTER_AWG):
        return True
    return state["peers"].get(target, {}).get("kind") in ROUTER_KINDS


def price_for(state, target):
    return ROUTER_PRICE if is_router(state, target) else PRICE


def create_peer(state, uid, until, kind="device"):
    """Добавляет конфиг в state (без применения на сервере). Возвращает его ключ и данные."""
    prefix = "rtr" if kind in ROUTER_KINDS else "vpn"
    names = {p["name"] for p in user_peers(state, uid).values()}
    n = next(i for i in range(1, 1000) if f"{prefix}{i}" not in names)
    priv = run(["awg", "genkey"])
    pub = run(["awg", "pubkey"], priv + "\n")
    peer = {"user": str(uid), "name": f"{prefix}{n}", "kind": kind,
            "priv": priv, "created": fmt_date(time.time()), "until": until, "warned": 0}
    peer["ip"] = next_free_ip(state, server_params(peer_iface(peer)[1]))
    state["peers"][pub] = peer
    return pub, peer


def client_conf(peer, params, host):
    lines = [
        "[Interface]",
        f"PrivateKey = {peer['priv']}",
        f"Address = {peer['ip']}/32",
        f"DNS = {CLIENT_DNS}",
        "MTU = 1280",
    ]
    lines += [f"{k} = {params[k]}" for k in OBFS_KEYS if k in params]
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {run(['awg', 'pubkey'], params['PrivateKey'] + chr(10))}",
        f"Endpoint = {host}:{params['ListenPort']}",
        # Роутеры часто не принимают IPv6 в этом поле — им только IPv4.
        f"AllowedIPs = {'0.0.0.0/0' if peer.get('kind') in ROUTER_KINDS else allowed_ips()}",
        "PersistentKeepalive = 25",
    ]
    return "\n".join(lines) + "\n"


def allowed_ips():
    if SPLIT_RU:
        try:
            with open(SPLIT_FILE) as f:
                return f.read().strip()
        except FileNotFoundError:
            pass
    return FULL_TUNNEL


async def refresh_split():
    """Пересчитывает список сетей для туннеля по свежему списку российских сетей."""
    nets = await asyncio.to_thread(split.fetch_ru_networks)
    force = []
    try:
        with open(FORCE_FILE) as f:
            force = [line.split("#")[0].strip() for line in f if line.split("#")[0].strip()]
    except FileNotFoundError:
        pass
    tunnel = await asyncio.to_thread(split.tunnel_networks, nets, SPLIT_LIMIT, force)
    text = ", ".join(map(str, tunnel)) + ", ::/0"
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SPLIT_FILE))
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, SPLIT_FILE)
    log.info("раздельный туннель: %d российских сетей -> %d сетей через VPN", len(nets), len(tunnel))


async def split_loop():
    while SPLIT_RU:
        try:
            await refresh_split()
            await asyncio.sleep(DAY)
        except Exception:
            log.exception("не удалось обновить список российских сетей")
            await asyncio.sleep(3600)


# ---------- клиентский бот ----------

async def send_peer(chat_id, peer):
    params = server_params(peer_iface(peer)[1])
    # Имя файла становится именем туннеля; на iOS оно не длиннее 15 символов.
    if RELAY_HOST:
        variants = [(f"{peer['name']}.conf", RELAY_HOST)]
        if SEND_DIRECT:
            variants.append((f"{peer['name']}-nl.conf", ENDPOINT_HOST))
    else:
        variants = [(f"{peer['name']}.conf", ENDPOINT_HOST)]
    for filename, host in variants:
        text = client_conf(peer, params, host)
        await USER_BOT.send_document(chat_id, document=io.BytesIO(text.encode()), filename=filename,
                                     caption=f"{filename} — {peer_status(peer)}")
        if peer.get("kind") in ROUTER_KINDS:
            continue
        # Конфиг с раздельным туннелем не помещается в QR-код — тогда только файл.
        if len(text) < 2000:
            png = io.BytesIO()
            qrcode.make(text).save(png, format="PNG")
            png.seek(0)
            await USER_BOT.send_photo(chat_id, photo=png, caption=f"QR-код: {filename}")
    if peer.get("kind") in ROUTER_KINDS:
        await USER_BOT.send_message(chat_id, router_help(peer, params))


def router_requirements(target):
    if target == NEW_ROUTER and plain_routers():
        return ("Подходит роутер с VPN-клиентом WireGuard: Keenetic, TP-Link, Asus, Xiaomi, MikroTik, OpenWrt и другие. "
                "Проверьте, что в настройках роутера есть раздел «VPN-клиент» → «WireGuard».")
    return ("Подходят только роутеры с поддержкой AmneziaWG:\n"
            "• Keenetic с KeeneticOS 4.2 или новее;\n"
            "• роутеры с прошивкой OpenWrt и пакетом amneziawg.\n"
            "Обычный WireGuard (TP-Link, Asus, MikroTik, роутеры провайдеров) не подойдёт — подключение не заработает.")


def router_help(peer, params):
    if peer_iface(peer)[0] == ROUTER_IFACE:
        text = client_conf(peer, params, RELAY_HOST or ENDPOINT_HOST)
        fields = dict(line.split(" = ", 1) for line in text.splitlines() if " = " in line)
        host, _, port = fields["Endpoint"].rpartition(":")
        return (
            f"📡 Настройка роутера для {peer['name']}\n\n"
            "1. Откройте настройки роутера → «VPN-клиент» (или «VPN», «Другие подключения») → «WireGuard».\n"
            f"2. Загрузите файл {peer['name']}.conf. Если роутер не умеет загружать файл, впишите поля вручную:\n\n"
            f"Закрытый ключ: {fields['PrivateKey']}\n"
            f"IP-адрес: {fields['Address']}\n"
            f"DNS: {fields['DNS']}\n"
            f"MTU: {fields['MTU']}\n"
            f"Открытый ключ узла: {fields['PublicKey']}\n"
            f"Адрес конечной точки: {host}\n"
            f"Порт: {port}\n"
            "Разрешённые IP-адреса: 0.0.0.0/0\n"
            "Keepalive: 25\n"
            "Ключ PSK: оставьте пустым\n\n"
            "3. Сохраните и включите подключение, выберите его для выхода в интернет.\n"
            "4. Проверьте с любого устройства дома: на ipinfo.io должны быть Нидерланды.\n\n"
            "Один конфиг — один роутер. Торренты через VPN заблокированы."
        )
    asc = " ".join(params.get(k, "0") for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"))
    return (
        f"📡 Настройка роутера Keenetic для {peer['name']}\n\n"
        "1. Откройте my.keenetic.net → «Интернет» → «Другие подключения» → «WireGuard» → "
        f"«Загрузить из файла» и выберите файл {peer['name']}.conf.\n"
        "2. В настройках подключения включите «Использовать для выхода в интернет».\n"
        "3. Откройте my.keenetic.net/a (командная строка) и выполните по очереди, "
        "заменив Wireguard0 на имя вашего подключения, если оно другое:\n\n"
        f"interface Wireguard0 wireguard asc {asc}\n"
        "system configuration save\n\n"
        "4. Выключите и включите подключение. Проверьте с любого устройства дома: на ipinfo.io должны быть Нидерланды.\n\n"
        "OpenWrt: установите пакеты kmod-amneziawg, amneziawg-tools, luci-proto-amneziawg "
        "и импортируйте файл в «Network → Interfaces → Add new interface → AmneziaWG».\n\n"
        "Один конфиг — один роутер. Торренты через VPN заблокированы."
    )


def pay_text():
    # Файл читается при каждом нажатии: реквизиты можно менять без перезапуска бота.
    try:
        with open(PAY_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Реквизиты не указаны — напишите администратору."


def target_label(state, target):
    if target == NEW:
        return "новый конфиг для себя"
    if target == NEW_ROUTER:
        return "новый конфиг для роутера (WireGuard)"
    if target == NEW_ROUTER_AWG:
        return "новый конфиг для роутера (AmneziaWG)"
    peer = state["peers"].get(target)
    return f"продление {peer['name']} ({peer_status(peer)})" if peer else "продление удалённого конфига"


async def request_access(user):
    """Отправляет администраторам запрос доступа (один раз)."""
    async with lock:
        state = load_state()
        u = get_user(state, user)
        first = "requested" not in u
        u.setdefault("requested", int(time.time()))
        save_state(state)
    if first:
        for admin in ADMINS:
            try:
                await ADMIN_BOT.send_message(admin, f"🙋 Запрос доступа\n{u['name']}\nID {user.id}",
                                             reply_markup=access_buttons(user.id))
            except Exception:
                log.exception("не удалось отправить запрос доступа админу %s", admin)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with lock:
        state = load_state()
        get_user(state, update.effective_user)
        save_state(state)
    if not is_approved(state, update.effective_user.id):
        await request_access(update.effective_user)
        await update.message.reply_text(f"Привет! Это VPN на AmneziaWG.\n\n{closed_text(state, update.effective_user.id)}")
        return
    mine = user_peers(state, update.effective_user.id)
    active = sum(1 for p in mine.values() if peer_active(p))
    await update.message.reply_text(
        f"Привет! Это VPN на AmneziaWG.\nНа {DAYS} дней: для себя (одно устройство) — {PRICE} ₽, "
        f"для роутера (весь дом) — {ROUTER_PRICE} ₽.\n\n"
        + (f"Ваших конфигов: {len(mine)}, активных: {active}. Подробнее — «{BTN_LIST}»." if mine
           else f"Чтобы подключиться, нажмите «{BTN_BUY}»."),
        reply_markup=KEYBOARD)


async def ask_payment(message, ctx, target, state):
    ctx.user_data["pay_for"] = target
    warning = f"{router_requirements(target)}\n\n" if target in (NEW_ROUTER, NEW_ROUTER_AWG) else ""
    await message.reply_text(
        f"Оплата: {target_label(state, target)} — {price_for(state, target)} ₽ за {DAYS} дней.\n\n"
        f"{warning}{pay_text()}\n\n"
        "После оплаты пришлите сюда скриншот перевода одним фото. "
        "Администратор проверит его, и бот сразу пришлёт результат.")


async def buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not is_approved(state, update.effective_user.id):
        await request_access(update.effective_user)
        await update.message.reply_text(closed_text(state, update.effective_user.id))
        return
    if len(user_peers(state, update.effective_user.id)) >= MAX_PEERS and not is_admin(update.effective_user.id):
        await update.message.reply_text(
            f"У вас уже {MAX_PEERS} конфигов — это максимум. Продлите или удалите ненужный в «{BTN_LIST}».")
        return
    text, buttons = BUY_MENU["root"]
    await update.message.reply_text(text, reply_markup=buttons)


def _buy_screen(guide, target, price, back):
    """Экран «инструкция + купить»: подробные шаги для выбранного устройства и кнопка оплаты."""
    return (GUIDES[guide], InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Купить — {price} ₽ за {DAYS} дней", callback_data=f"buy:{target}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"menu:{back}")],
    ]))


# Меню покупки по шагам: устройство или роутер → конкретный вариант → инструкция и «Купить».
BUY_MENU = {
    "root": (f"Что подключаем? Срок — {DAYS} дней.", InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📱 Для устройства — {PRICE} ₽", callback_data="menu:device")],
        [InlineKeyboardButton(f"📡 Для роутера (весь дом) — {ROUTER_PRICE} ₽", callback_data="menu:router")],
        [InlineKeyboardButton("🛒 Что выбрать?", callback_data="menu:what")],
    ])),
    "device": ("📱 Какое устройство? Один конфиг — одно устройство.", InlineKeyboardMarkup([
        [InlineKeyboardButton("🍏 iPhone / iPad", callback_data="menu:ios"),
         InlineKeyboardButton("🤖 Android", callback_data="menu:android")],
        [InlineKeyboardButton("💻 Компьютер (Windows, macOS, Linux)", callback_data="menu:pc")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:root")],
    ])),
    "router": ("📡 Какой у вас роутер?\n\n"
               "Посмотрите название на наклейке снизу роутера или в его веб-интерфейсе.", InlineKeyboardMarkup([
        [InlineKeyboardButton("Keenetic или OpenWrt (AmneziaWG)", callback_data="menu:router-awg")],
        [InlineKeyboardButton("Другой роутер с WireGuard", callback_data="menu:router-wg")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:root")],
    ])),
    "what": (GUIDES["what"], InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:root")]])),
    "ios": _buy_screen("ios", NEW, PRICE, "device"),
    "android": _buy_screen("android", NEW, PRICE, "device"),
    "pc": _buy_screen("pc", NEW, PRICE, "device"),
    "router-awg": _buy_screen("router-awg", NEW_ROUTER_AWG, ROUTER_PRICE, "router"),
    "router-wg": _buy_screen("router-wg", NEW_ROUTER, ROUTER_PRICE, "router"),
}


async def on_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        file = await msg.photo[-1].get_file()
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file = await msg.document.get_file()
    else:
        await msg.reply_text("Пришлите скриншот оплаты картинкой.")
        return
    state = load_state()
    if not is_approved(state, update.effective_user.id):
        await request_access(update.effective_user)
        await msg.reply_text(closed_text(state, update.effective_user.id))
        return
    data = bytes(await file.download_as_bytearray())

    target = ctx.user_data.pop("pay_for", None)
    mine = user_peers(state, update.effective_user.id)
    if target is None and not mine:
        target = NEW
    if target is None:
        # Непонятно, за что оплата — спрашиваем, картинку держим до ответа.
        ctx.user_data["pending_photo"] = (data, msg.caption)
        rows = [[InlineKeyboardButton(f"📱 Новый для себя — {PRICE} ₽", callback_data=f"payfor:{NEW}")],
                [InlineKeyboardButton(f"📡 Роутер AmneziaWG — {ROUTER_PRICE} ₽", callback_data=f"payfor:{NEW_ROUTER_AWG}")],
                [InlineKeyboardButton(f"📡 Роутер WireGuard — {ROUTER_PRICE} ₽", callback_data=f"payfor:{NEW_ROUTER}")]]
        rows += [[InlineKeyboardButton(f"🔄 Продлить {p['name']}", callback_data=f"payfor:{pub}")]
                 for pub, p in mine.items()]
        await msg.reply_text("За что эта оплата?", reply_markup=InlineKeyboardMarkup(rows))
        return
    await submit_payment(update.effective_user, target, data, msg.caption, msg)


async def submit_payment(user, target, data, comment, reply_to):
    async with lock:
        state = load_state()
        u = get_user(state, user)
        pid = str(state["next_payment"])
        state["next_payment"] += 1
        price = price_for(state, target)
        state["payments"][pid] = {"user": str(user.id), "target": target, "status": "pending",
                                  "price": price, "created": int(time.time())}
        save_state(state)

    caption = (f"Оплата №{pid} — {price} ₽\n{u['name']}\nID {user.id}\n"
               f"За: {target_label(state, target)}")
    if comment:
        caption += f"\nКомментарий: {comment}"
    # file_id одного бота не годится для другого — пересылаем саму картинку,
    # а file_id админ-бота запоминаем, чтобы показать её снова в «Оплаты».
    photo_id = None
    for admin in ADMINS:
        try:
            sent = await ADMIN_BOT.send_photo(admin, photo=io.BytesIO(data), caption=caption,
                                              reply_markup=payment_buttons(pid))
            photo_id = photo_id or sent.photo[-1].file_id
        except Exception:
            log.exception("не удалось отправить оплату админу %s", admin)
    if photo_id:
        async with lock:
            state = load_state()
            state["payments"][pid]["photo"] = photo_id
            save_state(state)
    await reply_to.reply_text("Скриншот отправлен администратору. Как только он подтвердит оплату, придёт сообщение.")


async def list_configs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    mine = user_peers(state, update.effective_user.id)
    if not mine:
        await update.message.reply_text(f"Конфигов пока нет. Нажмите «{BTN_BUY}».")
        return
    for pub, p in mine.items():
        row = [InlineKeyboardButton("⬇️ Скачать", callback_data=f"get:{pub}")]
        if not is_admin(p["user"]):
            row.append(InlineKeyboardButton(f"🔄 Продлить — {price_for(state, pub)} ₽", callback_data=f"renew:{pub}"))
        buttons = InlineKeyboardMarkup([row, [InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{pub}")]])
        icon = "📡" if p.get("kind") in ROUTER_KINDS else "📱"
        await update.message.reply_text(f"{icon} {p['name']} — {peer_status(p)}", reply_markup=buttons)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=HELP_BUTTONS)


async def on_user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action, _, arg = query.data.partition(":")
    await query.answer()

    if action == "menu":
        text, buttons = BUY_MENU.get(arg, BUY_MENU["root"])
        await query.edit_message_text(text, reply_markup=buttons)
        return

    if action == "help":
        await query.message.reply_text(GUIDES.get(arg, HELP_TEXT), reply_markup=HELP_BUTTONS)
        return

    if action == "buy":
        state = load_state()
        if not is_approved(state, uid):
            await query.edit_message_text(closed_text(state, uid))
            return
        await query.edit_message_reply_markup(reply_markup=None)  # инструкция остаётся на экране
        await ask_payment(query.message, ctx, arg, state)
        return

    if action == "payfor":
        pending = ctx.user_data.pop("pending_photo", None)
        if not pending:
            await query.edit_message_text("Пришлите скриншот ещё раз.")
            return
        await query.edit_message_text(f"Оплата: {target_label(load_state(), arg)}.")
        await submit_payment(query.from_user, arg, pending[0], pending[1], query.message)
        return

    state = load_state()
    peer = state["peers"].get(arg)
    if not peer or peer["user"] != str(uid):
        await query.edit_message_text("Конфиг не найден.")
        return
    if action == "renew":
        await ask_payment(query.message, ctx, arg, state)
    elif action == "get":
        if not peer_active(peer):
            await query.message.reply_text(f"{peer['name']}: срок закончился, сначала продлите его.")
            return
        await send_peer(query.message.chat_id, peer)
    elif action == "del":
        await query.edit_message_text(
            f"Удалить {peer['name']}? Оплаченные дни не вернутся.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Да, удалить", callback_data=f"delyes:{arg}"),
                InlineKeyboardButton("Отмена", callback_data=f"delno:{arg}"),
            ]]))
    elif action == "delno":
        await query.edit_message_text(f"{peer['name']} — {peer_status(peer)}")
    elif action == "delyes":
        async with lock:
            state = load_state()
            old_state = json.loads(json.dumps(state))
            state["peers"].pop(arg, None)
            apply_or_rollback(state, old_state)
            save_state(state)
        await query.edit_message_text(f"{peer['name']} удалён.")


# ---------- админ-бот ----------

ABTN_PAY = "⏳ Оплаты"
ABTN_USERS = "👥 Клиенты"
ABTN_GIVE = "🎁 Выдать конфиг"
ABTN_STATS = "📊 Статистика"
ABTN_RESEND = "📤 Разослать конфиги"
ADMIN_KEYBOARD = ReplyKeyboardMarkup([[ABTN_PAY, ABTN_USERS], [ABTN_GIVE, ABTN_STATS], [ABTN_RESEND]],
                                     resize_keyboard=True)


def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and is_admin(update.effective_user.id):
            await func(update, ctx)
    return wrapper


def payment_buttons(pid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Одобрить (+{DAYS} дн.)", callback_data=f"ok:{pid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{pid}"),
    ]])


def access_buttons(uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Пустить", callback_data=f"acc:yes:{uid}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"acc:no:{uid}"),
    ]])


def peer_buttons(pub):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"➕ {DAYS} дней", callback_data=f"give:{pub}"),
        InlineKeyboardButton("⛔️ Отключить", callback_data=f"take:{pub}"),
    ]])


async def grant(uid, target, days, paid=True):
    """Выдаёт новый конфиг или продлевает имеющийся и отправляет клиенту. Возвращает текст итога."""
    async with lock:
        state = load_state()
        old_state = json.loads(json.dumps(state))
        state["users"].setdefault(str(uid), {"name": str(uid)})["approved"] = True
        if target in KIND_BY_TARGET:
            pub, peer = create_peer(state, uid, time.time() + days * DAY, KIND_BY_TARGET[target])
        else:
            pub, peer = target, state["peers"].get(target)
            if not peer:
                return "Конфиг уже удалён клиентом — продлевать нечего."
            peer["until"] = max(peer["until"], time.time()) + days * DAY
            peer["warned"] = 0
        apply_or_rollback(state, old_state)
        save_state(state)

    head = "Оплата подтверждена!" if paid else "Подарок от администратора!"
    try:
        if target in KIND_BY_TARGET:
            await USER_BOT.send_message(uid, f"{head} Ваш новый конфиг {peer['name']} "
                                             f"активен до {fmt_date(peer['until'])}:", reply_markup=KEYBOARD)
            await send_peer(uid, peer)
        else:
            await USER_BOT.send_message(uid, f"{head} {peer['name']} продлён до "
                                             f"{fmt_date(peer['until'])}. Если он уже есть в приложении — "
                                             "просто включите его.", reply_markup=KEYBOARD)
        sent = ""
    except Exception:
        log.exception("не удалось отправить клиенту %s", uid)
        sent = "\nКлиенту написать не удалось — он ещё не нажимал /start в клиентском боте."
    return f"{peer['name']}: {peer_status(peer)}{sent}"


async def take(pub):
    async with lock:
        state = load_state()
        old_state = json.loads(json.dumps(state))
        peer = state["peers"].get(pub)
        if peer:
            peer["until"] = 0
            apply_or_rollback(state, old_state)
            save_state(state)
        return peer


@admin_only
async def admin_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Админ-панель MOST.\n\n"
        "Скриншоты оплат приходят сюда сами — с кнопками «Одобрить» / «Отклонить». "
        "После одобрения клиент сразу получает новый конфиг или продление.\n\n"
        f"{ABTN_PAY} — неразобранные оплаты и запросы доступа\n"
        f"{ABTN_USERS} — клиенты и их конфиги: продлить или отключить\n"
        f"{ABTN_GIVE} — бесплатно выдать конфиг по ID\n"
        f"{ABTN_STATS} — кто подключён и трафик\n"
        f"{ABTN_RESEND} — отправить всем клиентам актуальные файлы их конфигов\n\n"
        "Команды: /send ID текст — написать клиенту, /broadcast текст — написать всем.",
        reply_markup=ADMIN_KEYBOARD)


@admin_only
async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    pending = [(pid, p) for pid, p in state["payments"].items() if p["status"] == "pending"]
    waiting = [uid for uid, u in state["users"].items()
               if u.get("requested") and "approved" not in u and not is_approved(state, uid)]
    for uid in waiting:
        await update.message.reply_text(f"🙋 Запрос доступа\n{user_name(state, uid)}\nID {uid}",
                                        reply_markup=access_buttons(uid))
    if not pending and not waiting:
        await update.message.reply_text("Неразобранных оплат нет.", reply_markup=ADMIN_KEYBOARD)
        return
    for pid, p in pending:
        caption = (f"Оплата №{pid} от {fmt_date(p['created'])}\n{user_name(state, p['user'])}\nID {p['user']}\n"
                   f"За: {target_label(state, p.get('target', NEW))}")
        if p.get("photo"):
            await update.message.reply_photo(p["photo"], caption=caption, reply_markup=payment_buttons(pid))
        else:
            await update.message.reply_text(caption, reply_markup=payment_buttons(pid))


@admin_only
async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    with_peers = {p["user"] for p in state["peers"].values()}
    active = {p["user"] for p in state["peers"].values() if peer_active(p)}
    await update.message.reply_text(
        f"Клиентов: {len(state['users'])}, с конфигами: {len(with_peers)}, с активными: {len(active)}.\n"
        f"Активных конфигов: {sum(1 for p in state['peers'].values() if peer_active(p))}.",
        reply_markup=ADMIN_KEYBOARD)
    peers = sorted(state["peers"].items(), key=lambda kv: -kv[1]["until"])
    for pub, p in peers[:40]:
        await update.message.reply_text(
            f"{user_name(state, p['user'])} · ID {p['user']}\n{p['name']} — {peer_status(p)}",
            reply_markup=None if is_admin(p["user"]) else peer_buttons(pub))
    if len(peers) > 40:
        await update.message.reply_text(f"Показаны первые 40 конфигов из {len(peers)}.")


@admin_only
async def admin_give_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["await"] = "give"
    await update.message.reply_text(
        f"Пришлите ID клиента и, через пробел, число дней (по умолчанию {DAYS}).\n"
        f"Например: 123456789 {DAYS}\n"
        f"Для роутера добавьте «роутер» (WireGuard) или «amnezia» (Keenetic/OpenWrt): "
        f"123456789 {DAYS} роутер\n\n"
        "Клиент получит новый конфиг бесплатно. ID видно в его оплатах и в «👥 Клиенты». "
        "Клиент должен хотя бы раз нажать /start в клиентском боте.")


@admin_only
async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    waiting = ctx.user_data.pop("await", None)
    if waiting == "resend":
        await resend_confirm(update, ctx)
        return
    if waiting != "give":
        await update.message.reply_text("Выберите действие на кнопках ниже.", reply_markup=ADMIN_KEYBOARD)
        return
    words = update.message.text.lower().split()
    AWG_WORDS = ("amnez", "амнез", "keenetic", "кинет", "awg", "openwrt")
    awg = any(w.startswith(AWG_WORDS) for w in words)
    router = awg or any(w.startswith(("роут", "router")) for w in words)
    parts = [w for w in words if w.isdigit()]
    if not parts or not parts[0].isdigit() or (len(parts) > 1 and not parts[1].isdigit()):
        ctx.user_data["await"] = "give"
        await update.message.reply_text(f"Нужно число — ID клиента. Например: 123456789 {DAYS}")
        return
    uid, days = int(parts[0]), int(parts[1]) if len(parts) > 1 else DAYS
    result = await grant(uid, NEW_ROUTER_AWG if awg else NEW_ROUTER if router else NEW, days, paid=False)
    await update.message.reply_text(f"Готово: выдан конфиг.\n{result}", reply_markup=ADMIN_KEYBOARD)


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return
    action, _, arg = query.data.partition(":")

    if action == "acc":
        verdict, _, uid = arg.partition(":")
        async with lock:
            state = load_state()
            state["users"].setdefault(uid, {"name": uid})["approved"] = verdict == "yes"
            save_state(state)
        await query.answer()
        try:
            await USER_BOT.send_message(
                int(uid), f"Доступ открыт! Нажмите «{BTN_BUY}»." if verdict == "yes"
                else "Администратор отклонил запрос доступа.", reply_markup=KEYBOARD if verdict == "yes" else None)
        except Exception:
            pass
        await query.edit_message_text(f"{query.message.text}\n\n{'✅ Пущен' if verdict == 'yes' else '❌ Отказано'}")
        return

    if action == "resend":
        await query.answer()
        text = ctx.user_data.pop("resend_text", None)
        if arg == "yes" and text:
            await query.edit_message_text("📤 Рассылаю…")
            await resend_all(query, text)
        elif arg == "yes":
            await query.edit_message_text(f"Текст рассылки потерялся — нажмите «{ABTN_RESEND}» ещё раз.")
        else:
            await query.edit_message_text("Рассылка отменена.")
        return

    if action in ("give", "take"):
        state = load_state()
        peer = state["peers"].get(arg)
        if not peer:
            await query.answer("Конфиг удалён.")
            return
        if action == "give":
            result = await grant(int(peer["user"]), arg, DAYS, paid=False)
            await query.answer(result.split("\n")[0][:190])
        else:
            await take(arg)
            await query.answer(f"{peer['name']} отключён")
            try:
                await USER_BOT.send_message(int(peer["user"]), f"Конфиг {peer['name']} отключён администратором.")
            except Exception:
                pass
        state = load_state()
        peer = state["peers"].get(arg)
        await query.edit_message_text(
            f"{user_name(state, peer['user'])} · ID {peer['user']}\n{peer['name']} — {peer_status(peer)}",
            reply_markup=peer_buttons(arg))
        return

    async with lock:
        state = load_state()
        payment = state["payments"].get(arg)
        if not payment or payment["status"] != "pending":
            await query.answer("Эта оплата уже разобрана.")
            return
        payment["status"] = "approved" if action == "ok" else "rejected"
        payment["by"] = query.from_user.id
        save_state(state)
    await query.answer()

    uid = int(payment["user"])
    if action == "ok":
        result = "✅ Одобрено. " + await grant(uid, payment.get("target", NEW), DAYS)
    else:
        result = "❌ Отклонено"
        try:
            await USER_BOT.send_message(
                uid, "Оплата не подтверждена. Если это ошибка — пришлите скриншот ещё раз или напишите администратору.")
        except Exception:
            pass
    if query.message.photo:
        await query.edit_message_caption(f"{query.message.caption}\n\n{result}")
    else:
        await query.edit_message_text(f"{query.message.text}\n\n{result}")


@admin_only
async def admin_resend_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["await"] = "resend"
    await update.message.reply_text(
        "Напишите, что поменялось в конфиге — это увидят клиенты.\n"
        "Например: «теперь работают российские сайты с включённым VPN».")


async def resend_confirm(update, ctx):
    text = update.message.text.strip()
    ctx.user_data["resend_text"] = text
    state = load_state()
    peers = [p for p in state["peers"].values() if peer_active(p)]
    users = {p["user"] for p in peers}
    await update.message.reply_text(
        f"Клиенты получат:\n\n{resend_message(text)}\n\n…и кнопку на каждый свой конфиг — файл придёт по нажатию.\n\n"
        f"Разослать {len(peers)} активных конфигов {len(users)} клиентам? Сроки не меняются.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📤 Да, разослать", callback_data="resend:yes"),
            InlineKeyboardButton("Отмена", callback_data="resend:no"),
        ]]))


def resend_message(text):
    return f"🔄 Обновление конфига — {text}"


async def resend_all(query, text):
    state = load_state()
    by_user = {}
    for pub, p in state["peers"].items():
        if peer_active(p):
            by_user.setdefault(p["user"], []).append((pub, p))
    sent, failed = 0, []
    for uid, peers in by_user.items():
        # Не заваливаем чат файлами: кнопка на каждый конфиг, файлы — по нажатию.
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton(f"📥 Получить {p['name']}", callback_data=f"get:{pub}")]
                                        for pub, p in peers])
        try:
            await USER_BOT.send_message(
                int(uid), f"{resend_message(text)}\n\nНажмите на конфиг, чтобы получить обновлённый файл. "
                          "Старый в приложении можно удалить.", reply_markup=buttons)
            await asyncio.sleep(0.05)
            sent += 1
        except Exception:
            log.exception("не удалось разослать конфиги %s", uid)
            failed.append(user_name(state, uid))
    text = f"📤 Разослано клиентам: {sent} из {len(by_user)}."
    if failed:
        text += "\nНе удалось (не нажимали /start или заблокировали бота): " + ", ".join(failed)[:3000]
    await query.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)


@admin_only
async def admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    names = {pub: f"{user_name(state, p['user'])} / {p['name']}" for pub, p in state["peers"].items()}
    now = time.time()
    rows = []
    dumps = [run(["awg", "show", IFACE, "dump"])]
    if plain_routers():
        dumps.append(run(["awg", "show", ROUTER_IFACE, "dump"]))
    for line in [line for dump in dumps for line in dump.splitlines()[1:]]:
        f = line.split("\t")
        pub, handshake, rx, tx = f[0], int(f[4]), int(f[5]), int(f[6])
        ago = "никогда" if not handshake else f"{int(now - handshake) // 60} мин назад"
        rows.append(f"{names.get(pub, pub[:8])}: вход {ago}, ↓{rx / 2**20:.0f} МБ ↑{tx / 2**20:.0f} МБ")
    active = sum(1 for p in state["peers"].values() if peer_active(p) and not is_admin(p["user"]))
    approved = sum(1 for p in state["payments"].values() if p["status"] == "approved")
    revenue = sum(p.get("price", PRICE) for p in state["payments"].values() if p["status"] == "approved")
    await update.message.reply_text(
        f"Оплаченных конфигов сейчас: {active}\nОдобренных оплат всего: {approved} "
        f"(≈ {revenue} ₽)\n\n" + ("\n".join(rows) or "Подключений нет."),
        reply_markup=ADMIN_KEYBOARD)


@admin_only
async def admin_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2 or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /send ID текст")
        return
    await USER_BOT.send_message(int(ctx.args[0]), " ".join(ctx.args[1:]))
    await update.message.reply_text("Отправлено.")


@admin_only
async def admin_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: /broadcast текст")
        return
    text = update.message.text.split(maxsplit=1)[1]
    sent = 0
    for uid in load_state()["users"]:
        try:
            await USER_BOT.send_message(int(uid), text)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"Отправлено: {sent}.")


# ---------- окончание подписок ----------

async def check_expiry():
    now = time.time()
    notify = []
    async with lock:
        state = load_state()
        old_state = json.loads(json.dumps(state))
        expired = False
        for pub, p in state["peers"].items():
            if is_admin(p["user"]) or not p["until"]:
                continue
            left = p["until"] - now
            renew = f"Продлить: «{BTN_LIST}» → «🔄 Продлить»."
            if left <= 0 and p["warned"] < 3:
                p["warned"] = 3
                expired = True
                notify.append((p["user"], f"Срок конфига {p['name']} закончился, он отключён. {renew}"))
            elif 0 < left <= DAY and p["warned"] < 2:
                p["warned"] = 2
                notify.append((p["user"], f"Конфиг {p['name']} отключится {fmt_date(p['until'])} — "
                                          f"меньше чем через сутки. {renew}"))
            elif DAY < left <= 3 * DAY and p["warned"] < 1:
                p["warned"] = 1
                notify.append((p["user"], f"Конфиг {p['name']} работает до {fmt_date(p['until'])}. {renew}"))
        if expired:
            apply_or_rollback(state, old_state)
        save_state(state)
    for uid, text in notify:
        try:
            await USER_BOT.send_message(int(uid), text, reply_markup=KEYBOARD)
        except Exception:
            log.warning("не удалось уведомить %s", uid)


async def expiry_loop():
    while True:
        try:
            await check_expiry()
        except Exception as e:
            await report_error("Ошибка проверки сроков", e)
        await asyncio.sleep(600)


# ---------- запуск ----------

async def report_error(title, exc):
    """Пишет ошибку в лог и присылает её администраторам в админ-бот."""
    detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else repr(exc)
    # Последние строки трассировки — чтобы по сообщению было видно, где именно упало.
    frames = traceback.extract_tb(exc.__traceback__)[-3:]
    if frames:
        detail += "\n" + "\n".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}: {f.line}" for f in frames)
    log.error("%s: %s", title, detail, exc_info=exc)
    for admin in ADMINS:
        try:
            await ADMIN_BOT.send_message(admin, f"⚠️ {title}\n{detail[:3000]}")
        except Exception:
            pass


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    await report_error("Ошибка в боте", ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await ctx.bot.send_message(update.effective_chat.id, "Что-то пошло не так, попробуйте ещё раз.")
        except Exception:
            pass


def build_user_app():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_BUY]), buy))
    app.add_handler(MessageHandler(filters.Text([BTN_LIST]), list_configs))
    app.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_screenshot))
    # Старые кнопки из прошлой версии бота, оставшиеся у клиентов на экране.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))
    app.add_handler(CallbackQueryHandler(on_user_button))
    app.add_error_handler(on_error)
    return app


def build_admin_app():
    app = Application.builder().token(ADMIN_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], admin_start))
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(CommandHandler("send", admin_send))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(MessageHandler(filters.Text([ABTN_PAY]), admin_pending))
    app.add_handler(MessageHandler(filters.Text([ABTN_USERS]), admin_users))
    app.add_handler(MessageHandler(filters.Text([ABTN_GIVE]), admin_give_start))
    app.add_handler(MessageHandler(filters.Text([ABTN_STATS]), admin_stats))
    app.add_handler(MessageHandler(filters.Text([ABTN_RESEND]), admin_resend_ask))
    app.add_handler(CommandHandler("resend", admin_resend_ask))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))
    app.add_handler(CallbackQueryHandler(on_admin_button))
    app.add_error_handler(on_error)
    return app


async def amain():
    global USER_BOT, ADMIN_BOT
    user_app, admin_app = build_user_app(), build_admin_app()
    async with user_app, admin_app:
        USER_BOT, ADMIN_BOT = user_app.bot, admin_app.bot
        # Сразу применяем сроки к серверу: после простоя бота истёкшие конфиги отключатся.
        async with lock:
            write_server_conf(load_state())
        for app in (user_app, admin_app):
            await app.start()
            await app.updater.start_polling()
        tasks = [asyncio.create_task(expiry_loop()), asyncio.create_task(split_loop())]

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        for task in tasks:
            task.cancel()
        for app in (user_app, admin_app):
            await app.updater.stop()
            await app.stop()


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
