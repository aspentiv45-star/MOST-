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
import random
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
PRICE = int(os.environ.get("PRICE", "100"))
ROUTER_PRICE = int(os.environ.get("ROUTER_PRICE", "500"))
# Бесплатный пробный период для нового клиента (один раз на аккаунт); 0 — выключить.
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "3"))
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

# Профили маскировки под мобильных операторов. Jc/Jmin/Jmax и I1 задаёт только клиент
# (сервер эти пакеты-пустышки просто отбрасывает), поэтому у каждого конфига они свои
# и сервер менять не нужно. Значения — из замеров bivlked/amneziawg-installer:
# мобильные сети режут крупные пустышки и случайный I1, а пакет в форме ответа DNS пропускают.
OPERATORS = {
    "mts": "МТС", "beeline": "Билайн", "megafon": "МегаФон", "t2": "Т2 (Tele2)",
    "yota": "Yota", "tmob": "Т-Мобайл", "wifi": "Только Wi-Fi / домашний", "other": "Другой",
}
PROFILES = {
    "mts": {"Jc": 3, "Jmin": 40, "Jmax": 90, "I1": True},
    "beeline": {"Jc": 4, "Jmin": 40, "Jmax": 120, "I1": True},
    "megafon": {"Jc": 3, "Jmin": 40, "Jmax": 90, "I1": False},
    "t2": {"Jc": 3, "Jmin": 40, "Jmax": 90, "I1": True},
    "yota": {"Jc": 3, "Jmin": 30, "Jmax": 80, "I1": True},
    "tmob": {"Jc": 2, "Jmin": 20, "Jmax": 60, "I1": True},
    "wifi": {"Jc": 4, "Jmin": 40, "Jmax": 150, "I1": True},
    "other": {"Jc": 3, "Jmin": 40, "Jmax": 90, "I1": True},
}

BTN_BUY = "➕ Купить конфиг"
BTN_LIST = "📄 Мои конфиги"
BTN_HELP = "❓ Как подключить"
BTN_NEWS = "🆕 Что нового"
BTN_SUPPORT = "🆘 Нужна помощь"
BTN_CLEAR = "🧹 Почистить чат"
KEYBOARD = ReplyKeyboardMarkup([[BTN_BUY], [BTN_LIST, BTN_HELP], [BTN_NEWS, BTN_SUPPORT], [BTN_CLEAR]],
                               resize_keyboard=True)

CLEAR_BUTTONS = InlineKeyboardMarkup([[
    InlineKeyboardButton("🧹 Да, очистить", callback_data="clr:yes"),
    InlineKeyboardButton("Отмена", callback_data="clr:no"),
]])
CLEAR_QUESTION = ("Удалить сообщения в этом чате? Telegram разрешает ботам удалять только сообщения "
                  "за последние 48 часов, более старые останутся.")


async def clear_chat(bot, chat_id, last_id):
    """Удаляет сообщения чата от last_id назад, пачками по 100, пока удаляется хоть что-то."""
    for top in range(last_id, max(0, last_id - 1000), -100):
        ids = list(range(max(1, top - 99), top + 1))
        try:
            await bot.delete_messages(chat_id, ids)
        except Exception:
            # В пачке есть сообщения старше 48 часов — удаляем по одному, что получится.
            results = await asyncio.gather(*(bot.delete_message(chat_id, i) for i in ids), return_exceptions=True)
            if not any(r is True for r in results):
                break

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
        "Соединение замаскировано, провайдеры его не режут.\n\n"
        "❗ Другие роутеры (TP-Link, Asus, Xiaomi, MikroTik, роутеры провайдеров) умеют только обычный WireGuard, "
        "а его провайдеры режут — VPN на них работать не будет. Для них купите конфиг «Для себя» на каждое устройство.\n\n"
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
    "trouble": (
        "🆘 Не работает?\n\n"
        f"1. Проверьте срок: «{BTN_LIST}» — у конфига должен быть 🟢. Если ⚪️ — продлите.\n"
        "2. Выключите и включите VPN. Выключите другие VPN-приложения.\n"
        f"3. Скачайте конфиг заново («{BTN_LIST}» → «⬇️ Скачать»), удалите старый в приложении и импортируйте новый.\n"
        f"4. Не подключается или тормозит на мобильном интернете — «{BTN_LIST}» → «📶 Оператор» → выберите своего "
        "оператора (или «Другой»), удалите старый туннель и импортируйте новый файл. Не помогло — попробуйте "
        "другого оператора из списка: настройки у них разные.\n"
        "   Обновите приложение AmneziaVPN / AmneziaWG до последней версии.\n"
        "5. Один конфиг нельзя включать на двух устройствах одновременно — будут обрывы.\n"
        "6. Торренты через VPN заблокированы — это не ошибка.\n\n"
        "7. Роутер: если после включения VPN пропадает Wi-Fi или интернет — в «Разрешённых IP» стоит 0.0.0.0/0. "
        "Скачайте конфиг заново и импортируйте файл или вставьте список из сообщения бота.\n\n"
        "Не помогло — напишите администратору, приложите скриншот ошибки."
    ),
}

HELP_BUTTONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🛒 Что купить?", callback_data="help:what")],
    [InlineKeyboardButton("🍏 iPhone", callback_data="help:ios"),
     InlineKeyboardButton("🤖 Android", callback_data="help:android")],
    [InlineKeyboardButton("💻 Компьютер", callback_data="help:pc")],
    [InlineKeyboardButton("📡 Роутер (Keenetic, OpenWrt)", callback_data="help:router-awg")],
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


def fmt_day(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y")


async def add_news(text):
    """Записывает обновление в историю «Что нового». Возвращает дату записи."""
    now = time.time()
    async with lock:
        state = load_state()
        state["news"].append({"date": int(now), "text": text})
        save_state(state)
    return now


def news_message(ts, text):
    return f"📢 Обновление от {fmt_day(ts)}\n\n{text}"


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
    state.setdefault("tickets", {})
    state.setdefault("next_ticket", 1)
    state.setdefault("news", [])  # история обновлений для «Что нового»: [{"date": ts, "text": ...}]
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
        trial = " (пробный период)" if peer.get("trial") and not peer.get("paid") else ""
        return f"🟢 до {fmt_date(peer['until'])}{trial}"
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
    if kind not in ROUTER_KINDS:
        peer["op"] = state["users"].get(str(uid), {}).get("op")
    peer["ip"] = next_free_ip(state, server_params(peer_iface(peer)[1]))
    state["peers"][pub] = peer
    return pub, peer


def dns_i1(seed):
    """Маскирующий пакет I1 в форме ответа DNS (как в amneziawg-installer).

    Случайный I1 часть мобильных сетей отбрасывает вместе с рукопожатием,
    а пакет осмысленной формы пропускает. Для одного конфига он всегда одинаковый.
    """
    r = random.Random(seed)
    label = r.randint(20, 62)
    answers = r.randint(1, 2)
    head = f"85800001{answers:04x}00000000{label:02x}"
    tail = "0463646e730669636c6f756403636f6d0000010001"  # .cdns.icloud.com, тип A, класс IN
    ttl = r.choice(["0000003c", "0000012c", "00000384", "00000e10"])
    for _ in range(answers):
        o1 = r.randint(1, 221)
        o1 = {10: 222, 127: 223}.get(o1, o1)
        tail += f"c00c00010001{ttl}0004{o1:02x}{r.randint(0, 255):02x}{r.randint(0, 255):02x}{r.randint(1, 254):02x}"
    return f"<r 2><b 0x{head}><rc {label}><b 0x{tail}>"


def obfs_params(peer, params):
    """Параметры маскировки для клиента: серверные, с поправкой под оператора конфига."""
    params = {k: params[k] for k in OBFS_KEYS if k in params}
    prof = PROFILES.get(peer.get("op"))
    if prof:
        params.update(Jc=str(prof["Jc"]), Jmin=str(prof["Jmin"]), Jmax=str(prof["Jmax"]))
        for k in ("I1", "I2", "I3", "I4", "I5"):
            params.pop(k, None)
        if prof["I1"]:
            params["I1"] = dns_i1(peer["priv"])
    return params


def client_conf(peer, params, host):
    lines = [
        "[Interface]",
        f"PrivateKey = {peer['priv']}",
        f"Address = {peer['ip']}/32",
        f"DNS = {CLIENT_DNS}",
        "MTU = 1280",
    ]
    lines += [f"{k} = {v}" for k, v in obfs_params(peer, params).items()]
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {run(['awg', 'pubkey'], params['PrivateKey'] + chr(10))}",
        f"Endpoint = {host}:{params['ListenPort']}",
        f"AllowedIPs = {router_allowed_ips(host) if peer.get('kind') in ROUTER_KINDS else allowed_ips()}",
        "PersistentKeepalive = 25",
    ]
    return "\n".join(lines) + "\n"


def router_allowed_ips(host):
    """Всё IPv4, кроме адреса VPN-сервера и домашних сетей.

    Многие роутеры, получив 0.0.0.0/0, заворачивают в туннель и пакеты к самому
    VPN-серверу — туннель замыкается на себя и интернет пропадает у всего дома.
    IPv6 не включаем: роутеры часто его в этом поле не принимают.
    """
    try:
        server = [ipaddress.IPv4Network(f"{host}/32")]
    except ValueError:
        server = []
    local = [ipaddress.IPv4Network(n) for n in split.LOCAL]
    return ", ".join(map(str, split._complement(server + local)))


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
            f"Разрешённые IP-адреса (скопируйте целиком):\n{fields['AllowedIPs']}\n\n"
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
           else f"🎁 Попробуйте бесплатно {TRIAL_DAYS} дня: «{BTN_BUY}» → «Попробовать бесплатно»."
           if trial_available(state, update.effective_user.id)
           else f"Чтобы подключиться, нажмите «{BTN_BUY}»."),
        reply_markup=KEYBOARD)


async def ask_payment(message, ctx, target, state):
    ctx.user_data["pay_for"] = target
    leave_ticket(ctx)
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
    text, buttons = root_menu(state, update.effective_user.id)
    await update.message.reply_text(text, reply_markup=buttons)


def trial_available(state, uid):
    return TRIAL_DAYS > 0 and not state["users"].get(str(uid), {}).get("trial_used")


def root_menu(state, uid):
    """Первый экран покупки; новым клиентам — с кнопкой пробного периода."""
    text, buttons = BUY_MENU["root"]
    if not trial_available(state, uid):
        return text, buttons
    trial = [InlineKeyboardButton(f"🎁 Попробовать бесплатно {TRIAL_DAYS} дня", callback_data="trial:start")]
    return (f"{text}\n\n🎁 Новым клиентам — {TRIAL_DAYS} дня бесплатно на одно устройство. "
            "Понравится — продлите этот же конфиг, переустанавливать ничего не нужно.",
            InlineKeyboardMarkup([trial, *buttons.inline_keyboard]))


async def start_trial(query, ctx):
    user = query.from_user
    async with lock:
        state = load_state()
        u = get_user(state, user)
        if not is_approved(state, user.id):
            save_state(state)
            await query.edit_message_text(closed_text(state, user.id))
            return
        if not trial_available(state, user.id):
            await query.edit_message_text("Пробный период уже использован. Чтобы продолжить — оплатите конфиг.")
            return
        old_state = json.loads(json.dumps(state))
        pub, peer = create_peer(state, user.id, time.time() + TRIAL_DAYS * DAY)
        peer["trial"] = True
        peer["warned"] = 1  # напоминание «за 3 дня» для пробного не нужно
        u["trial_used"] = int(time.time())
        apply_or_rollback(state, old_state)
        save_state(state)

    await query.edit_message_reply_markup(reply_markup=None)
    await USER_BOT.send_message(
        user.id, f"🎁 Пробный период на {TRIAL_DAYS} дня: ваш конфиг {peer['name']} работает до "
                 f"{fmt_date(peer['until'])}.\n\nКак подключить — «{BTN_HELP}».\n"
                 f"Понравится — «{BTN_LIST}» → «🔄 Продлить» ({PRICE} ₽ за {DAYS} дней), "
                 "этот же конфиг продолжит работать.", reply_markup=KEYBOARD)
    await send_peer(user.id, peer)
    for admin in ADMINS:
        try:
            await ADMIN_BOT.send_message(admin, f"🎁 Пробный период: {u['name']}, ID {user.id}, "
                                                f"{peer['name']} до {fmt_date(peer['until'])}")
        except Exception:
            pass


def op_picker(prefix, suffix=""):
    """Выбор оператора; кнопка шлёт «prefix:код[suffix]»."""
    codes = list(OPERATORS)
    rows = [[InlineKeyboardButton(OPERATORS[c], callback_data=f"{prefix}:{c}{suffix}") for c in codes[i:i + 2]]
            for i in range(0, len(codes), 2)]
    return InlineKeyboardMarkup(rows)


OP_QUESTION = ("📶 Какой у вас мобильный оператор?\n\n"
               "Конфиг подстроится под его сеть: так меньше обрывов и быстрее подключение. "
               "Дома по Wi-Fi он тоже будет работать. Если мобильным интернетом не пользуетесь — "
               "выберите «Только Wi-Fi».")


async def remember_op(user, op):
    async with lock:
        state = load_state()
        get_user(state, user)["op"] = op
        save_state(state)
    return state


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
               "Подходят только роутеры с AmneziaWG: Keenetic (KeeneticOS 4.2 и новее) и OpenWrt.\n\n"
               "TP-Link, Asus, Xiaomi, MikroTik и роутеры провайдеров не подходят: они умеют только обычный "
               "WireGuard, а его провайдеры режут. Для них купите конфиг «Для устройства» на каждое устройство.\n\n"
               "Модель написана на наклейке снизу роутера.", InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ У меня Keenetic или OpenWrt", callback_data="menu:router-awg")],
        [InlineKeyboardButton("📱 Другой роутер — купить для устройства", callback_data="menu:device")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:root")],
    ])),
    "what": (GUIDES["what"], InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:root")]])),
    "ios": _buy_screen("ios", NEW, PRICE, "device"),
    "android": _buy_screen("android", NEW, PRICE, "device"),
    "pc": _buy_screen("pc", NEW, PRICE, "device"),
    "router-awg": _buy_screen("router-awg", NEW_ROUTER_AWG, ROUTER_PRICE, "router"),
}


async def on_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        file = await msg.photo[-1].get_file()
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file = await msg.document.get_file()
    else:
        await msg.reply_text("Пришлите картинку.")
        return
    data = bytes(await file.download_as_bytearray())

    # Скриншот проблемы в «Нужна помощь» — вложение к обращению, а не оплата.
    # Все фото альбома и всё, что клиент пришлёт дальше, попадают в то же обращение.
    if ctx.user_data.get("await") == "ticket":
        ctx.user_data.pop("await")
        ctx.user_data["album"] = msg.media_group_id
        await create_ticket(update, ctx, text=msg.caption or "(скриншот без описания)", photo=data)
        return
    if (ctx.user_data.get("await") or "").startswith("ticket_more:"):
        tid = ctx.user_data["await"].partition(":")[2]
        await ticket_followup(update, ctx, tid, msg.caption or "(скриншот)", photo=data)
        return

    state = load_state()
    if not is_approved(state, update.effective_user.id):
        await request_access(update.effective_user)
        await msg.reply_text(closed_text(state, update.effective_user.id))
        return
    target = ctx.user_data.pop("pay_for", None)
    if target is None:
        # Оплату принимаем только после выбора, за что она.
        await msg.reply_text(
            "Чтобы оплатить, сначала выберите, за что платите:\n"
            f"• новый конфиг — «{BTN_BUY}» → выберите устройство → «💳 Купить»;\n"
            f"• продление — «{BTN_LIST}» → нужный конфиг → «🔄 Продлить».\n"
            "После этого пришлите скриншот перевода.\n\n"
            f"Если это скриншот проблемы — нажмите «{BTN_SUPPORT}» и пришлите его там.",
            reply_markup=KEYBOARD)
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
        rows = [row, [InlineKeyboardButton("🏓 Пинг", callback_data=f"png:{pub}")]]
        if p.get("kind") not in ROUTER_KINDS:
            rows.append([InlineKeyboardButton(f"📶 Оператор: {OPERATORS.get(p.get('op'), 'не выбран')}",
                                              callback_data=f"opc:{pub}")])
        rows.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{pub}")])
        buttons = InlineKeyboardMarkup(rows)
        icon = "📡" if p.get("kind") in ROUTER_KINDS else "📱"
        await update.message.reply_text(f"{icon} {p['name']} — {peer_status(p)}", reply_markup=buttons)


async def news_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    news = load_state()["news"][-10:][::-1]
    if not news:
        await update.message.reply_text("Пока обновлений не было.", reply_markup=KEYBOARD)
        return
    text = "🆕 Что нового\n\n" + "\n\n".join(f"📅 {fmt_day(n['date'])}\n{n['text']}" for n in news)
    await update.message.reply_text(text[:4000], reply_markup=KEYBOARD)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=HELP_BUTTONS)


async def on_user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action, _, arg = query.data.partition(":")
    await query.answer()

    if action == "tku":
        kind, _, tid = arg.partition(":")
        await on_client_ticket_button(query, ctx, kind, tid)
        return

    if action == "menu":
        if arg == "root":
            text, buttons = root_menu(load_state(), uid)
        else:
            text, buttons = BUY_MENU.get(arg, BUY_MENU["root"])
        await query.edit_message_text(text, reply_markup=buttons)
        return

    if action == "clr":
        if arg == "yes":
            await clear_chat(ctx.bot, query.message.chat_id, query.message.message_id)
            await ctx.bot.send_message(query.message.chat_id, "🧹 Чат очищен. Конфиги всегда можно скачать снова "
                                       f"в «{BTN_LIST}».", reply_markup=KEYBOARD)
        else:
            await query.edit_message_text("Хорошо, ничего не удаляю.")
        return

    if action == "trial":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(OP_QUESTION, reply_markup=op_picker("opt"))
        return

    if action in ("opt", "opb") and arg in OPERATORS:
        state = await remember_op(query.from_user, arg)
        if action == "opt":
            await start_trial(query, ctx)
            return
        if not is_approved(state, uid):
            await query.edit_message_text(closed_text(state, uid))
            return
        await query.edit_message_text(f"📶 Оператор: {OPERATORS[arg]}.")
        await ask_payment(query.message, ctx, NEW, state)
        return

    if action == "ops":
        op, _, pub = arg.partition(":")
        async with lock:
            state = load_state()
            peer = state["peers"].get(pub)
            if not peer or peer["user"] != str(uid) or op not in OPERATORS:
                await query.edit_message_text("Конфиг не найден.")
                return
            peer["op"] = op
            get_user(state, query.from_user)["op"] = op
            save_state(state)
        await query.edit_message_text(f"📶 {peer['name']}: оператор {OPERATORS[op]}.")
        if not peer_active(peer):
            await query.message.reply_text("Срок конфига закончился: продлите его, и он заработает с новыми настройками.")
            return
        await query.message.reply_text("Вот обновлённый файл. Удалите старый туннель в приложении и добавьте этот "
                                       "(ключи те же, платить заново не нужно).")
        await send_peer(query.message.chat_id, peer)
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
        if arg == NEW:
            await query.message.reply_text(OP_QUESTION, reply_markup=op_picker("opb"))
            return
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
    elif action == "png":
        await query.message.reply_text(await check_peer(arg, peer))
    elif action == "opc":
        await query.message.reply_text(f"{peer['name']}: {OP_QUESTION}", reply_markup=op_picker("ops", f":{arg}"))
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
ABTN_NEWS = "📢 Оповестить об обновлении"
ABTN_TICKETS = "📨 Жалобы"
ADMIN_KEYBOARD = ReplyKeyboardMarkup([[ABTN_PAY, ABTN_TICKETS], [ABTN_USERS, ABTN_GIVE], [ABTN_STATS, ABTN_RESEND],
                                      [ABTN_NEWS, BTN_CLEAR]],
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
    ], [InlineKeyboardButton("🏓 Пинг", callback_data=f"png:{pub}")]])


async def check_peer(pub, peer):
    """Проверка связи с конфигом: когда было рукопожатие, трафик и пинг от сервера до устройства."""
    iface = peer_iface(peer)[0]
    handshake = rx = tx = 0
    for line in run(["awg", "show", iface, "dump"]).splitlines()[1:]:
        f = line.split("\t")
        if f[0] == pub:
            handshake, rx, tx = int(f[4]), int(f[5]), int(f[6])
    head = f"🏓 {peer['name']}\n"
    if not peer_active(peer):
        return head + "⚪️ Срок закончился — конфиг отключён на сервере."
    # Рукопожатие повторяется каждые 2 минуты, пока туннель включён.
    if not handshake or time.time() - handshake > 180:
        when = "ни разу" if not handshake else f"последний раз {fmt_date(handshake)}"
        return head + (f"🔴 Сейчас не подключён (подключался {when}).\n\n"
                       "Включите VPN в приложении и нажмите «🏓 Пинг» ещё раз.")
    proc = await asyncio.create_subprocess_exec(
        "ping", "-c", "5", "-i", "0.3", "-W", "2", "-q", peer["ip"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out = (await proc.communicate())[0].decode()
    loss = next((w for w in out.replace(",", " ").split() if w.endswith("%")), "?")
    rtt = next((line.split("=")[1].split("/")[1] for line in out.splitlines() if "min/avg" in line), None)
    text = head + f"🟢 Подключён, трафик: ↓{tx / 2**20:.0f} МБ ↑{rx / 2**20:.0f} МБ\n"
    if rtt is None:
        return text + ("Пинг: устройство не ответило. Так бывает на iPhone и при экономии батареи — "
                       "если сайты открываются, всё в порядке.")
    ms = round(float(rtt))
    grade = "отлично" if ms < 80 else "нормально" if ms < 150 else "высокий — возможны лаги в играх и звонках"
    return text + f"Пинг до сервера: {ms} мс ({grade}), потери: {loss}"


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
            if paid:
                peer["paid"] = True  # после оплаты пометка «пробный период» уходит
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
        f"{ABTN_RESEND} — отправить всем клиентам актуальные файлы их конфигов\n"
        f"{ABTN_NEWS} — написать всем клиентам, что нового (попадёт в «{BTN_NEWS}»)\n"
        f"{ABTN_TICKETS} — обращения клиентов: ответить, дать конфиг, вернуть деньги, отклонить\n\n"
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
            f"{user_name(state, p['user'])} · ID {p['user']}\n{p['name']} — {peer_status(p)}"
            + (f"\n📶 {OPERATORS[p['op']]}" if p.get("op") in OPERATORS else ""),
            reply_markup=None if is_admin(p["user"]) else peer_buttons(pub))
    if len(peers) > 40:
        await update.message.reply_text(f"Показаны первые 40 конфигов из {len(peers)}.")


@admin_only
async def admin_give_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["await"] = "give"
    await update.message.reply_text(
        f"Пришлите ID клиента и, через пробел, число дней (по умолчанию {DAYS}).\n"
        f"Например: 123456789 {DAYS}\n"
        f"Для роутера (Keenetic, OpenWrt) добавьте слово «роутер»: "
        f"123456789 {DAYS} роутер\n\n"
        "Клиент получит новый конфиг бесплатно. ID видно в его оплатах и в «👥 Клиенты». "
        "Клиент должен хотя бы раз нажать /start в клиентском боте.")


@admin_only
async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    waiting = ctx.user_data.pop("await", None)
    if waiting and waiting.startswith(("tk_reply:", "tk_give:", "tk_spam:")):
        await admin_ticket_text(update, ctx, waiting)
        return
    if waiting == "news":
        ctx.user_data["news_text"] = update.message.text.strip()
        users = len(load_state()["users"])
        await update.message.reply_text(
            f"Клиенты получат:\n\n{news_message(time.time(), ctx.user_data['news_text'])}\n\n"
            f"Разослать {users} клиентам? Запись появится в «{BTN_NEWS}».",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📢 Да, разослать", callback_data="news:yes"),
                InlineKeyboardButton("Отмена", callback_data="news:no"),
            ]]))
        return
    if waiting == "resend":
        await resend_confirm(update, ctx)
        return
    if waiting != "give":
        await update.message.reply_text("Выберите действие на кнопках ниже.", reply_markup=ADMIN_KEYBOARD)
        return
    words = update.message.text.lower().split()
    AWG_WORDS = ("amnez", "амнез", "keenetic", "кинет", "awg", "openwrt")
    awg = any(w.startswith(AWG_WORDS) for w in words)
    # Обычный WireGuard для роутеров больше не продаётся: «роутер» = роутер AmneziaWG.
    awg = awg or any(w.startswith(("роут", "router")) for w in words)
    parts = [w for w in words if w.isdigit()]
    if not parts or not parts[0].isdigit() or (len(parts) > 1 and not parts[1].isdigit()):
        ctx.user_data["await"] = "give"
        await update.message.reply_text(f"Нужно число — ID клиента. Например: 123456789 {DAYS}")
        return
    uid, days = int(parts[0]), int(parts[1]) if len(parts) > 1 else DAYS
    result = await grant(uid, NEW_ROUTER_AWG if awg else NEW, days, paid=False)
    await update.message.reply_text(f"Готово: выдан конфиг.\n{result}", reply_markup=ADMIN_KEYBOARD)


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return
    action, _, arg = query.data.partition(":")

    if action == "clr":
        if arg == "yes":
            await clear_chat(ctx.bot, query.message.chat_id, query.message.message_id)
            await ctx.bot.send_message(query.message.chat_id, "🧹 Чат очищен.", reply_markup=ADMIN_KEYBOARD)
        else:
            await query.edit_message_text("Хорошо, ничего не удаляю.")
        return

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

    if action == "tk":
        kind, _, tid = arg.partition(":")
        await on_ticket_button(query, ctx, kind, tid)
        return

    if action == "news":
        await query.answer()
        text = ctx.user_data.pop("news_text", None)
        if arg != "yes":
            await query.edit_message_text("Оповещение отменено.")
            return
        if not text:
            await query.edit_message_text(f"Текст потерялся — нажмите «{ABTN_NEWS}» ещё раз.")
            return
        await query.edit_message_text("📢 Рассылаю…")
        ts = await add_news(text)
        sent = 0
        users = list(load_state()["users"])
        for uid in users:
            try:
                await USER_BOT.send_message(int(uid), news_message(ts, text), reply_markup=KEYBOARD)
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await query.message.reply_text(f"📢 Оповещено клиентов: {sent} из {len(users)}.", reply_markup=ADMIN_KEYBOARD)
        return

    if action == "resend":
        await query.answer()
        text = ctx.user_data.pop("resend_text", None)
        if arg == "yes" and text:
            await query.edit_message_text("📤 Рассылаю…")
            await add_news(f"Обновление конфига — {text}")
            await resend_all(query, text)
        elif arg == "yes":
            await query.edit_message_text(f"Текст рассылки потерялся — нажмите «{ABTN_RESEND}» ещё раз.")
        else:
            await query.edit_message_text("Рассылка отменена.")
        return

    if action == "png":
        peer = load_state()["peers"].get(arg)
        await query.answer()
        if peer:
            await query.message.reply_text(f"{user_name(load_state(), peer['user'])}\n" + await check_peer(arg, peer))
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
async def admin_news_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["await"] = "news"
    await update.message.reply_text(
        f"Напишите, что нового — это получат все клиенты, с сегодняшней датой ({fmt_day(time.time())}).\n"
        "Например: «Добавили подключение роутеров Keenetic».")


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


# ---------- обращения в поддержку ----------
# Статусы: open — ждёт решения; refund_wait — ждём реквизиты клиента;
# refund_ready — реквизиты есть, ждём перевода; closed — закрыто.

TICKET_STATUS = {
    "open": "🟡 открыто — ждёт вашего ответа",
    "answered": "💬 вы ответили, ждём клиента",
    "refund_wait": "💸 возврат: ждём реквизиты клиента",
    "refund_ready": "💸 возврат: реквизиты получены, нужно перевести",
    "closed": "✅ закрыто",
}


def ticket_buttons(tid, status):
    if status in ("open", "answered"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Ответить", callback_data=f"tk:reply:{tid}"),
             InlineKeyboardButton("🎁 Дать конфиг", callback_data=f"tk:give:{tid}")],
            [InlineKeyboardButton("💸 Вернуть деньги", callback_data=f"tk:refund:{tid}"),
             InlineKeyboardButton("❌ Отклонить", callback_data=f"tk:reject:{tid}")],
            [InlineKeyboardButton("🚫 Спам", callback_data=f"tk:spam:{tid}")],
        ])
    if status == "refund_ready":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Деньги отправлены", callback_data=f"tk:paid:{tid}")],
            [InlineKeyboardButton("❌ Отклонить", callback_data=f"tk:reject:{tid}")],
        ])
    if status == "refund_wait":
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отклонить", callback_data=f"tk:reject:{tid}")]])
    return None


def ticket_dialog(t):
    """Переписка по обращению: первое сообщение клиента, ответы и дополнительные вопросы."""
    msgs = t.get("messages") or [{"from": "client", "text": t["text"]}]
    who = {"client": "👤 Клиент", "admin": "💬 Вы"}
    return "\n\n".join(f"{who[m['from']]}: {m['text']}" for m in msgs[-8:])


def leave_ticket(ctx):
    """Клиент вышел из переписки по обращению: следующие сообщения в него не попадут."""
    if (ctx.user_data.get("await") or "").startswith("ticket"):
        ctx.user_data.pop("await")
    ctx.user_data.pop("album", None)


async def leave_ticket_on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    leave_ticket(ctx)


def open_ticket_buttons(tid):
    """Кнопки под сообщением «обращение принято»: клиент ещё пишет, ответа пока нет."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Вопрос решён", callback_data=f"tku:ok:{tid}"),
         InlineKeyboardButton("🏠 В меню", callback_data=f"tku:menu:{tid}")],
    ])


def client_ticket_buttons(tid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Вопрос решён", callback_data=f"tku:ok:{tid}")],
        [InlineKeyboardButton("❓ Задать ещё вопрос", callback_data=f"tku:more:{tid}"),
         InlineKeyboardButton("🏠 В меню", callback_data=f"tku:menu:{tid}")],
    ])


def ticket_text(state, tid):
    t = state["tickets"][tid]
    peers = user_peers(state, t["user"])
    configs = "\n".join(f"  {p['name']} — {peer_status(p)}" for p in peers.values()) or "  нет"
    text = (f"📨 Обращение №{tid} от {fmt_date(t['created'])}\n"
            f"{user_name(state, t['user'])} · ID {t['user']}\n"
            f"Статус: {TICKET_STATUS[t['status']]}\n\n"
            f"{ticket_dialog(t)}\n\nКонфиги клиента:\n{configs}")
    if t.get("requisites"):
        text += f"\n\n💳 Реквизиты для возврата:\n{t['requisites']}"
    if t.get("result"):
        text += f"\n\nИтог: {t['result']}"
    return text[:4000]


async def notify_admins(text, buttons=None):
    for admin in ADMINS:
        try:
            await ADMIN_BOT.send_message(admin, text, reply_markup=buttons)
        except Exception:
            log.exception("не удалось написать админу %s", admin)


def fmt_left(seconds):
    """Сколько осталось, по-человечески: «7 мин», «2 ч 5 мин», «1 д 3 ч»."""
    minutes = max(1, int((seconds + 59) // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} д" if days else "", f"{hours} ч" if hours else "", f"{mins} мин" if mins and not days else ""]
    return " ".join(p for p in parts if p) or "1 мин"


def support_ban_text(state, uid):
    """Текст запрета на обращения за спам или None, если запрета нет."""
    until = state["users"].get(str(uid), {}).get("support_ban_until", 0)
    if until <= time.time():
        return None
    return (f"🚫 Вы не можете отправить обращение, пока не пройдёт ещё {fmt_left(until - time.time())} "
            f"(до {fmt_date(until)}): вы заблокированы за спам.")


async def support_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    banned = support_ban_text(load_state(), update.effective_user.id)
    if banned:
        await update.message.reply_text(banned, reply_markup=KEYBOARD)
        return
    ctx.user_data.pop("pay_for", None)
    ctx.user_data.pop("album", None)
    ctx.user_data["await"] = "ticket"
    await update.message.reply_text(
        "🆘 Опишите проблему одним сообщением: что не работает, на каком устройстве, "
        "через Wi-Fi или мобильный интернет. Можно прислать скриншот ошибки с подписью. "
        "Администратор ответит здесь.\n\n"
        "Если передумали — напишите «отмена».")


async def create_ticket(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text=None, photo=None):
    banned = support_ban_text(load_state(), update.effective_user.id)
    if banned:
        await update.message.reply_text(banned, reply_markup=KEYBOARD)
        return
    text = (text if text is not None else update.message.text).strip()
    async with lock:
        state = load_state()
        get_user(state, update.effective_user)
        tid = str(state["next_ticket"])
        state["next_ticket"] += 1
        state["tickets"][tid] = {"user": str(update.effective_user.id), "text": text[:2000],
                                 "status": "open", "created": int(time.time()),
                                 "messages": [{"from": "client", "text": text[:2000]}]}
        save_state(state)
    ctx.user_data["await"] = f"ticket_more:{tid}"
    await update.message.reply_text(
        f"Обращение №{tid} принято. Если нужно, допишите сообщения или пришлите ещё скриншоты — "
        "всё попадёт в это обращение. Администратор ответит здесь, в боте.",
        reply_markup=open_ticket_buttons(tid))
    if photo:
        # file_id одного бота не годится для другого — пересылаем картинку и запоминаем file_id админ-бота.
        photo_id = None
        for admin in ADMINS:
            try:
                sent = await ADMIN_BOT.send_photo(admin, photo=io.BytesIO(photo), caption=f"📎 Скриншот к обращению №{tid}")
                photo_id = photo_id or sent.photo[-1].file_id
            except Exception:
                log.exception("не удалось отправить скриншот админу %s", admin)
        if photo_id:
            async with lock:
                state = load_state()
                state["tickets"][tid]["photo"] = photo_id
                save_state(state)
    await notify_admins(f"🆘 Новое обращение\n\n{ticket_text(state, tid)}", ticket_buttons(tid, "open"))


async def save_requisites(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tid):
    async with lock:
        state = load_state()
        state["users"][str(update.effective_user.id)].pop("await_requisites", None)
        t = state["tickets"].get(tid)
        if not t or t["status"] != "refund_wait":
            save_state(state)
            await update.message.reply_text("Это обращение уже закрыто.", reply_markup=KEYBOARD)
            return
        t["requisites"] = update.message.text.strip()[:500]
        t["status"] = "refund_ready"
        save_state(state)
    await update.message.reply_text(f"Реквизиты по обращению №{tid} получены. Администратор переведёт деньги "
                                    "и пришлёт подтверждение.", reply_markup=KEYBOARD)
    await notify_admins(f"💳 Реквизиты для возврата\n\n{ticket_text(state, tid)}", ticket_buttons(tid, "refund_ready"))


async def ticket_followup(update, ctx, tid, text, photo=None):
    """Дополнительное сообщение клиента: обращение снова ждёт ответа администратора."""
    banned = support_ban_text(load_state(), update.effective_user.id)
    if banned:
        leave_ticket(ctx)
        await update.message.reply_text(banned, reply_markup=KEYBOARD)
        return
    group = update.message.media_group_id
    if group and group == ctx.user_data.get("album"):
        # Следующее фото того же альбома: просто пересылаем админу, без лишних сообщений.
        for admin in ADMINS:
            try:
                await ADMIN_BOT.send_photo(admin, photo=io.BytesIO(photo), caption=f"📎 Скриншот к обращению №{tid}")
            except Exception:
                log.exception("не удалось отправить скриншот админу %s", admin)
        return
    ctx.user_data["album"] = group
    async with lock:
        state = load_state()
        t = state["tickets"].get(tid)
        if not t or t["status"] == "closed":
            save_state(state)
            leave_ticket(ctx)
            await update.message.reply_text("Это обращение уже закрыто. Чтобы задать новый вопрос — "
                                            f"«{BTN_SUPPORT}».", reply_markup=KEYBOARD)
            return
        t.setdefault("messages", [{"from": "client", "text": t["text"]}]).append({"from": "client", "text": text[:2000]})
        if t["status"] == "answered":
            t["status"] = "open"
        save_state(state)
    await update.message.reply_text(f"Добавлено к обращению №{tid}. Администратор ответит здесь.",
                                    reply_markup=open_ticket_buttons(tid))
    if photo:
        for admin in ADMINS:
            try:
                await ADMIN_BOT.send_photo(admin, photo=io.BytesIO(photo), caption=f"📎 Скриншот к обращению №{tid}")
            except Exception:
                log.exception("не удалось отправить скриншот админу %s", admin)
    await notify_admins(f"❓ Клиент задал ещё вопрос\n\n{ticket_text(state, tid)}",
                        ticket_buttons(tid, state["tickets"][tid]["status"]))


async def on_client_ticket_button(query, ctx, action, tid):
    state = load_state()
    t = state["tickets"].get(tid)
    if not t or t["user"] != str(query.from_user.id):
        await query.edit_message_reply_markup(reply_markup=None)
        return
    await query.edit_message_reply_markup(reply_markup=None)
    if action in ("ok", "menu"):
        leave_ticket(ctx)
    if action == "ok":
        if t["status"] != "closed":
            await close_ticket(tid, "решено (подтвердил клиент)", f"✅ Обращение №{tid} закрыто. Спасибо!")
            await notify_admins(f"✅ Клиент отметил обращение №{tid} решённым.")
    elif action == "more":
        if t["status"] == "closed":
            await query.message.reply_text(f"Обращение закрыто. Чтобы задать новый вопрос — «{BTN_SUPPORT}».",
                                           reply_markup=KEYBOARD)
            return
        ctx.user_data["await"] = f"ticket_more:{tid}"
        ctx.user_data.pop("album", None)
        await query.message.reply_text("Напишите ваш вопрос (можно несколько сообщений и скриншотов). "
                                       "Передумали — напишите «отмена».")
    else:
        await query.message.reply_text("Главное меню 👇", reply_markup=KEYBOARD)


async def user_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Свободный текст клиента: обращение, реквизиты для возврата или просто возврат в меню."""
    waiting = ctx.user_data.get("await") or ""
    if waiting.startswith("ticket_more:"):
        if update.message.text.strip().lower() in ("отмена", "отменить", "cancel"):
            leave_ticket(ctx)
            await update.message.reply_text("Хорошо, вы вышли из обращения. Главное меню 👇", reply_markup=KEYBOARD)
            return
        await ticket_followup(update, ctx, waiting.partition(":")[2], update.message.text.strip())
        return
    if ctx.user_data.pop("await", None) == "ticket":
        if update.message.text.strip().lower() in ("отмена", "отменить", "cancel"):
            await update.message.reply_text("Хорошо, обращение не отправлено.", reply_markup=KEYBOARD)
            return
        await create_ticket(update, ctx)
        return
    state = load_state()
    tid = state["users"].get(str(update.effective_user.id), {}).get("await_requisites")
    if tid:
        await save_requisites(update, ctx, tid)
        return
    await start(update, ctx)


@admin_only
async def admin_tickets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    # Только то, что ждёт действия администратора: отвеченные и ожидающие реквизитов — не здесь.
    active = [tid for tid, t in state["tickets"].items() if t["status"] in ("open", "refund_ready")]
    if not active:
        await update.message.reply_text("Открытых обращений нет.", reply_markup=ADMIN_KEYBOARD)
        return
    await update.message.reply_text(f"Открытых обращений: {len(active)}.", reply_markup=ADMIN_KEYBOARD)
    for tid in active[-20:]:
        if state["tickets"][tid].get("photo"):
            await update.message.reply_photo(state["tickets"][tid]["photo"], caption=f"📎 Скриншот к обращению №{tid}")
        await update.message.reply_text(ticket_text(state, tid),
                                        reply_markup=ticket_buttons(tid, state["tickets"][tid]["status"]))


async def close_ticket(tid, result, client_text):
    async with lock:
        state = load_state()
        t = state["tickets"][tid]
        t["status"] = "closed"
        t["result"] = result
        t["closed"] = int(time.time())
        state["users"].get(t["user"], {}).pop("await_requisites", None)
        save_state(state)
    try:
        await USER_BOT.send_message(int(t["user"]), client_text, reply_markup=KEYBOARD)
    except Exception:
        pass
    return state


async def ban_for_spam(tid, minutes):
    """Закрывает обращение как спам и запрещает клиенту новые обращения на minutes минут."""
    until = time.time() + minutes * 60
    async with lock:
        state = load_state()
        uid = state["tickets"][tid]["user"]
        state["users"].setdefault(uid, {"name": uid})["support_ban_until"] = int(until)
        save_state(state)
    return await close_ticket(
        tid, f"спам, обращения запрещены на {fmt_left(minutes * 60)}",
        f"🚫 Вы заблокированы за спам: отправлять обращения нельзя {fmt_left(minutes * 60)} "
        f"(до {fmt_date(until)}).")


async def on_ticket_button(query, ctx, action, tid):
    minutes = None
    if action == "spamset":
        tid, _, minutes = tid.partition(":")
    state = load_state()
    t = state["tickets"].get(tid)
    if not t or t["status"] == "closed":
        await query.answer("Обращение уже закрыто.")
        return
    await query.answer()
    uid = int(t["user"])

    if action == "reply":
        ctx.user_data["await"] = f"tk_reply:{tid}"
        await query.message.reply_text(f"Напишите ответ клиенту по обращению №{tid}.")
    elif action == "give":
        ctx.user_data["await"] = f"tk_give:{tid}"
        await query.message.reply_text(f"На сколько дней выдать новый конфиг по обращению №{tid}? Пришлите число, "
                                       f"например {DAYS}.")
    elif action == "refund":
        async with lock:
            state = load_state()
            state["tickets"][tid]["status"] = "refund_wait"
            state["users"].setdefault(str(uid), {"name": str(uid)})["await_requisites"] = tid
            save_state(state)
        try:
            await USER_BOT.send_message(
                uid, f"💸 По обращению №{tid} администратор оформляет возврат денег.\n\n"
                     "Пришлите сюда одним сообщением реквизиты: номер телефона или карты, банк и имя получателя.",
                reply_markup=KEYBOARD)
        except Exception:
            pass
        await query.edit_message_text(ticket_text(state, tid), reply_markup=ticket_buttons(tid, "refund_wait"))
    elif action == "paid":
        state = await close_ticket(tid, "деньги возвращены",
                                   f"✅ По обращению №{tid} деньги отправлены на ваши реквизиты.")
        await query.edit_message_text(ticket_text(state, tid))
    elif action == "reject":
        state = await close_ticket(tid, "отклонено", f"Обращение №{tid} закрыто администратором.")
        await query.edit_message_text(ticket_text(state, tid))
    elif action == "spam":
        await query.message.reply_text(
            f"🚫 На сколько запретить клиенту обращения? (обращение №{tid} закроется)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("10 минут", callback_data=f"tk:spamset:{tid}:10"),
                 InlineKeyboardButton("1 час", callback_data=f"tk:spamset:{tid}:60"),
                 InlineKeyboardButton("1 день", callback_data=f"tk:spamset:{tid}:1440")],
                [InlineKeyboardButton("✏️ Своё число минут", callback_data=f"tk:spamask:{tid}")],
            ]))
    elif action == "spamask":
        ctx.user_data["await"] = f"tk_spam:{tid}"
        await query.message.reply_text(f"Пришлите, на сколько минут запретить обращения (обращение №{tid}).")
    elif action == "spamset":
        state = await ban_for_spam(tid, int(minutes))
        await query.edit_message_text(ticket_text(state, tid))


async def admin_ticket_text(update, ctx, waiting):
    kind, _, tid = waiting.partition(":")
    state = load_state()
    t = state["tickets"].get(tid)
    if not t or t["status"] == "closed":
        await update.message.reply_text("Обращение уже закрыто.", reply_markup=ADMIN_KEYBOARD)
        return
    text = update.message.text.strip()
    if kind == "tk_reply":
        try:
            await USER_BOT.send_message(int(t["user"]), f"💬 Ответ по обращению №{tid}:\n\n{text}",
                                        reply_markup=client_ticket_buttons(tid))
        except Exception:
            await update.message.reply_text("Клиенту написать не удалось.", reply_markup=ADMIN_KEYBOARD)
            return
        async with lock:
            state = load_state()
            t = state["tickets"][tid]
            t.setdefault("messages", [{"from": "client", "text": t["text"]}]).append({"from": "admin", "text": text[:2000]})
            if t["status"] == "open":
                t["status"] = "answered"
            save_state(state)
        await update.message.reply_text(
            "Ответ отправлен. Обращение убрано из «Жалоб»: если клиент задаст ещё вопрос, оно вернётся, "
            "а вам придёт уведомление.", reply_markup=ADMIN_KEYBOARD)
        return
    if not text.isdigit() or int(text) <= 0:
        ctx.user_data["await"] = waiting
        await update.message.reply_text("Нужно число минут, например 30." if kind == "tk_spam"
                                        else f"Нужно число дней, например {DAYS}.")
        return
    if kind == "tk_spam":
        state = await ban_for_spam(tid, int(text))
        await update.message.reply_text(ticket_text(state, tid), reply_markup=ADMIN_KEYBOARD)
        return
    result = await grant(int(t["user"]), NEW, int(text), paid=False)
    state = await close_ticket(tid, f"выдан конфиг на {text} дн.",
                               f"✅ Обращение №{tid} решено: вам выдан новый конфиг на {text} дн. (файл выше).")
    await update.message.reply_text(f"{ticket_text(state, tid)}\n\n{result}", reply_markup=ADMIN_KEYBOARD)


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


async def ask_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CLEAR_QUESTION, reply_markup=CLEAR_BUTTONS)


def build_user_app():
    app = Application.builder().token(TOKEN).build()
    # Кнопки меню и команды выводят клиента из переписки по обращению.
    app.add_handler(MessageHandler(filters.Text([BTN_BUY, BTN_LIST, BTN_HELP, BTN_NEWS, BTN_SUPPORT, BTN_CLEAR])
                                   | filters.COMMAND,
                                   leave_ticket_on_menu), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_BUY]), buy))
    app.add_handler(MessageHandler(filters.Text([BTN_LIST]), list_configs))
    app.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_NEWS]), news_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_screenshot))
    # Старые кнопки из прошлой версии бота, оставшиеся у клиентов на экране.
    app.add_handler(MessageHandler(filters.Text([BTN_SUPPORT]), support_start))
    app.add_handler(MessageHandler(filters.Text([BTN_CLEAR]), ask_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, user_text))
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
    app.add_handler(MessageHandler(filters.Text([ABTN_NEWS]), admin_news_ask))
    app.add_handler(MessageHandler(filters.Text([ABTN_TICKETS]), admin_tickets))
    app.add_handler(MessageHandler(filters.Text([BTN_CLEAR]), admin_only(ask_clear)))
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
