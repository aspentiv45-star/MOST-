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
import base64
import html
import io
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import traceback
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo

import qrcode
import split
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
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
# DNS на VPN-сервере с блокировкой рекламы (ставит adblock.sh); пусто — блокировки нет.
ADBLOCK_DNS = os.environ.get("ADBLOCK_DNS", "")
MAX_PEERS = int(os.environ.get("MAX_PEERS", "5"))
PRICE = int(os.environ.get("PRICE", "100"))
ROUTER_PRICE = int(os.environ.get("ROUTER_PRICE", "500"))
# Бесплатный пробный период для нового клиента (один раз на аккаунт); 0 — выключить.
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "3"))
DAYS = int(os.environ.get("DAYS", "30"))
PRICE3 = int(os.environ.get("PRICE3", "249"))       # 3 месяца для устройства (скидка)
TOPUP_MIN = int(os.environ.get("TOPUP_MIN", "150"))  # минимальное пополнение баланса, ₽
REF_NEED = int(os.environ.get("REF_NEED", "3"))      # сколько друзей должны оплатить, чтобы получить подарок
REF_DAYS = int(os.environ.get("REF_DAYS", "30"))     # подарок за каждых REF_NEED друзей, дней
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

# Режим «только заблокированное»: через VPN идут лишь эти сервисы, всё остальное (российские
# сайты, банки) — напрямую с устройства. Около 100 сетей: больший список приложения Amnezia
# обрабатывают плохо (зависания на Windows, обрыв правил на Linux, тормоза интерфейса).
# Режимы конфига. full — весь трафик через вход в России; smart — только заблокированное;
# game — только игры и напрямую на основной сервер, без входа в России: меньше пинг.
MODES = {"full": "🛡 Обход белых списков", "smart": "⚡️ Основной", "game": "🎮 Игровой: телефон",
         "pcgame": "🎮 Игровой: ПК"}
# pcgame — весь трафик через VPN (один IP: античиты Easy Anti-Cheat и др. не рвут соединение,
# Discord и Telegram работают), но напрямую на основной сервер, минуя вход в России: пинг ниже.
# Порт основного сервера для игрового режима: 443 принимает alt-port.sh (как QUIC его режут реже).
GAME_PORT = os.environ.get("GAME_PORT", "443")
# Сети Cloudflare (официальный список cloudflare.com/ips) — только для игрового режима: через него
# работают серверы многих игр. Вместе с ними через VPN пойдут и сайты на Cloudflare.
CLOUDFLARE_NETS = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
)
GAME_NETS = ("5.180.72.0/22", "2a09:fa40::/29")
# Чтобы во время игры ответить в Telegram, позвать команду в Discord или глянуть гайд на YouTube.
# Плюс DNS через VPN: иначе российский DNS отдаст YouTube «российский» адрес, и видео пойдёт мимо VPN.
GAME_EXTRA_NETS = (
    # Telegram
    "91.105.192.0/23", "91.108.4.0/22", "91.108.8.0/21", "91.108.16.0/21", "91.108.56.0/22",
    "95.161.64.0/20", "149.154.160.0/20", "185.76.151.0/24", "2001:67c:4e8::/48", "2001:b28:f23c::/47",
    "2001:b28:f23f::/48",
    # YouTube и Google
    "64.233.160.0/19", "66.102.0.0/20", "74.125.0.0/16", "108.177.0.0/17", "142.250.0.0/15",
    "172.217.0.0/16", "172.253.0.0/16", "173.194.0.0/16", "192.178.0.0/15", "209.85.128.0/17",
    "216.58.192.0/19", "216.239.32.0/19", "2a00:1450::/32", "2001:4860::/32", "2404:6800::/32",
    # Discord
    "66.22.192.0/18", "138.128.136.0/21", "162.159.128.0/19", "162.245.208.0/21",
    # DNS
    "1.1.1.1/32", "1.0.0.1/32",
)
# Режимы, которые подключаются напрямую к основному серверу (UDP GAME_PORT), минуя вход в России:
# меньше пинг. «Только заблокированное» тоже — выключить: SMART_DIRECT=0 в bot.env.
# «Только заблокированное» = весь трафик через VPN, но подключение сразу к основному серверу (без входа
# в России). Российские адреса основной сервер сам отправляет через российский выход (ru-route.sh),
# так что банки и Госуслуги работают. Списки сервисов не нужны — ничего не устаревает.
# Вернуть старый режим со списком сервисов: SMART_FULL=0 в bot.env.
SMART_FULL = os.environ.get("SMART_FULL", "1") == "1"
DIRECT_MODES = ("game", "pcgame") + (("smart",) if os.environ.get("SMART_DIRECT", "1") == "1" else ())  # собственная сеть Supercell; адреса серверов ещё и из DNS (GAME_HOSTS)

# Устройства и какой режим им подходит. На телефонах приложение AmneziaWG понимает список сетей,
# а на iPhone иначе российские приложения не пустить мимо VPN. AmneziaVPN на компьютере
# список игнорирует (macOS) или зависает на нём (Windows) — там весь трафик.
OS_NAMES = {"ios": "🍏 iPhone / iPad", "android": "🤖 Android", "pc": "💻 Компьютер"}
SMART_OS = ("ios", "android")

SMART_NETS = (
    *split.FORCE_VPN,  # Telegram, Meta, основные сети Google, X
    # Google и YouTube — остальные сети
    "8.8.4.0/24", "8.8.8.0/24", "8.34.208.0/20", "8.35.192.0/20", "23.236.48.0/20", "23.251.128.0/19",
    "64.15.112.0/20", "70.32.128.0/19", "172.253.0.0/16", "192.178.0.0/15", "199.36.154.0/23",
    "199.36.156.0/24", "207.223.160.0/20", "208.65.152.0/22", "208.117.224.0/19",
    # Meta — остальные сети
    "31.13.96.0/19", "45.64.40.0/22", "74.119.76.0/22", "103.4.96.0/22", "204.15.20.0/22",
    # X (Twitter) — остальные сети
    "69.195.160.0/19", "192.133.76.0/22", "199.16.156.0/22", "199.59.148.0/22", "199.96.56.0/21",
    "202.160.128.0/22", "209.237.192.0/19",
    # Discord
    "66.22.192.0/18", "138.128.136.0/21", "162.159.128.0/19", "162.245.208.0/21",
    # Twitch
    "52.223.192.0/18", "99.181.64.0/18", "185.42.204.0/22",
    # Reddit и другие сайты на Fastly
    "151.101.0.0/16", "199.232.0.0/16",
    *GAME_NETS,  # игры Supercell
    # DNS из конфига — чтобы адреса заблокированных сайтов не подменяли
    "1.1.1.1/32", "1.0.0.1/32",
    # IPv6 этих сервисов: у сервера IPv6 нет, так приложения сразу переходят на IPv4
    "2001:4860::/32", "2404:6800::/32", "2607:f8b0::/32", "2a00:1450::/32", "2a03:2880::/32",
    "2001:67c:4e8::/48", "2001:b28:f23c::/47", "2001:b28:f23f::/48", "2a0a:f280::/32",
)


DAY = 86400
# Цель оплаты: новый конфиг для устройства или для роутера; иначе — публичный ключ продлеваемого конфига.
NEW = "new"
TOPUP = "topup"                    # пополнение баланса (не конфиг)
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
BTN_BALANCE = "💰 Баланс и друзья"
KEYBOARD = ReplyKeyboardMarkup([[BTN_BUY], [BTN_LIST, BTN_HELP], [BTN_BALANCE, BTN_NEWS], [BTN_SUPPORT, BTN_CLEAR]],
                               resize_keyboard=True)

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
    "Как подключиться:\n"
    "1. Установите приложение AmneziaWG (App Store / Google Play).\n"
    f"2. «{BTN_BUY}» → оплатите → пришлите скриншот перевода.\n"
    "3. Бот пришлёт конфиг — добавьте его в приложение и включите.\n\n"
    f"{PRICE} ₽ за {DAYS} дней на устройство, роутер — {ROUTER_PRICE} ₽.\n"
    "👇 Инструкция для вашего устройства:"
)

# Подробные инструкции для клиентов — открываются кнопками из «❓ Как подключить».
MODE_TEXT = (
    "⚡️ Основной — всё через VPN сразу в Нидерланды: быстрее всего. Российские сайты и приложения "
    "(банки, Госуслуги) VPN сам отправляет через Россию. Выбирайте его.\n\n"
    "🛡 Обход белых списков — то же самое, но через сервер в Москве. Для случаев, когда оператор "
    "режет зарубежные адреса (ограничения мобильного интернета, «белые списки») и «Основной» не подключается.\n\n"
    "🎮 Игровой — минимальный пинг:\n"
    "• телефон: игры (Brawl Stars, Roblox, CoD Mobile, Pokémon GO, Fortnite и др.) + Telegram, Discord и YouTube; "
    "App Store и Google Play — напрямую, чтобы игры качались быстро;\n"
    "• ПК: всё через VPN, кроме Steam, CS2, Dota 2, Faceit и российских сайтов.\n\n"
    "Не подключается «Основной» или «Игровой» — выберите «Обход белых списков»."
)

# Список для клиентов: что идёт через VPN в режиме «только заблокированное».
# Сворачиваемая цитата Telegram — показывается свёрнутой, по нажатию разворачивается (parse_mode=HTML).
SMART_LIST = (
    "<blockquote expandable>📋 Что идёт через VPN в «Только заблокированное» (нажмите)\n\n"
    "Telegram, YouTube, Google, Instagram, WhatsApp, Facebook, X, Discord, Twitch, Reddit, TikTok, "
    "Netflix, Spotify, SoundCloud, Signal, Viber, LinkedIn, Medium, Patreon, Notion, Speedtest, нейросети "
    "(ChatGPT, Claude, Gemini, Grok, Perplexity, Midjourney и др.), игры Supercell и Roblox.\n\n"
    "Остальное — напрямую. Чего-то не хватает — напишите в «🆘 Нужна помощь».</blockquote>"
)

GUIDES = {
    "what": (
        "🛒 Что купить?\n\n"
        f"📱 Для себя — {PRICE} ₽: одно устройство (телефон или компьютер).\n"
        f"📡 Роутер — {ROUTER_PRICE} ₽: весь дом. Только Keenetic (KeeneticOS 4.2+) и OpenWrt.\n\n"
        "Другие роутеры (TP-Link, Asus и др.) не подойдут. Не уверены — берите «Для себя»."
    ),
    "ios": (
        "🍏 iPhone и iPad\n\n"
        "1. Установите AmneziaWG из App Store.\n"
        f"2. «{BTN_LIST}» → «⬇️ Скачать» → «iPhone».\n"
        "3. Нажмите на файл → «Поделиться» → AmneziaWG → «Разрешить».\n"
        "4. Включите переключатель."
    ),
    "android": (
        "🤖 Android\n\n"
        "1. Установите AmneziaVPN из Google Play.\n"
        f"2. «{BTN_LIST}» → «⬇️ Скачать» → «Android» → «🔑 Ключ».\n"
        "3. Нажмите на ключ (он скопируется) → в AmneziaVPN «➕» → «Вставить ключ».\n"
        "4. Нажмите «Подключиться».\n\n"
        "Или файлом в AmneziaWG: «➕» → «Импорт из файла»."
    ),
    "pc": (
        "💻 Компьютер\n\n"
        "1. Установите AmneziaWG (amnezia.org).\n"
        f"2. «{BTN_LIST}» → «⬇️ Скачать» → «Windows, Mac» → сохраните файл.\n"
        "3. В AmneziaWG: «Импорт из файла» → выберите файл → «Подключить»."
    ),
    "router-awg": (
        "📡 Роутер (Keenetic, OpenWrt)\n\n"
        "Keenetic:\n"
        "1. my.keenetic.net → «Интернет» → «Другие подключения» → «WireGuard» → «Загрузить из файла».\n"
        "2. Включите «Использовать для выхода в интернет».\n"
        "3. Выполните на my.keenetic.net/a команду из сообщения бота, затем «system configuration save».\n\n"
        "OpenWrt: установите amneziawg-tools и luci-proto-amneziawg, добавьте интерфейс AmneziaWG "
        "→ «Import configuration» → файл, затем зону wan."
    ),
    "mode": (
        "🔀 Режимы\n\n"
        + MODE_TEXT +
        f"\n\nПоменять: «{BTN_LIST}» → «🔀 Режим». Бот пришлёт новый файл — замените им старый."
        + ("" if SMART_FULL else "\n\n" + SMART_LIST)
    ),
    "trouble": (
        "🆘 Не работает?\n\n"
        f"1. Проверьте срок в «{BTN_LIST}» (🟢 — активен).\n"
        "2. Выключите и включите VPN, закройте другие VPN.\n"
        "3. Скачайте конфиг заново и замените старый.\n"
        "4. Не открывается какой-то сайт — выключите «🛡 Реклама».\n"
        "5. Не подключается «Основной» или «Игровой» — выберите «Обход белых списков».\n"
        "6. Один конфиг — одно устройство.\n\n"
        f"Не помогло — «{BTN_SUPPORT}»."
    ),
}

HELP_BUTTONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🛒 Что купить?", callback_data="help:what")],
    [InlineKeyboardButton("🍏 iPhone", callback_data="help:ios"),
     InlineKeyboardButton("🤖 Android", callback_data="help:android")],
    [InlineKeyboardButton("💻 Компьютер", callback_data="help:pc")],
    [InlineKeyboardButton("📡 Роутер (Keenetic, OpenWrt)", callback_data="help:router-awg")],
    [InlineKeyboardButton("🔀 Режимы: что это и как поменять", callback_data="help:mode")],
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


REFRESH_BUTTON = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить меню", callback_data="refresh:")]])


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
    # Опросы: {id: {"q", "opts", "votes": {uid: номер варианта}, "created", "closed"}}
    state.setdefault("polls", {})
    state.setdefault("next_poll", 1)
    state.setdefault("settings", {})
    # VLESS убран: выданные подписки VLESS удаляем, за действующие возвращаем цену на баланс.
    for pub in [k for k, p in state["peers"].items() if p.get("kind") == "vless"]:
        p = state["peers"].pop(pub)
        if p.get("until", 0) > time.time():
            u = state["users"].setdefault(p["user"], {"name": p["user"]})
            u["balance"] = u.get("balance", 0) + PRICE
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
        # Адреса на .0 и .255 в сетях больше /24 формально годятся, но некоторые устройства их не любят.
        if str(host) not in used and host.packed[-1] not in (0, 255):
            return str(host)
    raise RuntimeError("в подсети закончились адреса")


def is_router(state, target):
    if target in (NEW_ROUTER, NEW_ROUTER_AWG):
        return True
    return state["peers"].get(target, {}).get("kind") in ROUTER_KINDS


def price_for(state, target):
    return ROUTER_PRICE if is_router(state, target) else PRICE


def plans(state, target):
    """Варианты оплаты: (месяцев, дней, цена). Для устройства — ещё 3 месяца со скидкой."""
    if is_router(state, target):
        return [(1, DAYS, ROUTER_PRICE)]
    return [(1, DAYS, PRICE), (3, 3 * DAYS, PRICE3)]


def plan(state, target, months):
    return next((p for p in plans(state, target) if p[0] == months), plans(state, target)[0])


def balance(state, uid):
    return state["users"].get(str(uid), {}).get("balance", 0)


def months_text(m):
    return {1: "1 месяц", 3: "3 месяца"}.get(m, f"{m} мес.")


def create_peer(state, uid, until, kind="device"):
    """Добавляет конфиг в state (без применения на сервере). Возвращает его ключ и данные."""
    prefix = "rtr" if kind in ROUTER_KINDS else "vpn"
    names = {p["name"] for p in user_peers(state, uid).values()}
    n = next(i for i in range(1, 1000) if f"{prefix}{i}" not in names)
    priv = run(["awg", "genkey"])
    pub = run(["awg", "pubkey"], priv + "\n")
    peer = {"user": str(uid), "name": f"{prefix}{n}", "kind": kind,
            "priv": priv, "created": fmt_date(time.time()), "made": int(time.time()),
            "until": until, "warned": 0}
    if kind not in ROUTER_KINDS:
        u = state["users"].get(str(uid), {})
        peer["op"] = u.get("op")
        # Режим под устройство: на телефонах через VPN только заблокированное, на компьютере — всё.
        peer["os"] = u.get("os")
        if u.get("os") in SMART_OS or SMART_FULL:  # «Основной» — весь трафик, подходит и компьютеру
            peer["mode"] = "smart"
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
    # Профили операторов временно выключены (OP_PROFILES=0 по умолчанию): после их
    # появления был массовый сбой, конфиги снова такие же, как раньше.
    if os.environ.get("OP_PROFILES", "0") != "1":
        return params
    prof = PROFILES.get(peer.get("op"))
    if prof:
        params.update(Jc=str(prof["Jc"]), Jmin=str(prof["Jmin"]), Jmax=str(prof["Jmax"]))
        for k in ("I1", "I2", "I3", "I4", "I5"):
            params.pop(k, None)
        if prof["I1"]:
            params["I1"] = dns_i1(peer["priv"])
    return params


def ads_blocked(peer):
    """Блокируется ли реклама у конфига: DNS с блокировкой есть, клиент её не выключал.
    В игровом режиме и у роутеров — обычный DNS (у них он не идёт через VPN или настраивается иначе)."""
    return (bool(ADBLOCK_DNS) and peer.get("ads") != "off" and mode_of(peer) != "game"
            and peer.get("kind") not in ROUTER_KINDS)


def client_conf(peer, params, host, port=None):
    allowed = (router_allowed_ips(host) if peer.get("kind") in ROUTER_KINDS else
               (FULL_TUNNEL if SMART_FULL else smart_allowed_ips()) if mode_of(peer) == "smart" else
               smart_allowed_ips(game_only=True) if mode_of(peer) == "game" else
               PCGAME_NETS[0] if mode_of(peer) == "pcgame" and PCGAME_NETS[0] else allowed_ips())
    # DNS с блокировкой — на VPN-сервере, запросы к нему должны идти через VPN. В списках сетей
    # («только заблокированное», игровой ПК, раздельный туннель) локальные адреса 10.x вырезаны — добавляем.
    if ads_blocked(peer) and "0.0.0.0/0" not in allowed:
        allowed += f",{ADBLOCK_DNS}/32"
    lines = [
        "[Interface]",
        f"PrivateKey = {peer['priv']}",
        f"Address = {peer['ip']}/32",
        f"DNS = {ADBLOCK_DNS if ads_blocked(peer) else CLIENT_DNS}",
        "MTU = 1280",
    ]
    lines += [f"{k} = {v}" for k, v in obfs_params(peer, params).items()]
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {run(['awg', 'pubkey'], params['PrivateKey'] + chr(10))}",
        f"Endpoint = {host}:{port or params['ListenPort']}",
        f"AllowedIPs = {allowed}",
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


# Адреса, которые бот узнаёт сам и добавляет в режимы (новые файлы конфигов их уже содержат).
# Игровые серверы Supercell — по DNS раз в 6 часов.
GAME_HOSTS = ("game.brawlstarsgame.com", "game.clashroyaleapp.com", "gamea.clashofclans.com",
              "game.clashofclans.com", "game.haydaygame.com", "game.boombeachgame.com",
              # вход (Supercell ID), магазин и загрузка обновлений — точечно, по адресам, а не целыми сетями CDN
              "id.supercell.com", "security.supercell.com", "accounts.supercell.com", "store.supercell.com",
              "game-assets.brawlstarsgame.com", "game-assets.clashroyaleapp.com",
              "game-assets.clashofclans.com", "game-assets.haydaygame.com", "game-assets.boombeachgame.com")
# Ещё игры для игрового режима на телефоне: в России недоступны, режут вход или покупки. Только серверы
# входа и API — по адресам из DNS (по одной строке на адрес), а не целыми сетями: список для AmneziaWG
# остаётся коротким. Сама игра (матчи) часто идёт с других серверов — они в России не заблокированы.
GAME_SERVICES = {
    "Call of Duty Mobile": ("prod.demonware.net", "profile.callofduty.com", "s.activision.com",
                            "www.callofduty.com", "www.activision.com"),
    "Pokémon GO и Monster Hunter Now": ("pgorelease.nianticlabs.com", "sso.pokemon.com", "access.pokemon.com",
                                        "nianticlabs.com", "auth.nianticlabs.com"),
    "Fortnite (Epic Games)": ("account-public-service-prod.ol.epicgames.com",
                              "fortnite-public-service-prod11.ol.epicgames.com",
                              "lightswitch-public-service-prod.ol.epicgames.com",
                              "launcher-public-service-prod06.ol.epicgames.com", "store.epicgames.com"),
    "EA (FC Mobile, The Sims, Apex)": ("accounts.ea.com", "gateway.ea.com", "signin.ea.com", "www.ea.com"),
    "Blizzard (Hearthstone, Diablo Immortal, Warcraft Rumble)": (
        "eu.actual.battle.net", "us.actual.battle.net", "account.battle.net", "eu.battle.net",
        "eu.version.battle.net"),
    "Minecraft и Xbox": ("user.auth.xboxlive.com", "xsts.auth.xboxlive.com", "api.minecraftservices.com",
                         "sessionserver.mojang.com", "authserver.mojang.com", "login.live.com"),
    "Riot (Wild Rift, LoL, TFT, Valorant)": ("auth.riotgames.com", "authenticate.riotgames.com",
                                             "entitlements.auth.riotgames.com",
                                             "clientconfig.rpg.riotgames.com"),
    "Ubisoft": ("public-ubiservices.ubi.com", "connect.ubisoft.com"),
    "PlayStation App": ("ca.account.sony.com", "my.account.sony.com", "web.np.playstation.com"),
    "Nintendo": ("accounts.nintendo.com", "api.accounts.nintendo.com"),
}
GAME_IP_MAX = 4      # адресов с одного имени: у CDN их бывает десятки
GAME_IPS = set()
# Магазины приложений — мимо VPN в игровом режиме: игры и обновления скачиваются напрямую, на полной
# скорости. Google Play раздаёт файлы с тех же серверов Google, что и YouTube, поэтому из туннеля
# вырезаются только подсети /24 с адресами магазина; App Store (сети Apple) и так идёт мимо.
STORE_HOSTS = ("play.googleapis.com", "play-lh.googleusercontent.com", "play-fe.googleapis.com",
               "android.clients.google.com", "play.google.com", "dl.google.com", "edgedl.me.gvt1.com",
               "redirector.gvt1.com", "apps.apple.com", "itunes.apple.com", "iosapps.itunes.apple.com",
               "apps.mzstatic.com", "is1-ssl.mzstatic.com", "updates.cdn-apple.com", "appldnld.apple.com")
STORE_IPS = set()
GAME_LIST_MAX = 250  # длиннее список AmneziaWG на телефоне принимает плохо
# TikTok: сети TikTok и ByteDance (RIPEstat, раз в сутки) и адреса его серверов по DNS.
# Сетей у них много, берём самые крупные — общий список должен остаться около ста.
TIKTOK_ASNS = (138699, 396986)
TIKTOK_MAX = 15
TIKTOK_SEED = ("71.18.16.0/22", "71.18.247.0/24", "71.18.248.0/21", "101.45.200.0/23", "101.45.248.0/22",
               "103.136.220.0/23", "2404:9dc0::/32")
TIKTOK_HOSTS = ("www.tiktok.com", "m.tiktok.com", "api.tiktokv.com", "api16-normal-c-useast1a.tiktokv.com",
                "api16-normal-c-alisg.tiktokv.com", "api22-normal-c-alisg.tiktokv.com",
                "api16-normal-useast5.tiktokv.us", "log.tiktokv.com", "mon.tiktokv.com")
TIKTOK_NETS = set(TIKTOK_SEED)
# Roblox (в России заблокирован) — его собственные сети, только для игрового режима на телефонах.
ROBLOX_ASNS = (22697,)
ROBLOX_SEED = ("128.116.0.0/17",)
ROBLOX_NETS = set(ROBLOX_SEED)
# Ещё популярные сервисы для «Только заблокированное».
# Со своими сетями — сетями (Netflix — постоянный список; Spotify и LinkedIn — из RIPEstat, по 5 крупнейших):
NETFLIX_NETS = ("23.246.0.0/18", "37.77.184.0/21", "45.57.0.0/17", "64.120.128.0/17", "66.197.128.0/17",
                "108.175.32.0/20", "192.173.64.0/18", "198.38.96.0/19", "198.45.48.0/20")
EXTRA_ASNS = {"Spotify": 8403, "LinkedIn": 14413}
EXTRA_NETS = set()
# На общих облаках (Cloudflare, AWS) — только точные адреса по DNS, чтобы не тянуть через VPN чужие сайты.
# Сервисы с адресами по DNS: по ним же бот понимает, что у клиента в файле устарели адреса сервиса.
SMART_SERVICES = {
    # Нейросети (кроме российских — Алисы и GigaChat: они работают напрямую и могут не пустить с иностранного IP)
    "ChatGPT": ("chatgpt.com", "chat.openai.com", "openai.com", "api.openai.com", "cdn.oaistatic.com",
                "ab.chatgpt.com", "sora.com", "sora.chatgpt.com"),
    "Claude": ("claude.ai", "api.anthropic.com", "anthropic.com"),
    "Gemini": ("gemini.google.com", "aistudio.google.com", "notebooklm.google.com"),
    "Copilot": ("copilot.microsoft.com", "www.bing.com", "sydney.bing.com"),
    "Grok": ("grok.com", "x.ai", "api.x.ai"),
    "Perplexity": ("www.perplexity.ai", "perplexity.ai"),
    "Mistral": ("chat.mistral.ai",), "DeepSeek": ("chat.deepseek.com",), "Poe": ("poe.com",),
    "Character.AI": ("character.ai",), "Hugging Face": ("huggingface.co",),
    "Midjourney": ("www.midjourney.com", "cdn.midjourney.com"),
    "Suno": ("suno.com", "studio-api.prod.suno.com"), "Udio": ("www.udio.com",),
    "Runway": ("app.runwayml.com",), "Leonardo": ("app.leonardo.ai",), "ElevenLabs": ("elevenlabs.io",),
    "Ideogram": ("ideogram.ai",), "Pika": ("pika.art",), "Cursor": ("cursor.com", "api2.cursor.sh"),
    "GitHub Copilot": ("api.githubcopilot.com",), "Meta AI": ("meta.ai",),
    "Signal": ("signal.org", "chat.signal.org", "storage.signal.org", "cdn.signal.org", "cdn2.signal.org"),
    "Viber": ("viber.com", "www.viber.com", "dl-media.viber.com"),
    "SoundCloud": ("soundcloud.com", "api-v2.soundcloud.com", "cf-media.sndcdn.com"),
    "Spotify": ("open.spotify.com", "spclient.wg.spotify.com", "audio-ak-spotify-com.akamaized.net"),
    "LinkedIn": ("www.linkedin.com",), "Medium": ("medium.com",), "Patreon": ("www.patreon.com",),
    "Notion": ("www.notion.so",),
    # Speedtest (Ookla) — в России ограничен. Через VPN сайт и выбор сервера; сам замер идёт к выбранному
    # серверу проверки (их тысячи у разных провайдеров, в список не уместить).
    "Speedtest": ("www.speedtest.net", "speedtest.net", "b.cdn.speedtest.net", "c.speedtest.net",
                  "api.speedtest.net", "www.ookla.com"),
}
SERVICE_IPS = {}  # сервис -> текущие адреса
SMART_IPS = set()
TIKTOK_IPS = set()


# «🎮 Игровой для ПК»: всё через VPN напрямую в Нидерланды, КРОМЕ того, что работает из России и с
# российским IP лучше: Steam, CS2, Dota 2 (сети Valve), Faceit, КиберШок, Яндекс Музыка и всё российское.
# Discord, Spotify, Cloudflare, Telegram, Meta, Google — всегда через VPN.
VALVE_ASNS = (32590,)
SPOTIFY_ASNS = (8403,)
PC_DIRECT_HOSTS = ("faceit.com", "api.faceit.com", "www.faceit.com", "faceit-client.faceit-cdn.net",
                   "cybershoke.net", "music.yandex.ru", "music.yandex.net")
DISCORD_NETS = ("66.22.192.0/18", "138.128.136.0/21", "162.159.128.0/19", "162.245.208.0/21")
PCGAME_NETS = [""]  # готовый список AllowedIPs; пусто — пока не собран, тогда весь трафик


async def refresh_pcgame():
    direct = []
    for asn in VALVE_ASNS:
        direct += [n for n in await asyncio.to_thread(split.fetch_asn_prefixes, asn) if n.version == 4]
    direct += [ipaddress.ip_network(f"{ip}/32") for ip in await resolve(PC_DIRECT_HOSTS)]
    # Адреса наших серверов — мимо туннеля, иначе подключение к ним уйдёт в сам туннель.
    for host in (ENDPOINT_HOST, RELAY_HOST):
        try:
            direct.append(ipaddress.ip_network(f"{host}/32"))
        except ValueError:
            pass
    force = [*split.FORCE_VPN, *(n for n in CLOUDFLARE_NETS if ":" not in n), *DISCORD_NETS]
    for asn in SPOTIFY_ASNS:
        force += [str(n) for n in await asyncio.to_thread(split.fetch_asn_prefixes, asn) if n.version == 4]
    ru = await asyncio.to_thread(split.fetch_ru_networks)
    # Valve, Faceit и т. п. не должны попасть в «через VPN» из-за принудительных сетей (Cloudflare):
    # то, что пересекается с ними, остаётся через VPN — так решил клиент («Cloudflare через VPN»).
    nets = await asyncio.to_thread(split.tunnel_networks, ru + direct, 800, force)
    PCGAME_NETS[0] = ",".join(map(str, nets)) + ",::/0"
    log.info("игровой режим ПК: %d сетей через VPN (мимо: %d сетей Valve/Faceit и российские)",
             len(nets), len(direct))


async def resolve(hosts, per_host=None):
    loop = asyncio.get_running_loop()
    found = set()
    for host in hosts:
        try:
            infos = await asyncio.wait_for(loop.getaddrinfo(host, 443, family=socket.AF_INET), 10)
            found.update(sorted({i[4][0] for i in infos})[:per_host])
        except Exception:
            pass
    return found


async def dynamic_loop():
    """Раз в 6 часов: адреса игр и TikTok по DNS; раз в сутки: сети TikTok из RIPEstat."""
    rounds = 0
    while True:
        for hosts, target in ((GAME_HOSTS, GAME_IPS),) + (() if SMART_FULL else ((TIKTOK_HOSTS, TIKTOK_IPS),)):
            found = await resolve(hosts)
            if target is GAME_IPS:
                found |= await resolve([h for hs in GAME_SERVICES.values() for h in hs], GAME_IP_MAX)
            if found:
                target.clear()
                target.update(found)
        found = await resolve(STORE_HOSTS)
        if found:
            STORE_IPS.clear()
            STORE_IPS.update(found)
        for name, hosts in ({} if SMART_FULL else SMART_SERVICES).items():
            found = await resolve(hosts)
            if found:
                SERVICE_IPS[name] = found
        SMART_IPS.clear()
        SMART_IPS.update(ip for ips in SERVICE_IPS.values() for ip in ips)
        try:
            await notify_stale_smart()
        except Exception:
            log.exception("проверка устаревших конфигов не удалась")
        if rounds % 4 == 0:
            nets = []
            for asn in TIKTOK_ASNS:
                try:
                    nets += await asyncio.to_thread(split.fetch_asn_prefixes, asn)
                except Exception as e:
                    log.warning("сети TikTok AS%s не скачались: %s", asn, e)
            if nets:
                # Самые крупные сети: IPv4 — до TIKTOK_MAX, IPv6 — до 3.
                v4 = sorted((n for n in nets if n.version == 4), key=lambda n: n.prefixlen)[:TIKTOK_MAX]
                v6 = sorted((n for n in nets if n.version == 6), key=lambda n: n.prefixlen)[:3]
                TIKTOK_NETS.clear()
                TIKTOK_NETS.update(TIKTOK_SEED)
                TIKTOK_NETS.update(str(n) for n in v4 + v6)
                log.info("TikTok: взято %d сетей из %d", len(v4) + len(v6), len(nets))
            extra = set()
            for name, asn in EXTRA_ASNS.items():
                try:
                    got = [n for n in await asyncio.to_thread(split.fetch_asn_prefixes, asn) if n.version == 4]
                    extra.update(str(n) for n in sorted(got, key=lambda n: n.prefixlen)[:5])
                except Exception as e:
                    log.warning("сети %s не скачались: %s", name, e)
            if extra:
                EXTRA_NETS.clear()
                EXTRA_NETS.update(extra)
            nets = []
            for asn in ROBLOX_ASNS:
                try:
                    nets += await asyncio.to_thread(split.fetch_asn_prefixes, asn)
                except Exception as e:
                    log.warning("сети Roblox AS%s не скачались: %s", asn, e)
            if nets:
                v4 = sorted((n for n in nets if n.version == 4), key=lambda n: n.prefixlen)[:10]
                v6 = sorted((n for n in nets if n.version == 6), key=lambda n: n.prefixlen)[:2]
                ROBLOX_NETS.clear()
                ROBLOX_NETS.update(ROBLOX_SEED)
                ROBLOX_NETS.update(str(n) for n in v4 + v6)
                log.info("Roblox: взято %d сетей из %d", len(v4) + len(v6), len(nets))
        if rounds % 4 == 0:
            try:
                await refresh_pcgame()
            except Exception as e:
                log.warning("список для игрового режима ПК не собрался: %s", e)
        rounds += 1
        await asyncio.sleep(6 * 3600)


def mode_of(peer):
    return peer.get("mode") if peer.get("mode") in MODES else "full"


def smart_allowed_ips(game_only=False):
    """Список сетей для «только заблокированное» (или только игры для игрового режима)."""
    nets = ([*GAME_NETS, *CLOUDFLARE_NETS, *ROBLOX_NETS, *GAME_EXTRA_NETS] if game_only else
            [*SMART_NETS, *TIKTOK_NETS, *NETFLIX_NETS, *EXTRA_NETS, *ROBLOX_NETS])
    ips = GAME_IPS if game_only else GAME_IPS | TIKTOK_IPS | SMART_IPS
    nets = [ipaddress.ip_network(n) for n in nets]
    v4 = list(ipaddress.collapse_addresses(
        [n for n in nets if n.version == 4] + [ipaddress.ip_network(f"{ip}/32") for ip in ips]))
    v6 = list(ipaddress.collapse_addresses(n for n in nets if n.version == 6))
    if game_only:
        v4 = without_stores(v4, GAME_LIST_MAX - len(v6))
    return ",".join(map(str, [*v4, *v6]))


def without_stores(v4, limit):
    """Вырезать из списка подсети /24 магазинов приложений — пока список не длиннее limit."""
    for ip in sorted(STORE_IPS):
        cut = ipaddress.ip_network(f"{ip}/24", strict=False)
        nxt = []
        for n in v4:
            if n.supernet_of(cut) and n != cut:
                nxt += n.address_exclude(cut)
            elif not cut.supernet_of(n):
                nxt.append(n)
        nxt = list(ipaddress.collapse_addresses(nxt))
        if len(nxt) > limit:
            break
        v4 = nxt
    return v4


async def split_loop():
    while SPLIT_RU:
        try:
            await refresh_split()
            await asyncio.sleep(DAY)
        except Exception:
            log.exception("не удалось обновить список российских сетей")
            await asyncio.sleep(3600)


# ---------- клиентский бот ----------

# Название туннеля в приложении: латиница без пробелов, на iOS не длиннее 15 символов (оставляем место под «-nl»).
TUNNEL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,12}")


def title(peer):
    """Как конфиг называется в сообщениях: название клиента, если он его задал."""
    return f"{peer['label']} ({peer['name']})" if peer.get("label") else peer["name"]


def file_base(peer):
    """Имя файла конфига: название клиента, если оно годится для туннеля, иначе vpnN."""
    label = peer.get("label") or ""
    return label if TUNNEL_NAME.fullmatch(label) else peer["name"]


def vpn_key(peer, params, host, port, conf_text):
    """Ключ vpn://… для приложения AmneziaVPN («Вставить ключ») — тот же конфиг одной строкой.

    Формат как у самого AmneziaVPN (и amneziawg-installer): JSON, сжатый zlib, с 4 байтами длины
    впереди (qCompress), в base64url без «=».
    """
    obfs = obfs_params(peer, params)
    fields = dict(line.split(" = ", 1) for line in conf_text.splitlines() if " = " in line)
    inner = {k: obfs.get(k, "0") for k in ("H1", "H2", "H3", "H4", "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4")}
    if any(obfs.get(f"I{i}") for i in range(1, 6)):
        inner.update({f"I{i}": obfs.get(f"I{i}", "") for i in range(1, 6)})
    inner.update({
        "allowed_ips": [ip.strip() for ip in fields["AllowedIPs"].split(",") if ip.strip()],
        "client_ip": peer["ip"], "client_ipv6": "", "client_priv_key": peer["priv"],
        "config": conf_text.strip(), "hostName": host, "mtu": "1280", "persistent_keep_alive": "25",
        "port": int(port), "server_pub_key": fields["PublicKey"],
    })
    dns = [d.strip() for d in fields["DNS"].split(",")]
    outer = {
        "containers": [{"awg": {"isThirdPartyConfig": True,
                                "last_config": json.dumps(inner, ensure_ascii=False, separators=(",", ":")),
                                "port": str(port), "protocol_version": "2", "transport_proto": "udp"},
                        "container": "amnezia-awg"}],
        "defaultContainer": "amnezia-awg", "description": f"MOST {title(peer)}",
        "dns1": dns[0], "dns2": dns[1] if len(dns) > 1 else dns[0], "hostName": host,
    }
    raw = json.dumps(outer, ensure_ascii=False, separators=(",", ":")).encode()
    packed = len(raw).to_bytes(4, "big") + zlib.compress(raw)
    return "vpn://" + base64.urlsafe_b64encode(packed).decode().rstrip("=")


def qr_png(text):
    """QR-код конфига или None, если он не помещается даже в самый ёмкий QR (около 2900 символов)."""
    try:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
        qr.add_data(text)
        qr.make(fit=True)
    except Exception:
        return None
    png = io.BytesIO()
    qr.make_image().save(png, format="PNG")
    png.seek(0)
    return png


def peer_variants(peer):
    """Файлы конфига: имя, адрес и порт подключения."""
    # Имя файла становится именем туннеля; на iOS оно не длиннее 15 символов.
    if mode_of(peer) in DIRECT_MODES and peer.get("kind") not in ROUTER_KINDS:
        # Игровой режим — напрямую на основной сервер, минуя вход в России.
        return [(f"{file_base(peer)}.conf", ENDPOINT_HOST, GAME_PORT)]
    if RELAY_HOST:
        variants = [(f"{file_base(peer)}.conf", RELAY_HOST, None)]
        if SEND_DIRECT:
            variants.append((f"{file_base(peer)}-nl.conf", ENDPOINT_HOST, None))
        return variants
    return [(f"{file_base(peer)}.conf", ENDPOINT_HOST, None)]


def peer_pub(peer):
    """Публичный ключ конфига (по нему кнопки находят конфиг)."""
    return next((k for k, p in load_state()["peers"].items() if p.get("priv") == peer.get("priv")), None)


def download_buttons(pub):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍏 iPhone / 💻 Windows, Mac", callback_data=f"dl:ios:{pub}")],
        [InlineKeyboardButton("🤖 Android", callback_data=f"dl:and:{pub}")],
        [InlineKeyboardButton("📦 Все режимы сразу", callback_data=f"dl:allm:{pub}")],
        [InlineKeyboardButton("⬅️ К конфигу", callback_data=f"cfg:{pub}")],
    ])


def back_button(pub):
    return InlineKeyboardButton("⬅️ Назад", callback_data=f"cfg:{pub}")


async def show_card(query, pub, note=""):
    """Возвращает сообщение к экрану конфига (note — строка сверху: что сейчас изменилось)."""
    state = load_state()
    p = state["peers"].get(pub)
    if not p:
        await query.edit_message_text("Конфиг не найден.")
        return
    text, buttons = config_card(state, pub, p, back=len(user_peers(state, p["user"])) > 1)
    try:
        await query.edit_message_text((f"{note}\n\n" if note else "") + text, reply_markup=buttons)
    except Exception:
        pass


async def show_download(query, pub, note=""):
    """Экран «скачать новый файл» в том же сообщении — после смены режима, оператора, рекламы."""
    p = load_state()["peers"].get(pub)
    if not p:
        return
    if not peer_active(p):
        await show_card(query, pub, f"{note}\nСрок закончился — продлите, и конфиг заработает с новыми настройками.")
        return
    await query.edit_message_text(f"{note}\n\n📥 Скачайте новый файл и замените им старый туннель. "
                                  "На каком устройстве?", reply_markup=download_buttons(pub))


def android_buttons(pub):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Ключ (рекомендуется)", callback_data=f"dl:key:{pub}")],
        [InlineKeyboardButton("📄 Файл", callback_data=f"dl:file:{pub}"),
         InlineKeyboardButton("🔳 QR-код", callback_data=f"dl:qr:{pub}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"dl:back:{pub}")],
    ])


async def send_peer(chat_id, peer):
    """Выдача конфига: роутеру — сразу файл; телефону и компьютеру — сначала выбор устройства."""
    pub = peer_pub(peer)
    if peer.get("kind") in ROUTER_KINDS or not pub:
        await deliver_peer(chat_id, peer, "all")
        return
    await USER_BOT.send_message(chat_id, f"📥 {title(peer)}: на каком устройстве подключаете?",
                                reply_markup=download_buttons(pub))


async def remember_smart_snapshot(peer):
    """Запоминает адреса сервисов, попавшие в файл «Только заблокированное», — чтобы потом понять, что устарели."""
    if mode_of(peer) != "smart" or SMART_FULL:
        return
    pub = peer_pub(peer)
    async with lock:
        state = load_state()
        p = state["peers"].get(pub)
        if p:
            p["smart_ips"] = sorted(SMART_IPS)
            p.pop("stale_notified", None)
            save_state(state)


async def notify_stale_smart():
    """Клиентам «Только заблокированное», у которых в файле не осталось ни одного текущего адреса сервиса,
    бот предлагает скачать файл заново (не чаще раза в 3 дня)."""
    if SMART_FULL:
        return
    async with lock:
        state = load_state()
        todo = []
        for pub, p in state["peers"].items():
            if mode_of(p) != "smart" or not peer_active(p) or not p.get("smart_ips"):
                continue
            if time.time() - p.get("stale_notified", 0) < 3 * DAY:
                continue
            old = set(p["smart_ips"])
            broken = [name for name, ips in SERVICE_IPS.items() if ips and not ips & old]
            if broken:
                p["stale_notified"] = int(time.time())
                todo.append((pub, p, broken))
        if todo:
            save_state(state)
    for pub, p, broken in todo:
        try:
            await USER_BOT.send_message(
                int(p["user"]),
                f"🔄 {title(p)}: обновите конфиг, чтобы снова работали: {', '.join(broken[:8])}"
                + (" и другие" if len(broken) > 8 else "") + ".\n\n"
                "У этих сервисов сменились адреса. Нажмите кнопку, удалите старый туннель и добавьте новый — "
                "ключи и срок те же.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Получить новый конфиг",
                                                                         callback_data=f"get:{pub}")]]))
        except Exception:
            pass


# «📦 Все режимы сразу»: по файлу на режим — в приложении это отдельные туннели, клиент переключается
# между ними сам. Ключ у всех один (это тот же конфиг), поэтому включать можно только один туннель.
ALL_MODES = {"smart": "osn", "full": "obhod", "game": "game"}
ALL_MODES_PC = {"smart": "osn", "full": "obhod", "pcgame": "game"}


def all_modes_buttons(pub):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Ключи (AmneziaVPN)", callback_data=f"dl:amk:{pub}")],
        [InlineKeyboardButton("🔳 QR-коды", callback_data=f"dl:amq:{pub}"),
         InlineKeyboardButton("📄 Файлы", callback_data=f"dl:amf:{pub}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"dl:back:{pub}")],
    ])


ALL_MODES_HINT = {
    "amk": "👆 Нажмите на ключ — он скопируется. AmneziaVPN → «➕» → «Вставить ключ». Так с каждым ключом.",
    "amq": "👆 Отсканируйте каждый QR-код в AmneziaWG («➕» → «Сканировать QR-код») с другого экрана.",
    "amf": "👆 Добавьте все файлы в AmneziaWG (или AmneziaVPN).",
}


async def deliver_all_modes(chat_id, peer, fmt="amf"):
    """По конфигу на каждый режим — ключами, QR-кодами или файлами (fmt: amk, amq, amf)."""
    params = server_params(peer_iface(peer)[1])
    base = file_base(peer)
    ids = []
    for mode, suffix in (ALL_MODES_PC if peer.get("os") == "pc" else ALL_MODES).items():
        mp = dict(peer, mode=mode)
        _, host, port = peer_variants(mp)[0]
        name = f"{base[:14 - len(suffix)]}-{suffix}"  # имя туннеля на iOS — до 15 символов
        text = client_conf(mp, params, host, port)
        if fmt == "amk":
            key = vpn_key(mp, params, host, port or params["ListenPort"], text)
            if len(key) < 4000:  # больше не влезает в одно сообщение Telegram
                msg = await USER_BOT.send_message(chat_id, f"{MODES[mode]}\n<code>{key}</code>", parse_mode="HTML")
            else:
                msg = await USER_BOT.send_document(chat_id, document=io.BytesIO(text.encode()),
                                                   filename=f"{name}.conf",
                                                   caption=f"{MODES[mode]}: ключ слишком длинный — вот файл.")
        elif fmt == "amq":
            png = qr_png(text)
            if png:
                msg = await USER_BOT.send_photo(chat_id, photo=png, caption=MODES[mode])
            else:
                msg = await USER_BOT.send_message(chat_id, f"{MODES[mode]}: слишком длинный для QR-кода — "
                                                           "возьмите ключ или файл.")
        else:
            msg = await USER_BOT.send_document(chat_id, document=io.BytesIO(text.encode()),
                                               filename=f"{name}.conf", caption=MODES[mode])
        ids.append(msg.message_id)
    await USER_BOT.send_message(
        chat_id, ALL_MODES_HINT[fmt] + "\nЭто режимы — переключайтесь между ними прямо в приложении. "
                                       "Включайте только один за раз.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            "✅ Готово — убрать", callback_data="kdone:" + ",".join(map(str, ids)))]]))


async def deliver_peer(chat_id, peer, how):
    """how: file — файл; qr — QR-код; key — ключ для AmneziaVPN; ios — файл и QR; all — всё (роутеры)."""
    if how in ALL_MODES_HINT and peer.get("kind") not in ROUTER_KINDS:
        await deliver_all_modes(chat_id, peer, how)
        return
    await remember_smart_snapshot(peer)
    params = server_params(peer_iface(peer)[1])
    for filename, host, port in peer_variants(peer):
        text = client_conf(peer, params, host, port)
        file_msg = None
        if how in ("file", "ios", "all"):
            file_msg = await USER_BOT.send_document(chat_id, document=io.BytesIO(text.encode()), filename=filename,
                                                    caption=f"{filename} — {peer_status(peer)}",
                                                    reply_markup=InlineKeyboardMarkup([[CLOSE_BUTTON]]))
        if peer.get("kind") in ROUTER_KINDS:
            continue
        if how in ("qr", "ios"):
            png = qr_png(text)
            if png:
                # Файл и QR пришли вместе (iPhone) — одна кнопка под QR убирает оба.
                done = (InlineKeyboardButton("✅ Готово — убрать файл и QR", callback_data=f"kdone:{file_msg.message_id}")
                        if file_msg else CLOSE_BUTTON)
                await USER_BOT.send_photo(chat_id, photo=png, caption=f"QR-код: {filename}",
                                          reply_markup=InlineKeyboardMarkup([[done]]))
            elif how == "qr":
                await USER_BOT.send_message(chat_id, f"{filename}: в этом режиме конфиг слишком длинный для QR-кода — "
                                                     "возьмите ключ или файл.",
                                            reply_markup=InlineKeyboardMarkup([[CLOSE_BUTTON]]))
        if how == "key":
            key = vpn_key(peer, params, host, port or params["ListenPort"], text)
            # Кнопка «Скопировать» в Telegram вмещает только 256 символов, а ключ длиннее,
            # поэтому ключ — моноширинным текстом: одно нажатие на него копирует целиком.
            # Отдельным сообщением только ключ — его удобно скопировать целиком, без лишнего текста.
            key_msg = await USER_BOT.send_message(chat_id, f"<code>{key}</code>", parse_mode="HTML")
            await USER_BOT.send_message(chat_id, "👆 Нажмите на ключ — он скопируется.\n"
                                                 "Затем AmneziaVPN → «➕» → «Вставить ключ» → «Подключиться».",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                            "✅ Готово", callback_data=f"kdone:{key_msg.message_id}")]]))
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
            f"📡 Настройка роутера для {title(peer)}\n\n"
            "1. Откройте настройки роутера → «VPN-клиент» (или «VPN», «Другие подключения») → «WireGuard».\n"
            f"2. Загрузите файл {file_base(peer)}.conf. Если роутер не умеет загружать файл, впишите поля вручную:\n\n"
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
        f"📡 Настройка роутера Keenetic для {title(peer)}\n\n"
        "1. Откройте my.keenetic.net → «Интернет» → «Другие подключения» → «WireGuard» → "
        f"«Загрузить из файла» и выберите файл {file_base(peer)}.conf.\n"
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
    if target == TOPUP:
        return "пополнение баланса"
    if target == NEW:
        return "новый конфиг для себя"
    if target == NEW_ROUTER:
        return "новый конфиг для роутера (WireGuard)"
    if target == NEW_ROUTER_AWG:
        return "новый конфиг для роутера (AmneziaWG)"
    peer = state["peers"].get(target)
    return f"продление {title(peer)} ({peer_status(peer)})" if peer else "продление удалённого конфига"


async def request_access(user):
    """Отправляет администраторам запрос доступа (один раз)."""
    async with lock:
        state = load_state()
        u = get_user(state, user)
        first = "requested" not in u
        u.setdefault("requested", int(time.time()))
        save_state(state)
    if first:
        ref = u.get("ref")
        who = f"\n👥 Пригласил: {user_name(state, ref)} (ID {ref})" if ref else ""
        for admin in ADMINS:
            try:
                await ADMIN_BOT.send_message(admin, f"🙋 Запрос доступа\n{u['name']}\nID {user.id}{who}",
                                             reply_markup=access_buttons(user.id))
            except Exception:
                log.exception("не удалось отправить запрос доступа админу %s", admin)


REF_ASK = ("👥 Доступ по приглашению. Пришлите ID друга, который вас пригласил, — одним числом.\n"
           f"Он есть у друга в «{BTN_BALANCE}».")


async def gate(update, ctx, state):
    """Пускает дальше, только если доступ открыт. Новичка сначала спрашивает, кто его пригласил,
    и только потом отправляет запрос администратору."""
    user = update.effective_user
    if is_approved(state, user.id):
        return True
    u = state["users"].get(str(user.id), {})
    if not u.get("ref") and "requested" not in u and u.get("approved") is not False:
        ctx.user_data["await"] = "ref"
        await update.message.reply_text(REF_ASK)
        return False
    await request_access(user)
    await update.message.reply_text(closed_text(state, user.id))
    return False


def parse_ref(state, uid, text):
    """ID пригласившего из текста или ссылки; None — если такого клиента нет."""
    rid = re.sub(r"^ref_", "", (text or "").strip())
    if not rid.isdigit() or rid == str(uid) or rid not in state["users"]:
        return None
    if not is_approved(state, rid) and not is_admin(rid):
        return None
    return rid


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with lock:
        state = load_state()
        u = get_user(state, update.effective_user)
        # Ссылка друга: t.me/бот?start=ref_ID — запоминаем, кто пригласил (только новичкам).
        arg = (ctx.args or [""])[0] if getattr(ctx, "args", None) else ""
        note = ""
        if arg.startswith("ref_"):
            me = str(update.effective_user.id)
            rid = parse_ref(state, me, arg)
            if arg == f"ref_{me}":
                note = "👥 Это ваша ссылка — отправьте её другу."
            elif u.get("ref"):
                note = f"👥 Вас уже пригласил {user_name(state, u['ref'])}."
            elif user_peers(state, me) or any(p["user"] == me for p in state["payments"].values()):
                note = "👥 Вы уже клиент — ссылка приглашения работает только для новых."
            elif rid:
                u["ref"] = rid
                note = f"👥 Вас пригласил {user_name(state, rid)}."
        save_state(state)
    if note:
        await update.message.reply_text(note)
    if not await gate(update, ctx, state):
        return
    mine = user_peers(state, update.effective_user.id)
    active = sum(1 for p in mine.values() if peer_active(p))
    await update.message.reply_text(
        f"Привет! Это VPN MOST.\n{PRICE} ₽ за {DAYS} дней на устройство, 3 месяца — {PRICE3} ₽. "
        f"Роутер — {ROUTER_PRICE} ₽.\n\n"
        + (f"Ваших конфигов: {len(mine)}, активных: {active}. Подробнее — «{BTN_LIST}»." if mine
           else f"🎁 Попробуйте бесплатно {TRIAL_DAYS} дня: «{BTN_BUY}» → «Попробовать бесплатно»."
           if trial_available(state, update.effective_user.id)
           else f"Чтобы подключиться, нажмите «{BTN_BUY}»."),
        reply_markup=KEYBOARD)


async def ask_payment(message, ctx, target, state, query=None):
    """Выбор срока оплаты; с query — в том же сообщении (продление с экрана конфига, с кнопкой «Назад»).
    Если вариант один (роутер) — сразу реквизиты."""
    leave_ticket(ctx)
    options = plans(state, target)
    if len(options) == 1:
        await pay_screen(message, ctx, target, 1, state, query)
        return
    rows = []
    for m, days, price in options:
        save = m * PRICE - price
        rows.append([InlineKeyboardButton(f"💳 {months_text(m)} — {price} ₽" + (f" (выгода {save} ₽)" if save > 0 else ""),
                                          callback_data=f"per:{m}:{target}")])
    if target not in KIND_BY_TARGET:
        rows.append([back_button(target)])
    text = f"Оплата: {target_label(state, target)}.\nНа какой срок?"
    bal = balance(state, message.chat_id)
    if bal:
        text += f"\n\n💰 На балансе {bal} ₽ — можно оплатить с него."
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def pay_screen(message, ctx, target, months, state, query=None):
    """Оплата выбранного срока — только с баланса: хватает — кнопка «Оплатить», нет — «Пополнить»
    (после пополнения конфиг оплатится сам)."""
    months, days, price = plan(state, target, months)
    warning = f"{router_requirements(target)}\n\n" if target in (NEW_ROUTER, NEW_ROUTER_AWG) else ""
    bal = balance(state, message.chat_id)
    text = f"Оплата: {target_label(state, target)} — {price} ₽ за {days} дней.\n\n{warning}💰 На балансе: {bal} ₽."
    rows = []
    if bal >= price:
        rows.append([InlineKeyboardButton(f"✅ Оплатить {price} ₽ с баланса", callback_data=f"bal:{months}:{target}")])
    else:
        text += (f"\nНа балансе недостаточно средств. Чтобы пополнить, нажмите «Пополнить» — "
                 "после пополнения конфиг оплатится сам.")
        rows.append([InlineKeyboardButton("➕ Пополнить", callback_data=f"topb:{months}:{target}")])
    if len(plans(state, target)) > 1:
        rows.append([InlineKeyboardButton("⬅️ Другой срок", callback_data=f"pln:{target}")])
    elif target not in KIND_BY_TARGET:
        rows.append([back_button(target)])
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def buy_with_balance(uid, target, months):
    """Списывает цену с баланса и выдаёт или продлевает конфиг. Возвращает (получилось, текст)."""
    async with lock:
        state = load_state()
        u = state["users"].setdefault(str(uid), {"name": str(uid)})
        months, days, price = plan(state, target, months)
        if target not in KIND_BY_TARGET and target not in state["peers"]:
            return False, "Конфиг не найден."
        if u.get("balance", 0) < price:
            return False, f"На балансе {u.get('balance', 0)} ₽ — не хватает на {price} ₽."
        u["balance"] -= price
        pid = str(state["next_payment"])
        state["next_payment"] += 1
        state["payments"][pid] = {"user": str(uid), "target": target, "status": "approved", "via": "balance",
                                  "price": price, "days": days, "created": int(time.time())}
        save_state(state)
    result = await grant(int(uid), target, days)
    for admin in ADMINS:
        try:
            await ADMIN_BOT.send_message(admin, f"💰 Оплата с баланса №{pid} — {price} ₽\n{u['name']}\nID {uid}\n"
                                                f"За: {target_label(state, target)} на {days} дней\n{result}")
        except Exception:
            pass
    return True, f"💰 Оплачено с баланса: {price} ₽. Осталось {u['balance']} ₽."


async def pay_from_balance(query, ctx, target, months):
    """Кнопка «Оплатить с баланса» — сразу, без проверки администратором."""
    ok, text = await buy_with_balance(query.from_user.id, target, months)
    if not ok and "не хватает" in text:
        text += f" Пополнить: «{BTN_BALANCE}»."
    await query.edit_message_text(text)


# ---------- баланс и приглашения ----------

def balance_screen(state, uid):
    u = state["users"].get(str(uid), {})
    n = u.get("ref_count", 0)
    try:
        link = f"https://t.me/{USER_BOT.username}?start=ref_{uid}"
    except Exception:  # бот ещё не знает своё имя
        link = ""
    text = (f"💰 Баланс: {u.get('balance', 0)} ₽\n"
            f"Пополнение — от {TOPUP_MIN} ₽, баланс тратится на покупку и продление конфигов.\n\n"
            f"👥 Пригласите {REF_NEED} друзей — получите {REF_DAYS} дней бесплатно. Друг считается, "
            f"когда оплатит VPN.\nОплатили: {n % REF_NEED}/{REF_NEED} (всего друзей: {n}).\n\n"
            f"Ваш ID: <code>{uid}</code>" + (f"\nСсылка для друга: {link}" if link else ""))
    rows = [[InlineKeyboardButton("➕ Пополнить баланс", callback_data="top:")]]
    if not u.get("ref") and not any(p["user"] == str(uid) and p["status"] == "approved"
                                    for p in state["payments"].values()):
        rows.append([InlineKeyboardButton("✏️ Меня пригласил друг", callback_data="refset:")])
    return text, InlineKeyboardMarkup(rows)


async def balance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not await gate(update, ctx, state):
        return
    text, buttons = balance_screen(state, update.effective_user.id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=buttons, disable_web_page_preview=True)


TOPUP_AMOUNTS = (150, 300, 500, 1000)


async def topup_screen(message, ctx, amount, query=None, then=None):
    """Реквизиты для пополнения. then — (за что, месяцев): после пополнения бот сам оплатит это с баланса."""
    ctx.user_data["pay_for"] = TOPUP
    ctx.user_data["pay_amount"] = amount
    then = then or ctx.user_data.get("pay_then")
    if then:
        ctx.user_data["pay_then"] = then
    ctx.user_data.pop("await", None)
    after = f"\n\nПосле пополнения бот сам оплатит: {target_label(load_state(), then[0])}." if then else ""
    text = (f"➕ Пополнение баланса на {amount} ₽.\n\n{pay_text()}\n\n"
            f"Переведите ровно {amount} ₽ и пришлите сюда скриншот перевода одним фото.{after}")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Другая сумма", callback_data="top:")]])
    if query:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def referral_credit(uid):
    """Друг (uid) впервые оплатил VPN: засчитываем пригласившему; за каждых REF_NEED — подарок."""
    async with lock:
        state = load_state()
        u = state["users"].get(str(uid), {})
        rid = u.get("ref")
        if not rid or u.get("ref_counted") or rid not in state["users"]:
            return
        u["ref_counted"] = int(time.time())
        r = state["users"][rid]
        r["ref_count"] = r.get("ref_count", 0) + 1
        n = r["ref_count"]
        gift = ""
        if n % REF_NEED == 0:
            old_state = json.loads(json.dumps(state))
            mine = [(k, p) for k, p in user_peers(state, rid).items() if p.get("kind") not in ROUTER_KINDS]
            # Подарок — к конфигу, который кончается раньше всех; нет конфигов — на баланс.
            if mine:
                pub, peer = min(mine, key=lambda kv: kv[1]["until"] if peer_active(kv[1]) else float("inf"))
                peer["until"] = max(peer["until"], time.time()) + REF_DAYS * DAY
                peer["warned"] = 0
                apply_or_rollback(state, old_state)
                gift = f"🎁 Подарок: {title(peer)} продлён на {REF_DAYS} дней, до {fmt_date(peer['until'])}."
            else:
                r["balance"] = r.get("balance", 0) + PRICE
                gift = f"🎁 Подарок: {PRICE} ₽ на баланс — это месяц VPN."
        save_state(state)
    left = REF_NEED - n % REF_NEED
    text = (f"👥 Ваш друг {u.get('name', uid)} оплатил VPN — засчитан ({n % REF_NEED or REF_NEED}/{REF_NEED}).\n"
            + (gift if gift else f"Ещё {left} — и {REF_DAYS} дней бесплатно."))
    try:
        await USER_BOT.send_message(int(rid), text, reply_markup=KEYBOARD)
    except Exception:
        pass
    if gift:
        for admin in ADMINS:
            try:
                await ADMIN_BOT.send_message(admin, f"👥 {user_name(state, rid)} (ID {rid}) пригласил {n} друзей. {gift}")
            except Exception:
                pass


async def buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if not await gate(update, ctx, state):
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
        user.id, f"🎁 Пробный период до {fmt_date(peer['until'])}.\n"
                 f"Понравится — «{BTN_LIST}» → «🔄 Продлить», этот же конфиг продолжит работать.",
        reply_markup=KEYBOARD)
    await send_peer(user.id, peer)
    for admin in ADMINS:
        try:
            await ADMIN_BOT.send_message(admin, f"🎁 Пробный период: {u['name']}, ID {user.id}, "
                                                f"{title(peer)} до {fmt_date(peer['until'])}")
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


async def remember_os(user, os_):
    async with lock:
        state = load_state()
        get_user(state, user)["os"] = os_
        save_state(state)
    return state


async def remember_op(user, op):
    async with lock:
        state = load_state()
        get_user(state, user)["op"] = op
        save_state(state)
    return state


def _buy_screen(guide, target, price, back):
    """Экран «инструкция + купить»: подробные шаги для выбранного устройства и кнопка оплаты."""
    return (GUIDES[guide], InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Купить — {price} ₽ за {DAYS} дней" if target != NEW else
                              f"💳 Купить — {PRICE} ₽/мес или {PRICE3} ₽ за 3 мес",
                              callback_data=f"buy:{target}:{guide}" if guide in OS_NAMES else f"buy:{target}")],
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
    if not await gate(update, ctx, state):
        return
    target = ctx.user_data.pop("pay_for", None)
    if target is None:
        # Оплату принимаем только после выбора, за что она.
        await msg.reply_text(
            "Чтобы оплатить, сначала нажмите «Пополнить»:\n"
            f"• новый конфиг — «{BTN_BUY}» → устройство → «💳 Купить» → срок → «➕ Пополнить»;\n"
            f"• продление — «{BTN_LIST}» → конфиг → «🔄 Продлить» → «➕ Пополнить»;\n"
            f"• просто баланс — «{BTN_BALANCE}» → «➕ Пополнить баланс».\n"
            "После этого пришлите скриншот перевода.\n\n"
            f"Если это скриншот проблемы — нажмите «{BTN_SUPPORT}» и пришлите его там.",
            reply_markup=KEYBOARD)
        return
    months = ctx.user_data.pop("pay_months", 1)
    amount = ctx.user_data.pop("pay_amount", None)
    then = ctx.user_data.pop("pay_then", None) if target == TOPUP else None
    await submit_payment(update.effective_user, target, data, msg.caption, msg, months, amount, then)


async def submit_payment(user, target, data, comment, reply_to, months=1, amount=None, then=None):
    async with lock:
        state = load_state()
        u = get_user(state, user)
        pid = str(state["next_payment"])
        state["next_payment"] += 1
        if target == TOPUP:
            price, days = int(amount or TOPUP_MIN), 0
        else:
            months, days, price = plan(state, target, months)
        state["payments"][pid] = {"user": str(user.id), "target": target, "status": "pending",
                                  "price": price, "days": days, "created": int(time.time())}
        if then:
            state["payments"][pid]["then"] = list(then)
        save_state(state)

    term = f" на {days} дней" if days else ""
    if then:
        term += f"\nПосле пополнения — оплата: {target_label(state, then[0])}"
    caption = (f"Оплата №{pid} — {price} ₽\n{u['name']}\nID {user.id}\n"
               f"За: {target_label(state, target)}{term}"
               + ("\n⚠️ Проверьте, что сумма на скриншоте совпадает." if target == TOPUP else ""))
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


def config_card(state, pub, p, back=True):
    """Экран одного конфига: что он такое и все действия с ним."""
    row = [InlineKeyboardButton("⬇️ Скачать", callback_data=f"get:{pub}")]
    if not is_admin(p["user"]):
        row.append(InlineKeyboardButton(f"🔄 Продлить — {price_for(state, pub)} ₽", callback_data=f"renew:{pub}"))
    rows = [row, [InlineKeyboardButton("🏓 Пинг", callback_data=f"png:{pub}"),
                  InlineKeyboardButton("✏️ Название", callback_data=f"ren:{pub}")]]
    if p.get("kind") not in ROUTER_KINDS and os.environ.get("OP_PROFILES", "0") == "1":
        rows.append([InlineKeyboardButton(f"📶 Оператор: {OPERATORS.get(p.get('op'), 'не выбран')}",
                                          callback_data=f"opc:{pub}")])
    if p.get("kind") not in ROUTER_KINDS:
        rows.append([InlineKeyboardButton(f"🔀 Режим: {MODES[mode_of(p)]}", callback_data=f"mode:{pub}")])
    if ADBLOCK_DNS and p.get("kind") not in ROUTER_KINDS and mode_of(p) != "game":
        rows.append([InlineKeyboardButton("🛡 Реклама: " + ("блокируется" if ads_blocked(p) else "не блокируется"),
                                          callback_data=f"ads:{pub}")])
    rows.append([InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{pub}")])
    if back:
        rows.append([InlineKeyboardButton("⬅️ Все конфиги", callback_data="cfgl:")])
    icon = "📡" if p.get("kind") in ROUTER_KINDS else "📱"
    device = f"\n{OS_NAMES[p['os']]}" if p.get("os") in OS_NAMES else ""
    return f"{icon} {title(p)} — {peer_status(p)}{device}", InlineKeyboardMarkup(rows)


def config_list(state, uid):
    """Список конфигов кнопками: выбрал — открылся экран конфига в том же сообщении."""
    mine = sorted(user_peers(state, uid).items(), key=lambda kv: kv[1]["name"])
    rows = []
    for pub, p in mine:
        icon = "📡" if p.get("kind") in ROUTER_KINDS else "📱"
        mark = "🟢" if peer_active(p) else "⚪️"
        rows.append([InlineKeyboardButton(f"{mark} {icon} {title(p)}", callback_data=f"cfg:{pub}")])
    return "📄 Ваши конфиги — выберите, какой изменить или скачать:", InlineKeyboardMarkup(rows)


async def list_configs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    mine = user_peers(state, update.effective_user.id)
    if not mine:
        await update.message.reply_text(f"Конфигов пока нет. Нажмите «{BTN_BUY}».")
        return
    if len(mine) == 1:
        pub, p = next(iter(mine.items()))
        text, buttons = config_card(state, pub, p, back=False)
    else:
        text, buttons = config_list(state, update.effective_user.id)
    await update.message.reply_text(text, reply_markup=buttons)


def news_screen(show_all=False):
    """«Что нового»: последние 3 записи или все (сколько влезет в сообщение) и кнопка переключения."""
    news = load_state()["news"][::-1]
    if not news:
        return "Пока обновлений не было.", None
    shown = news if show_all else news[:3]
    text = "🆕 Что нового\n\n"
    for n in shown:
        item = f"📅 {fmt_day(n['date'])}\n{n['text']}\n\n"
        if len(text) + len(item) > 3900:
            break
        text += item
    if len(news) <= 3:
        return text.strip(), None
    button = (InlineKeyboardButton("🔼 Только последние", callback_data="newsv:short") if show_all else
              InlineKeyboardButton(f"📜 Показать все ({len(news)})", callback_data="newsv:all"))
    return text.strip(), InlineKeyboardMarkup([[button]])


async def news_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, buttons = news_screen()
    await update.message.reply_text(text, reply_markup=buttons or KEYBOARD)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=HELP_BUTTONS)


# ---------- опросы ----------

def poll_results(poll, mine=None):
    """Результаты опроса с процентами и полосками; mine — номер варианта, за который голосовал этот человек."""
    votes = list(poll["votes"].values())
    total = len(votes)
    lines = []
    for i, opt in enumerate(poll["opts"]):
        n = votes.count(i)
        pct = round(100 * n / total) if total else 0
        bar = "▓" * round(pct / 10) + "░" * (10 - round(pct / 10))
        lines.append(f"{'✅ ' if i == mine else ''}{opt}\n{bar} {pct}% ({n})")
    return "\n\n".join(lines) + f"\n\nВсего голосов: {total}" + (" · опрос закрыт" if poll.get("closed") else "")


def poll_buttons(pid, poll):
    if poll.get("closed"):
        return None
    rows = [[InlineKeyboardButton(opt, callback_data=f"pv:{pid}:{i}")] for i, opt in enumerate(poll["opts"])]
    rows.append([InlineKeyboardButton("🔄 Обновить результаты", callback_data=f"pv:{pid}:r")])
    return InlineKeyboardMarkup(rows)


async def on_poll_vote(query, arg):
    """Голос клиента: записываем и показываем ему общие результаты в процентах. Голос можно поменять."""
    pid, _, choice = arg.partition(":")
    uid = str(query.from_user.id)
    async with lock:
        state = load_state()
        poll = state["polls"].get(pid)
        if not poll:
            await query.answer("Опрос не найден.")
            return
        if choice.isdigit() and not poll.get("closed") and int(choice) < len(poll["opts"]):
            poll["votes"][uid] = int(choice)
            save_state(state)
    await query.answer("Голос учтён" if choice.isdigit() and not poll.get("closed") else "")
    text = f"🗳 {poll['q']}\n\n" + poll_results(poll, poll["votes"].get(uid))
    try:
        await query.edit_message_text(text, reply_markup=poll_buttons(pid, poll))
    except Exception:
        pass  # результаты не изменились — Telegram не даёт отредактировать сообщение на тот же текст


async def on_user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action, _, arg = query.data.partition(":")
    await query.answer()

    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_reply_markup(None)  # старше 48 часов — хотя бы убрать кнопки
        return

    if action == "dl":
        how, _, pub = arg.partition(":")
        peer = load_state()["peers"].get(pub)
        if not peer or peer["user"] != str(uid):
            await query.edit_message_text("Конфиг не найден.")
            return
        if not peer_active(peer):
            await query.edit_message_text(f"{title(peer)}: срок закончился, сначала продлите его.")
            return
        if how == "and":
            await query.edit_message_text(f"🤖 {title(peer)}: как добавить?\n"
                                          "🔑 Ключ — для AmneziaVPN, проще всего.\n"
                                          "📄 Файл — для AmneziaWG.",
                                          reply_markup=android_buttons(pub))
        elif how == "allm":
            await query.edit_message_text(f"📦 {title(peer)}: все режимы сразу. Как прислать?",
                                          reply_markup=all_modes_buttons(pub))
        elif how == "back":
            await query.edit_message_text(f"📥 {title(peer)}: на каком устройстве подключаете?",
                                          reply_markup=download_buttons(pub))
        elif how in ("ios", "key", "file", "qr", "amk", "amq", "amf"):
            # Сначала экран конфига (с пометкой), потом файлы под ним.
            await show_card(query, pub, "✅ Отправил ниже 👇")
            await deliver_peer(query.message.chat_id, peer, how)
        return

    if action == "rsn":
        await query.edit_message_text("Хорошо, ваши конфиги остаются как есть.")
        return
    if action in ("rsy", "rsp") and arg.partition(":")[0] in MODES:
        mode, _, pub = arg.partition(":")
        mine = device_peers(load_state(), uid)
        if not mine:
            await query.edit_message_text("Действующих конфигов нет — сначала продлите конфиг.")
            return
        if action == "rsy" and len(mine) > 1:
            rows = [[InlineKeyboardButton(f"📱 {title(p)} — сейчас {MODES[mode_of(p)]}", callback_data=f"rsp:{mode}:{k}")]
                    for k, p in mine.items()]
            await query.edit_message_text(f"Для какого конфига прислать «{MODES[mode]}»?",
                                          reply_markup=InlineKeyboardMarkup(rows))
            return
        pub = pub if pub in mine else next(iter(mine))
        async with lock:
            state = load_state()
            if pub in state["peers"]:
                state["peers"][pub]["mode"] = mode
                save_state(state)
        await query.edit_message_text(f"✅ {title(mine[pub])}: режим «{MODES[mode]}». На каком устройстве подключаете?",
                                      reply_markup=download_buttons(pub))
        return

    if action == "kdone":
        # «✅ Готово» под ключом: убираем и ключ, и подсказку — ключ в чате больше не нужен.
        ids = [query.message.message_id] + [int(i) for i in arg.split(",") if i.isdigit()]
        try:
            await ctx.bot.delete_messages(query.message.chat_id, ids)
        except Exception:
            pass
        return

    if action == "cfgl":
        state = load_state()
        text, buttons = config_list(state, uid)
        try:
            await query.edit_message_text(text, reply_markup=buttons)
        except Exception:
            pass
        return

    if action == "newsv":
        text, buttons = news_screen(show_all=arg == "all")
        try:
            await query.edit_message_text(text, reply_markup=buttons)
        except Exception:
            pass
        return

    if action == "refresh":
        # «🔄 Обновить меню» под оповещением: присылаем свежую клавиатуру с кнопками.
        await query.message.reply_text("Меню обновлено 👇", reply_markup=KEYBOARD)
        return

    if action == "pv":
        await on_poll_vote(query, arg)
        return

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

    if action == "trial":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("На каком устройстве будете пользоваться? Конфиг настроится под него.",
                                       reply_markup=InlineKeyboardMarkup(
                                           [[InlineKeyboardButton(n, callback_data=f"tos:{k}")] for k, n in OS_NAMES.items()]))
        return

    if action == "tos" and arg in OS_NAMES:
        await remember_os(query.from_user, arg)
        await start_trial(query, ctx)
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

    if action == "mset":
        mode, _, pub = arg.partition(":")
        async with lock:
            state = load_state()
            p = state["peers"].get(pub)
            if not p or p["user"] != str(uid) or mode not in MODES:
                await query.edit_message_text("Конфиг не найден.")
                return
            if mode == "full":
                p.pop("mode", None)
            else:
                p["mode"] = mode
            save_state(state)
        await show_download(query, pub, f"✅ Режим: {MODES[mode]}."
                            + (" Работают игры, Telegram, Discord и YouTube." if mode == "game" else
                               " zapret на время VPN выключите." if mode == "pcgame" else ""))
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
        await show_download(query, pub, f"✅ Оператор: {OPERATORS[op]}.")
        return

    if action == "help":
        # Инструкция открывается в том же сообщении, а не новым — чат не засоряется.
        try:
            await query.edit_message_text(GUIDES.get(arg, HELP_TEXT), reply_markup=HELP_BUTTONS,
                                          parse_mode="HTML" if arg == "mode" else None)
        except Exception:
            pass  # та же инструкция уже открыта
        return

    if action == "buy":
        state = load_state()
        if not is_approved(state, uid):
            await query.edit_message_text(closed_text(state, uid))
            return
        await query.edit_message_reply_markup(reply_markup=None)  # инструкция остаётся на экране
        target, _, os_ = arg.partition(":")
        if os_ in OS_NAMES:
            state = await remember_os(query.from_user, os_)
        await ask_payment(query.message, ctx, target, state)
        return

    if action == "per":
        m, _, target = arg.partition(":")
        await pay_screen(query.message, ctx, target, int(m), load_state(), query)
        return
    if action == "pln":
        await ask_payment(query.message, ctx, arg, load_state(), query=query)
        return
    if action == "bal":
        m, _, target = arg.partition(":")
        await pay_from_balance(query, ctx, target, int(m))
        return
    if action in ("top", "topb"):
        need = 0
        if action == "topb":
            # «Пополнить» с экрана оплаты: после пополнения бот сам оплатит эту покупку.
            m, _, target = arg.partition(":")
            state = load_state()
            months, days, price = plan(state, target, int(m))
            ctx.user_data["pay_then"] = (target, months)
            need = price - balance(state, uid)
        elif query.message.text and query.message.text.startswith("💰 Баланс"):
            ctx.user_data.pop("pay_then", None)
        ctx.user_data["await"] = "topup"
        amounts = [a for a in TOPUP_AMOUNTS if a >= TOPUP_MIN]
        rows = [[InlineKeyboardButton(f"{a} ₽", callback_data=f"topa:{a}") for a in amounts[i:i + 2]]
                for i in range(0, len(amounts), 2)]
        if action == "topb":
            rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"per:{arg}")])
        hint = f"\nДля этой оплаты нужно не меньше {need} ₽." if need > TOPUP_MIN else ""
        await query.edit_message_text(f"➕ На сколько пополнить?{hint}\nНажмите сумму или напишите свою числом "
                                      f"(от {TOPUP_MIN} ₽).", reply_markup=InlineKeyboardMarkup(rows))
        return
    if action == "topa":
        await topup_screen(query.message, ctx, int(arg), query)
        return
    if action == "refset":
        ctx.user_data["await"] = "ref_set"
        await query.edit_message_text("✏️ Пришлите ID друга, который вас пригласил, — одним числом.")
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
        await ask_payment(query.message, ctx, arg, state, query=query)
    elif action == "ren":
        ctx.user_data["await"] = f"rename:{arg}"
        ctx.user_data["rename_msg"] = (query.message.chat_id, query.message.message_id)
        await query.edit_message_text(
            f"✏️ Напишите новое название для {title(peer)}, например «Телефон мамы».\n"
            "Латиницей без пробелов (mama-phone) — так же будет называться и туннель в приложении.",
            reply_markup=InlineKeyboardMarkup([[back_button(arg)]]))
    elif action == "cfg":
        many = len(user_peers(state, uid)) > 1
        text, buttons = config_card(state, arg, peer, back=many)
        await query.edit_message_text(text, reply_markup=buttons)
    elif action == "png":
        await query.edit_message_text(await check_peer(arg, peer), reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Ещё раз", callback_data=f"png:{arg}"), back_button(arg)]]))
    elif action == "mode":
        # Три кнопки: весь трафик, только заблокированное, игровой; игровой — дальше телефон или ПК.
        cur = mode_of(peer)
        mark = lambda on: "✅ " if on else ""
        await query.edit_message_text(
            f"🔀 {html.escape(title(peer))}: сейчас «{MODES[cur]}».\n\n" + MODE_TEXT + ("" if SMART_FULL else "\n\n" + SMART_LIST),
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(mark(cur == "smart") + MODES["smart"], callback_data=f"mset:smart:{arg}")],
                [InlineKeyboardButton(mark(cur == "full") + MODES["full"], callback_data=f"mset:full:{arg}")],
                [InlineKeyboardButton(mark(cur in ("game", "pcgame")) + "🎮 Игровой", callback_data=f"mgame:{arg}")],
                [back_button(arg)],
            ]))
    elif action == "mgame":
        cur = mode_of(peer)
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(("✅ " if cur == "game" else "") + "📱 Для телефона", callback_data=f"mset:game:{arg}")],
            [InlineKeyboardButton(("✅ " if cur == "pcgame" else "") + "💻 Для ПК", callback_data=f"mset:pcgame:{arg}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"mback:{arg}")],
        ]))
    elif action == "mback":
        cur = mode_of(peer)
        mark = lambda on: "✅ " if on else ""
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(mark(cur == "smart") + MODES["smart"], callback_data=f"mset:smart:{arg}")],
            [InlineKeyboardButton(mark(cur == "full") + MODES["full"], callback_data=f"mset:full:{arg}")],
            [InlineKeyboardButton(mark(cur in ("game", "pcgame")) + "🎮 Игровой", callback_data=f"mgame:{arg}")],
            [back_button(arg)],
        ]))
    elif action == "ads":
        async with lock:
            state = load_state()
            p = state["peers"].get(arg)
            if p.get("ads") == "off":
                p.pop("ads")
            else:
                p["ads"] = "off"
            save_state(state)
        await show_download(query, arg, "✅ Реклама " + ("блокируется." if ads_blocked(p) else "не блокируется."))
    elif action == "opc":
        buttons = op_picker("ops", f":{arg}")
        await query.edit_message_text(f"📶 {title(peer)}: какой у вас оператор?", reply_markup=InlineKeyboardMarkup(
            [*buttons.inline_keyboard, [back_button(arg)]]))
    elif action == "get":
        if not peer_active(peer):
            await show_card(query, arg, "Срок закончился — сначала продлите.")
            return
        await query.edit_message_text(f"📥 {title(peer)}: на каком устройстве подключаете?",
                                      reply_markup=download_buttons(arg))
    elif action == "del":
        await query.edit_message_text(
            f"Удалить {title(peer)}? Оплаченные дни не вернутся.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Да, удалить", callback_data=f"delyes:{arg}"),
                InlineKeyboardButton("Отмена", callback_data=f"delno:{arg}"),
            ]]))
    elif action == "delno":
        await show_card(query, arg)
    elif action == "delyes":
        async with lock:
            state = load_state()
            old_state = json.loads(json.dumps(state))
            state["peers"].pop(arg, None)
            apply_or_rollback(state, old_state)
            save_state(state)
        state = load_state()
        if user_peers(state, uid):
            text, buttons = config_list(state, uid)
            await query.edit_message_text(f"🗑 {title(peer)} удалён.\n\n{text}", reply_markup=buttons)
        else:
            await query.edit_message_text(f"🗑 {title(peer)} удалён.")


# ---------- админ-бот ----------

ABTN_PAY = "⏳ Оплаты"
ABTN_USERS = "👥 Клиенты"
ABTN_GIVE = "💰 Выдать деньги"
ABTN_STATS = "📊 Статистика"
ABTN_RESEND = "📤 Разослать конфиги"
ABTN_NEWS = "📢 Оповестить об обновлении"
ABTN_TICKETS = "📨 Жалобы"
ABTN_POLL = "🗳 Опрос"
ABTN_SERVERS = "🖥 Серверы"
ADMIN_KEYBOARD = ReplyKeyboardMarkup([[ABTN_PAY, ABTN_TICKETS], [ABTN_USERS, ABTN_GIVE], [ABTN_STATS, ABTN_RESEND],
                                      [ABTN_NEWS, ABTN_POLL], [ABTN_SERVERS, BTN_CLEAR]],
                                     resize_keyboard=True)


def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and is_admin(update.effective_user.id):
            await func(update, ctx)
    return wrapper


def payment_buttons(pid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"ok:{pid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{pid}"),
    ]])


def access_buttons(uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Пустить", callback_data=f"acc:yes:{uid}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"acc:no:{uid}"),
    ]])


def user_buttons(uid):
    """Кнопки под карточкой клиента; если конфигов несколько, бот спросит, с каким из них работать."""
    copy = InlineKeyboardButton(f"📋 ID {uid}", copy_text=CopyTextButton(str(uid)))
    back = [InlineKeyboardButton("⬅️ К списку", callback_data="usrb:")]
    if is_admin(uid):
        return InlineKeyboardMarkup([[copy], back])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Продлить", callback_data=f"uact:ext:{uid}"),
        InlineKeyboardButton("⛔️ Отключить", callback_data=f"uact:take:{uid}"),
    ], [
        InlineKeyboardButton("🏓 Пинг", callback_data=f"uact:png:{uid}"),
        InlineKeyboardButton("💰 Деньги", callback_data=f"money:{uid}"),
    ], [copy], back])


def user_card(state, uid, hs=None):
    """Одно сообщение на клиента: все его конфиги со сроками, подключением и оператором."""
    hs = last_handshakes() if hs is None else hs
    lines = [f"{user_name(state, uid)} · ID {uid}"]
    u = state["users"].get(str(uid), {})
    extra = []
    if u.get("balance"):
        extra.append(f"💰 {u['balance']} ₽")
    if u.get("ref_count"):
        extra.append(f"👥 привёл {u['ref_count']}")
    if u.get("ref"):
        extra.append(f"пришёл от {user_name(state, u['ref'])} (ID {u['ref']})")
    if extra:
        lines.append(" · ".join(extra))
    for pub, p in sorted(user_peers(state, uid).items(), key=lambda kv: kv[1]["name"]):
        icon = "📡" if p.get("kind") in ROUTER_KINDS else "📱"
        lines.append(f"\n{icon} {title(p)} — {peer_status(p)}")
        extra = []
        if peer_active(p):
            h = hs.get(pub, 0)
            extra.append("🔌 подключён сейчас" if is_online(h)
                         else f"💤 не подключён, последний раз: {fmt_date(h) if h else 'ни разу'}")
        if p.get("op") in OPERATORS:
            extra.append(f"📶 {OPERATORS[p['op']]}")
        if extra:
            lines.append("    " + " · ".join(extra))
    return "\n".join(lines)


def extend_buttons(pub):
    """На сколько продлить: быстрые варианты или своё число дней."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"+{d} дн.", callback_data=f"exd:{d}:{pub}") for d in (7, 30, 90, 365)],
        [InlineKeyboardButton("✏️ Своё число дней", callback_data=f"exc:{pub}"),
         InlineKeyboardButton("⬅️ Назад", callback_data=f"exb:{pub}")],
    ])


async def extend_peer(pub, days):
    """Продление конфига администратором на любое число дней (клиенту приходит уведомление)."""
    peer = load_state()["peers"].get(pub)
    if not peer:
        return "Конфиг удалён."
    return await grant(int(peer["user"]), pub, days, paid=False)


async def check_peer(pub, peer):
    """Проверка связи с конфигом: когда было рукопожатие, трафик и пинг от сервера до устройства."""
    iface = peer_iface(peer)[0]
    handshake = rx = tx = 0
    for line in run(["awg", "show", iface, "dump"]).splitlines()[1:]:
        f = line.split("\t")
        if f[0] == pub:
            handshake, rx, tx = int(f[4]), int(f[5]), int(f[6])
    head = f"🏓 {title(peer)}\n"
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
            await USER_BOT.send_message(uid, f"{head} Ваш новый конфиг {title(peer)} "
                                             f"активен до {fmt_date(peer['until'])}:", reply_markup=KEYBOARD)
            await send_peer(uid, peer)
        else:
            await USER_BOT.send_message(uid, f"{head} {title(peer)} продлён до "
                                             f"{fmt_date(peer['until'])}. Если он уже есть в приложении — "
                                             "просто включите его.", reply_markup=KEYBOARD)
        sent = ""
    except Exception:
        log.exception("не удалось отправить клиенту %s", uid)
        sent = "\nКлиенту написать не удалось — он ещё не нажимал /start в клиентском боте."
    if paid:
        try:
            await referral_credit(uid)
        except Exception:
            log.exception("приглашение не засчиталось")
    return f"{title(peer)}: {peer_status(peer)}{sent}"


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
        f"{ABTN_USERS} — клиенты и их конфиги: продлить, отключить, удалить неоплаченные; кто ждёт решения\n"
        f"{ABTN_GIVE} — начислить (или списать) деньги на баланс клиента по ID\n"
        f"{ABTN_STATS} — кто подключён и трафик\n"
        f"{ABTN_RESEND} — отправить всем клиентам актуальные файлы их конфигов\n"
        f"{ABTN_NEWS} — написать всем клиентам, что нового (попадёт в «{BTN_NEWS}»)\n"
        f"{ABTN_TICKETS} — обращения клиентов: ответить, дать конфиг, вернуть деньги, отклонить\n\n"
        "Команды: /send ID текст — написать клиенту, /broadcast текст — написать всем.",
        reply_markup=ADMIN_KEYBOARD)


@admin_only
async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_waiting(update.message)


async def send_waiting(message):
    """Запросы доступа и непроверенные оплаты — отдельными сообщениями с кнопками решения."""
    state = load_state()
    waiting, pending = waiting_items(state)
    for uid in waiting:
        await message.reply_text(f"🙋 Запрос доступа\n{user_name(state, uid)}\nID {uid}",
                                        reply_markup=access_buttons(uid))
    if not pending and not waiting:
        await message.reply_text("Никто не ждёт: запросов доступа и неразобранных оплат нет.")
        return
    for pid, p in pending:
        caption = (f"Оплата №{pid} от {fmt_date(p['created'])}\n{user_name(state, p['user'])}\nID {p['user']}\n"
                   f"За: {target_label(state, p.get('target', NEW))}")
        if p.get("photo"):
            await message.reply_photo(p["photo"], caption=caption, reply_markup=payment_buttons(pid))
        else:
            await message.reply_text(caption, reply_markup=payment_buttons(pid))


def last_handshakes():
    """Время последнего рукопожатия каждого конфига на сервере: {публичный ключ: unix-время, 0 — ни разу}."""
    result = {}
    for iface in [IFACE] + ([ROUTER_IFACE] if plain_routers() else []):
        try:
            dump = run(["awg", "show", iface, "dump"])
        except Exception:
            continue
        for line in dump.splitlines()[1:]:
            f = line.split("\t")
            result[f[0]] = int(f[4])
    return result


def is_online(handshake):
    # Пока туннель включён, рукопожатие повторяется каждые 2 минуты.
    return bool(handshake) and time.time() - handshake < 180


def user_groups(state):
    """Конфиги по группам: действующие (подключены сейчас и нет), закончившиеся или неоплаченные;
    и клиенты без конфигов."""
    active = sorted(((k, p) for k, p in state["peers"].items() if peer_active(p)), key=lambda kv: kv[1]["until"])
    hs = last_handshakes()
    off = sorted(((k, p) for k, p in state["peers"].items() if not peer_active(p)), key=lambda kv: -kv[1]["until"])
    with_peers = {p["user"] for p in state["peers"].values()}
    none = [uid for uid in state["users"] if uid not in with_peers and not is_admin(uid)]
    return {"act": active, "on": [kv for kv in active if is_online(hs.get(kv[0], 0))],
            "idle": [kv for kv in active if not is_online(hs.get(kv[0], 0))], "off": off, "none": none, "hs": hs}


def waiting_items(state):
    """Кто ждёт решения админа: запросы доступа и непроверенные оплаты."""
    pending = [(pid, p) for pid, p in state["payments"].items() if p["status"] == "pending"]
    waiting = [uid for uid, u in state["users"].items()
               if u.get("requested") and "approved" not in u and not is_approved(state, uid)]
    return waiting, pending


AUTODEL_DAYS = 5


def stale_peers(state, days=0):
    """Неоплаченные и закончившиеся конфиги, которые можно удалить: срок кончился больше days дней назад
    (неоплаченные — созданы больше days дней назад). Конфиги с непроверенной оплатой продления не трогаем."""
    now = time.time()
    paying = {p.get("target") for p in state["payments"].values() if p["status"] == "pending"}
    result = []
    for pub, p in state["peers"].items():
        if is_admin(p["user"]) or peer_active(p) or pub in paying:
            continue
        since = p["until"] or p.get("made", 0)
        if days == 0 or (since and now - since >= days * DAY):
            result.append(pub)
    return result


def remove_peers(state, pubs):
    for pub in pubs:
        state["peers"].pop(pub, None)


def users_menu(state):
    g = user_groups(state)
    waiting, pending = waiting_items(state)
    rows = []
    if waiting or pending:
        rows.append([InlineKeyboardButton(f"🙋 Ждут решения ({len(waiting) + len(pending)})", callback_data="usrw:")])
    rows += [
        [InlineKeyboardButton(f"🟢 Действуют ({len(g['act'])})", callback_data="usr:act")],
        [InlineKeyboardButton(f"⚪️ Закончились / не оплачены ({len(g['off'])})", callback_data="usr:off")],
        [InlineKeyboardButton(f"👤 Без конфига ({len(g['none'])})", callback_data="usr:none")],
    ]
    return f"👥 Клиентов: {len(state['users'])}. Кого показать?", InlineKeyboardMarkup(rows)


def cleanup_buttons(state):
    auto = state["settings"].get("autodel")
    stale = stale_peers(state)
    rows = []
    if stale:
        rows.append([InlineKeyboardButton(f"🗑 Удалить все неоплаченные ({len(stale)})", callback_data="cln:ask")])
    rows.append([InlineKeyboardButton(
        f"⏰ Автоудаление через {AUTODEL_DAYS} дн.: {'✅ вкл' if auto else '❌ выкл'}", callback_data="cln:auto")])
    return rows


@admin_only
async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, buttons = users_menu(load_state())
    await update.message.reply_text(text, reply_markup=buttons)


USERS_PAGE = 15


def group_users(state, groups, group):
    """ID клиентов группы в порядке их конфигов (у кого срок кончается раньше — выше)."""
    if group == "none":
        return list(groups["none"])
    return list(dict.fromkeys(p["user"] for _, p in groups["act" if group == "all" else group]))


async def show_user_group(query, ctx, group, page=0):
    """Список клиентов группы кнопками — в том же сообщении, по USERS_PAGE на страницу."""
    state = load_state()
    groups = user_groups(state)
    if group == "act":
        await query.edit_message_text(f"🟢 Действуют: {len(groups['act'])}. Кого показать?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔌 Подключены сейчас ({len(groups['on'])})", callback_data="usr:on")],
            [InlineKeyboardButton(f"💤 Не подключены ({len(groups['idle'])})", callback_data="usr:idle")],
            [InlineKeyboardButton(f"📋 Все действующие ({len(groups['act'])})", callback_data="usr:all")],
            [InlineKeyboardButton("⬅️ Группы", callback_data="usrg:")],
        ]))
        return
    ctx.user_data["usr_group"] = (group, page)
    uids = group_users(state, groups, group)
    names = {"on": "🔌 Подключены сейчас", "idle": "💤 Не подключены", "all": "🟢 Действуют",
             "off": "⚪️ Закончились / не оплачены", "none": "👤 Без конфига"}
    pages = max(1, (len(uids) + USERS_PAGE - 1) // USERS_PAGE)
    page = min(page, pages - 1)
    rows = []
    for uid in uids[page * USERS_PAGE:(page + 1) * USERS_PAGE]:
        peers = user_peers(state, uid)
        online = any(is_online(groups["hs"].get(k, 0)) for k in peers)
        mark = "🔌" if online else "🟢" if any(peer_active(p) for p in peers.values()) else "⚪️"
        label = f"{mark} {user_name(state, uid)[:40]}" + (f" · {len(peers)}" if len(peers) > 1 else "")
        rows.append([InlineKeyboardButton(label, callback_data=f"uc:{uid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"usrp:{group}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"usrp:{group}:{page + 1}"))
    if nav:
        rows.append(nav)
    if uids:
        rows.append([InlineKeyboardButton("📋 Все карточки сразу", callback_data=f"usra:{group}")])
    rows.append([InlineKeyboardButton("⬅️ Группы", callback_data="usrg:")])
    if group == "off":
        rows[-1:-1] = cleanup_buttons(state)
    text = f"{names.get(group, group)}: {len(uids)}" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
    await query.edit_message_text(text + ("\nВыберите клиента:" if uids else "\nЗдесь пока никого."),
                                  reply_markup=InlineKeyboardMarkup(rows))


async def send_all_cards(message, group):
    """«📋 Все карточки сразу»: карточки клиентов группы отдельными сообщениями (до 40)."""
    state = load_state()
    groups = user_groups(state)
    uids = group_users(state, groups, group)
    for uid in uids[:40]:
        await message.reply_text(user_card(state, uid, groups["hs"]), reply_markup=user_buttons(uid))
    if len(uids) > 40:
        await message.reply_text(f"Показаны первые 40 из {len(uids)}.")


@admin_only
async def admin_give_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["await"] = "give"
    await update.message.reply_text(
        "Пришлите ID клиента и сумму через пробел.\n"
        "Например: 123456789 300 — начислить 300 ₽, 123456789 -100 — списать 100 ₽.\n\n"
        "Клиенту придёт сообщение. ID видно в его оплатах и в «👥 Клиенты».")


async def give_money(uid, amount):
    """Начислить (amount > 0) или списать деньги с баланса клиента. Возвращает текст итога."""
    async with lock:
        state = load_state()
        u = state["users"].setdefault(str(uid), {"name": str(uid)})
        old = u.get("balance", 0)
        u["balance"] = max(0, old + amount)
        if amount > 0:
            u.setdefault("approved", True)  # начислили деньги — значит, пускаем
        save_state(state)
    diff = u["balance"] - old
    text = (f"🎁 Администратор начислил {diff} ₽ на баланс." if diff > 0 else
            f"Администратор списал {-diff} ₽ с баланса.") + f" Теперь {u['balance']} ₽."
    sent = ""
    if diff:
        try:
            await USER_BOT.send_message(int(uid), text, reply_markup=KEYBOARD)
        except Exception:
            sent = "\nКлиенту написать не удалось — он ещё не нажимал /start в клиентском боте."
    return f"{user_name(state, uid)}: баланс {old} → {u['balance']} ₽{sent}"


@admin_only
async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    waiting = ctx.user_data.pop("await", None)
    if waiting and waiting.startswith(("tk_reply:", "tk_give:", "tk_spam:")):
        await admin_ticket_text(update, ctx, waiting)
        return
    if waiting == "poll":
        lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
        if len(lines) < 3 or len(lines) > 11:
            ctx.user_data["await"] = "poll"
            await update.message.reply_text("Нужно: первая строка — вопрос, дальше от 2 до 10 вариантов, каждый с новой "
                                            "строки. Пришлите ещё раз.")
            return
        ctx.user_data["poll_draft"] = {"q": lines[0][:250], "opts": [o[:60] for o in lines[1:]]}
        draft = ctx.user_data["poll_draft"]
        await update.message.reply_text(
            f"Клиенты получат:\n\n🗳 {draft['q']}\n\n" + "\n".join(f"• {o}" for o in draft["opts"])
            + f"\n\nРазослать {len(load_state()['users'])} клиентам?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗳 Да, разослать", callback_data="poll:yes"),
                                                InlineKeyboardButton("Отмена", callback_data="poll:no")]]))
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
    if waiting and waiting.startswith("extend:"):
        # Своё число дней: ответ удаляем, исходное сообщение снова становится карточкой клиента.
        text = update.message.text.strip()
        pub = waiting.partition(":")[2]
        chat_id, msg_id = ctx.user_data.get("extend_msg", (update.effective_chat.id, None))
        try:
            await update.message.delete()
        except Exception:
            pass
        if not text.isdigit() or not 1 <= int(text) <= 3650:
            ctx.user_data["await"] = waiting
            note, markup = "Нужно число дней от 1 до 3650, например 45.", None
        else:
            ctx.user_data.pop("extend_msg", None)
            result = await extend_peer(pub, int(text))
            state = load_state()
            uid = state["peers"].get(pub, {}).get("user")
            note = result.split("\n")[0] + (f"\n\n{user_card(state, uid)}" if uid else "")
            markup = user_buttons(uid) if uid else None
        try:
            if msg_id:
                await ctx.bot.edit_message_text(note, chat_id=chat_id, message_id=msg_id, reply_markup=markup)
            else:
                await ctx.bot.send_message(chat_id, note, reply_markup=markup)
        except Exception:
            pass
        return
    if (waiting or "").startswith("money:"):
        amount = update.message.text.replace("₽", "").replace("+", "").strip()
        if not amount.lstrip("-").isdigit() or int(amount) == 0:
            ctx.user_data["await"] = waiting
            await update.message.reply_text("Нужно число, например 300 или -100.")
            return
        result = await give_money(waiting.partition(":")[2], int(amount))
        await update.message.reply_text(f"Готово.\n{result}", reply_markup=ADMIN_KEYBOARD)
        return
    if waiting != "give":
        await update.message.reply_text("Выберите действие на кнопках ниже.", reply_markup=ADMIN_KEYBOARD)
        return
    parts = update.message.text.replace("₽", " ").replace("+", " ").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit() or parts[1] in ("0", "-0"):
        ctx.user_data["await"] = "give"
        await update.message.reply_text("Нужно два числа: ID клиента и сумма. Например: 123456789 300")
        return
    result = await give_money(parts[0], int(parts[1]))
    await update.message.reply_text(f"Готово.\n{result}", reply_markup=ADMIN_KEYBOARD)


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return
    action, _, arg = query.data.partition(":")

    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_reply_markup(None)  # старше 48 часов — хотя бы убрать кнопки
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

    if action == "tun":
        await query.answer()
        if arg == "keep":
            TUN_STATE["snooze"] = time.time() + 6 * 3600
            await query.edit_message_text(f"{query.message.text}\n\n✅ Оставил как есть. Напомню не раньше чем через 6 часов.")
            return
        if arg == "swap" and TUN_PORT_RANGE:
            head = query.message.text

            async def progress(text):
                try:
                    await query.edit_message_text(f"{head}\n\n🔄 Подбираю порт…\n{text}")
                except Exception:
                    pass
            try:
                result = await pick_tunnel_port(progress)
            except Exception as e:
                result = f"Не получилось: {e}"
            await query.edit_message_text(f"{head}\n\n{result}")
            return
        await query.edit_message_text("Подбор порта не настроен: нужен ru-ports.sh на российском сервере.")
        return

    if action == "srv":
        await query.answer()
        text, _ = await server_status()
        try:
            await query.edit_message_text(text, reply_markup=SERVERS_BUTTON)
        except Exception:
            pass  # ничего не изменилось
        return

    if action == "poll":
        await on_admin_poll(query, ctx, arg)
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
                await USER_BOT.send_message(int(uid), news_message(ts, text), reply_markup=REFRESH_BUTTON)
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
        elif arg.startswith("m:") and arg[2:] in MODES and text:
            await query.edit_message_text(f"📤 Рассылаю: режим «{MODES[arg[2:]]}»…")
            await add_news(f"Обновление режима «{MODES[arg[2:]]}» — {text}")
            await resend_mode(query, text, arg[2:])
        elif arg == "yes" or arg.startswith("m:"):
            await query.edit_message_text(f"Текст рассылки потерялся — нажмите «{ABTN_RESEND}» ещё раз.")
        else:
            await query.edit_message_text("Рассылка отменена.")
        return

    if action == "cln":
        await query.answer()
        if arg == "ask":
            n = len(stale_peers(load_state()))
            await query.edit_message_text(
                f"Удалить {n} неоплаченных и закончившихся конфигов? Клиенты получат сообщение, "
                "что конфиг удалён; купить новый они смогут как обычно.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 Да, удалить", callback_data="cln:yes"),
                    InlineKeyboardButton("⬅️ Нет", callback_data="usr:off")]]))
            return
        if arg == "yes":
            await delete_stale(0)
        elif arg == "auto":
            async with lock:
                state = load_state()
                state["settings"]["autodel"] = not state["settings"].get("autodel")
                save_state(state)
            if state["settings"]["autodel"]:
                await delete_stale(AUTODEL_DAYS)
        await show_user_group(query, ctx, "off")
        return

    if action in ("usr", "usrp", "usrg", "usrb", "usra", "uc", "usrw"):
        await query.answer()
        if action == "usr":
            await show_user_group(query, ctx, arg)
        elif action == "usrp":
            group, _, page = arg.partition(":")
            await show_user_group(query, ctx, group, int(page))
        elif action == "usrg":
            text, buttons = users_menu(load_state())
            await query.edit_message_text(text, reply_markup=buttons)
        elif action == "usrb":
            group, page = ctx.user_data.get("usr_group", ("all", 0))
            await show_user_group(query, ctx, group, page)
        elif action == "usra":
            await send_all_cards(query.message, arg)
        elif action == "usrw":
            await query.edit_message_text("🙋 Ждут решения — ниже 👇",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                              "⬅️ Группы", callback_data="usrg:")]]))
            await send_waiting(query.message)
        else:
            state = load_state()
            if arg not in state["users"] and not user_peers(state, arg):
                await query.edit_message_text("Клиент не найден.")
                return
            await query.edit_message_text(user_card(state, arg), reply_markup=user_buttons(arg))
        return

    if action == "money":
        await query.answer()
        ctx.user_data["await"] = f"money:{arg}"
        await query.message.reply_text(f"💰 {user_name(load_state(), arg)}: сколько начислить? Пришлите число, "
                                       "например 300. Списать — с минусом: -100.")
        return

    if action == "uact":
        # Кнопка карточки клиента: один конфиг — сразу действие, несколько — выбор конфига.
        what, _, uid = arg.partition(":")
        state = load_state()
        peers = user_peers(state, uid)
        if what != "ext":
            peers = {k: p for k, p in peers.items() if peer_active(p)}
        if what == "back" or not peers:
            await query.answer("Нет подходящих конфигов." if what != "back" else "")
            await query.edit_message_text(user_card(state, uid), reply_markup=user_buttons(uid))
            return
        if len(peers) > 1:
            await query.answer()
            verb = {"ext": "продлить", "take": "отключить", "png": "проверить"}[what]
            rows = [[InlineKeyboardButton(f"{title(p)} — {peer_status(p)}", callback_data=f"{what}:{k}")]
                    for k, p in sorted(peers.items(), key=lambda kv: kv[1]["name"])]
            rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"uact:back:{uid}")])
            await query.edit_message_text(f"{user_card(state, uid)}\n\nКакой конфиг {verb}?",
                                          reply_markup=InlineKeyboardMarkup(rows))
            return
        action, arg = what, next(iter(peers))

    if action == "png":
        peer = load_state()["peers"].get(arg)
        await query.answer()
        if peer:
            await query.edit_message_text(
                f"{user_name(load_state(), peer['user'])}\n" + await check_peer(arg, peer),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Ещё раз", callback_data=f"png:{arg}"),
                                                    InlineKeyboardButton("⬅️ Назад", callback_data=f"uc:{peer['user']}")]]))
        return

    if action in ("ext", "exd", "exc", "exb"):
        days, _, pub = arg.partition(":") if action == "exd" else ("", "", arg)
        state = load_state()
        peer = state["peers"].get(pub)
        if not peer:
            await query.answer("Конфиг удалён.")
            return
        if action == "ext":
            await query.answer()
            await query.edit_message_text(f"{user_card(state, peer['user'])}\n\nНа сколько продлить {title(peer)}?",
                                          reply_markup=extend_buttons(pub))
        elif action == "exc":
            await query.answer()
            ctx.user_data["await"] = f"extend:{pub}"
            ctx.user_data["extend_msg"] = (query.message.chat_id, query.message.message_id)
            await query.edit_message_text(f"На сколько дней продлить {title(peer)}? Пришлите число, например 45.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                              "⬅️ Назад", callback_data=f"uc:{peer['user']}")]]))
        elif action == "exb":
            await query.answer()
            await query.edit_message_text(user_card(state, peer["user"]), reply_markup=user_buttons(peer["user"]))
        else:
            result = await extend_peer(pub, int(days))
            await query.answer(result.split("\n")[0][:190])
            await query.edit_message_text(user_card(load_state(), peer["user"]), reply_markup=user_buttons(peer["user"]))
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
            await query.answer(f"{title(peer)} отключён")
            try:
                await USER_BOT.send_message(int(peer["user"]), f"Конфиг {title(peer)} отключён администратором.")
            except Exception:
                pass
        state = load_state()
        peer = state["peers"].get(arg)
        await query.edit_message_text(user_card(state, peer["user"]), reply_markup=user_buttons(peer["user"]))
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
    if action == "ok" and payment.get("target") == TOPUP:
        async with lock:
            state = load_state()
            u = state["users"].setdefault(str(uid), {"name": str(uid)})
            u["balance"] = u.get("balance", 0) + payment["price"]
            save_state(state)
        result = f"✅ Баланс пополнен на {payment['price']} ₽, теперь {u['balance']} ₽."
        try:
            await USER_BOT.send_message(uid, f"💰 Баланс пополнен на {payment['price']} ₽. Теперь {u['balance']} ₽.",
                                        reply_markup=KEYBOARD)
        except Exception:
            pass
        if payment.get("then"):
            target, months = payment["then"]
            ok, text = await buy_with_balance(uid, target, int(months))
            result += f"\n{text}"
            try:
                await USER_BOT.send_message(uid, text)
            except Exception:
                pass
    elif action == "ok":
        result = "✅ Одобрено. " + await grant(uid, payment.get("target", NEW), payment.get("days") or DAYS)
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


# ---------- состояние серверов ----------

SERVERS_BUTTON = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="srv:")]])


async def sh(*cmd, timeout=15):
    """Команда без оболочки: код возврата и вывод (не блокирует бота)."""
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out = (await asyncio.wait_for(proc.communicate(), timeout))[0].decode(errors="replace")
        return proc.returncode, out
    except Exception:
        return 1, ""


async def unit_active(unit):
    return (await sh("systemctl", "is-active", "--quiet", unit))[0] == 0


async def ping_host(host, count=3):
    """Средний пинг в мс или None, если не отвечает."""
    rc, out = await sh("ping", "-c", str(count), "-i", "0.3", "-W", "2", "-q", host)
    for line in out.splitlines():
        if "min/avg" in line:
            return round(float(line.split("=")[1].split("/")[1]))
    return None


# ---------- туннель для российских сайтов: потери и подбор порта ----------
# На российском сервере ru-ports.sh направляет UDP-порты TUN_PORT_RANGE от основного сервера в туннель,
# поэтому порт можно менять только здесь — у основного сервера, без доступа к российскому.
TUN_CONF = os.path.join(os.path.dirname(CONF), "awg1.conf")
TUN_PORT_RANGE = os.environ.get("TUN_PORT_RANGE", "")      # например 1024-9999 (после ru-ports.sh)
TUN_ALERT_LOSS = float(os.environ.get("TUN_ALERT_LOSS", "5"))  # % потерь, при которых писать админу
TUN_STATE = {"bad": 0, "alerted": False, "snooze": 0}


async def tunnel_loss(count=30):
    """(потери в %, средний пинг) по туннелю до российского сервера."""
    rc, out = await sh("ping", "-c", str(count), "-i", "0.2", "-W", "2", "-q", "10.66.0.1",
                       timeout=count * 0.2 + 10)
    loss, avg = 100.0, None
    for line in out.splitlines():
        m = re.search(r"([\d.]+)% packet loss", line)
        if m:
            loss = float(m.group(1))
        if "min/avg" in line:
            avg = round(float(line.split("=")[1].split("/")[1]))
    return loss, avg


def tunnel_endpoint():
    try:
        with open(TUN_CONF) as f:
            for line in f:
                m = re.match(r"\s*Endpoint\s*=\s*([\d.]+):(\d+)", line)
                if m:
                    return m.group(1), int(m.group(2))
    except FileNotFoundError:
        pass
    return None, None


async def set_tunnel_port(port, persist):
    """Переключает туннель на другой порт российского сервера сразу, без перезапуска."""
    ip, _ = tunnel_endpoint()
    rc, out = await sh("awg", "show", "awg1", "peers")
    peer = out.split()[0] if out.split() else ""
    if not ip or not peer:
        raise RuntimeError("туннель awg1 не найден")
    await sh("awg", "set", "awg1", "peer", peer, "endpoint", f"{ip}:{port}")
    if persist:
        with open(TUN_CONF) as f:
            text = f.read()
        text = re.sub(r"(?m)^(\s*Endpoint\s*=\s*[\d.]+:)\d+", rf"\g<1>{port}", text)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(TUN_CONF))
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, TUN_CONF)


def tunnel_buttons():
    rows = [[InlineKeyboardButton("✅ Оставить", callback_data="tun:keep")]]
    if TUN_PORT_RANGE:
        rows.insert(0, [InlineKeyboardButton("🔄 Подобрать другой порт", callback_data="tun:swap")])
    return InlineKeyboardMarkup(rows)


async def pick_tunnel_port(progress):
    """Пробует несколько портов и оставляет тот, где меньше всего потерь. progress(text) — ход проверки."""
    lo, hi = (int(x) for x in TUN_PORT_RANGE.split("-"))
    _, cur = tunnel_endpoint()
    cur_loss, cur_avg = await tunnel_loss(20)
    lines = [f"Сейчас порт {cur}: потери {cur_loss:.0f}%, пинг {cur_avg or '—'} мс"]
    await progress("\n".join(lines))
    best = (cur_loss, cur_avg or 9999, cur)
    for port in random.sample([p for p in range(lo, hi + 1) if p != cur], 8):
        await set_tunnel_port(port, persist=False)
        await asyncio.sleep(4)  # новое рукопожатие
        loss, avg = await tunnel_loss(20)
        lines.append(f"порт {port}: потери {loss:.0f}%, пинг {avg or '—'} мс")
        await progress("\n".join(lines))
        if (loss, avg or 9999) < best[:2]:
            best = (loss, avg or 9999, port)
        if loss == 0 and avg:
            break
    await set_tunnel_port(best[2], persist=True)
    TUN_STATE.update(bad=0, alerted=False)
    head = (f"✅ Туннель переведён на порт {best[2]}: потери {best[0]:.0f}%." if best[2] != cur
            else f"Лучше порта {cur} не нашлось — оставил его.")
    return head + "\n\n" + "\n".join(lines)


async def tunnel_loop():
    """Раз в 10 минут: потери в туннеле для российских сайтов; 2 плохие проверки подряд — сообщение админу."""
    while True:
        await asyncio.sleep(600)
        if not os.path.exists(TUN_CONF) or not await unit_active("awg-quick@awg1"):
            continue
        try:
            loss, avg = await tunnel_loss(30)
        except Exception:
            continue
        if loss >= TUN_ALERT_LOSS:
            TUN_STATE["bad"] += 1
            if TUN_STATE["bad"] >= 2 and not TUN_STATE["alerted"] and time.time() > TUN_STATE["snooze"]:
                TUN_STATE["alerted"] = True
                _, port = tunnel_endpoint()
                hint = "" if TUN_PORT_RANGE else ("\n\nЧтобы бот сам подбирал порт: на российском сервере "
                                                  "sudo bash ru-ports.sh 104.249.40.115, затем TUN_PORT_RANGE=1024-9999 "
                                                  "в bot.env.")
                await notify_admins(f"🟠 Туннель для российских сайтов теряет пакеты: {loss:.0f}% "
                                    f"(пинг {avg or '—'} мс, порт {port}). Российские сайты могут грузиться плохо — "
                                    f"скорее всего, порт начали резать.{hint}", tunnel_buttons())
        else:
            if TUN_STATE["alerted"]:
                await notify_admins(f"🟢 Туннель для российских сайтов снова без потерь ({loss:.0f}%).")
            TUN_STATE.update(bad=0, alerted=False)


def _cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    return sum(v), v[3] + v[4]  # всего, простой (idle + iowait)


def _net_bytes(iface):
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(iface + ":"):
                v = line.split(":", 1)[1].split()
                return int(v[0]), int(v[8])
    return 0, 0


async def server_status():
    """Текст экрана «🖥 Серверы» и список проблем (для уведомлений)."""
    problems = []
    rc, out = await sh("ip", "-4", "route", "show", "default")
    iface = out.split(" dev ")[1].split()[0] if " dev " in out else ""
    t1, i1 = _cpu_times()
    n1 = _net_bytes(iface)
    await asyncio.sleep(1)
    t2, i2 = _cpu_times()
    n2 = _net_bytes(iface)
    cpu = round(100 * (1 - (i2 - i1) / max(1, t2 - t1)))
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = int(v.split()[0])
    mem_pct = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]))
    du = shutil.disk_usage("/")
    disk_pct = round(100 * du.used / du.total)
    down, up = (n2[0] - n1[0]) * 8 / 1e6, (n2[1] - n1[1]) * 8 / 1e6
    total_gb = (n2[0] + n2[1]) / 1e9

    hs = last_handshakes()
    online = sum(1 for h in hs.values() if is_online(h))
    vpn_ok = await unit_active(f"awg-quick@{IFACE}")
    if not vpn_ok:
        problems.append("VPN (AmneziaWG) на основном сервере не работает")
    if cpu >= 90:
        problems.append(f"процессор основного сервера загружен на {cpu}%")
    if disk_pct >= 90:
        problems.append(f"диск основного сервера заполнен на {disk_pct}%")

    lines = [f"🖥 Серверы · {datetime.now(TZ).strftime('%H:%M')}", "",
             f"🇳🇱 Основной ({ENDPOINT_HOST})",
             f"{'🟢 VPN работает' if vpn_ok else '🔴 VPN не работает'} · подключены сейчас: {online}",
             f"Процессор {cpu}% · память {mem_pct}% · диск {disk_pct}%",
             f"Канал сейчас: ↓{down:.0f} ↑{up:.0f} Мбит/с · с запуска сервера {total_gb:.0f} ГБ"]

    if RELAY_HOST:
        rping = await ping_host(RELAY_HOST)
        lines += ["", f"🇷🇺 Вход ({RELAY_HOST})",
                  f"🟢 отвечает · пинг {rping} мс" if rping is not None else "🔴 не отвечает на пинг"]
        if rping is None:
            problems.append(f"вход {RELAY_HOST} не отвечает")
    if os.path.exists(os.path.join(os.path.dirname(CONF), "awg1.conf")):
        tping = await ping_host("10.66.0.1") if await unit_active("awg-quick@awg1") else None
        lines.append(f"🟢 туннель для российских сайтов работает · {tping} мс" if tping is not None else
                     "🔴 туннель для российских сайтов не работает (сторож пробует поднять)")
        if tping is None:
            problems.append("туннель для российских сайтов не работает — российские сайты идут из Нидерландов")
        else:
            loss, _ = await tunnel_loss(15)
            lines.append(f"    потери в туннеле: {loss:.0f}% · порт {tunnel_endpoint()[1]}")
    if ADBLOCK_DNS:
        ok = await unit_active("dnsmasq")
        lines += ["", "🛡 Блокировка рекламы: " + ("работает" if ok else "🔴 не работает")]
        if not ok:
            problems.append("DNS с блокировкой рекламы не работает — у клиентов с ней не открываются сайты")
    return "\n".join(lines), problems


@admin_only
async def admin_servers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, _ = await server_status()
    await update.message.reply_text(text, reply_markup=SERVERS_BUTTON)


async def monitor_loop():
    """Раз в 2 минуты проверяет серверы; о проблеме, которая держится 2 проверки подряд, и о её
    исправлении пишет администратору."""
    seen, alerted = set(), set()
    while True:
        await asyncio.sleep(120)
        try:
            _, problems = await server_status()
        except Exception:
            log.exception("проверка серверов не удалась")
            continue
        now = set(problems)
        for p in sorted((now & seen) - alerted):
            await notify_admins(f"🔴 {p}\n\nПодробнее: «{ABTN_SERVERS}».")
            alerted.add(p)
        for p in sorted(alerted - now):
            await notify_admins(f"🟢 Снова в порядке: {p}")
            alerted.discard(p)
        seen = now


@admin_only
async def admin_poll(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """«🗳 Опрос»: результаты последнего опроса или сразу создание нового."""
    polls = load_state()["polls"]
    if not polls:
        await poll_new_ask(update.message, ctx)
        return
    pid = max(polls, key=int)
    await update.message.reply_text(admin_poll_text(pid, polls[pid]), reply_markup=admin_poll_buttons(pid, polls[pid]))


def admin_poll_text(pid, poll):
    voted = len(poll["votes"])
    return (f"🗳 Опрос №{pid} от {fmt_day(poll['created'])}: {poll['q']}\n"
            f"Разослан: {poll.get('sent', 0)}, проголосовали: {voted}\n\n" + poll_results(poll))


def admin_poll_buttons(pid, poll):
    row = [InlineKeyboardButton("🔄 Обновить", callback_data=f"poll:show:{pid}")]
    if not poll.get("closed"):
        row.append(InlineKeyboardButton("🔒 Закрыть", callback_data=f"poll:close:{pid}"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("🆕 Новый опрос", callback_data="poll:new")]])


async def poll_new_ask(message, ctx):
    ctx.user_data["await"] = "poll"
    await message.reply_text("Пришлите опрос одним сообщением: первая строка — вопрос, дальше варианты ответа, "
                             "каждый с новой строки (от 2 до 10). Например:\n\n"
                             "Чем пользуетесь чаще?\nТелефон\nКомпьютер\nОбоими")


async def on_admin_poll(query, ctx, arg):
    what, _, pid = arg.partition(":")
    await query.answer()
    if what == "new":
        await poll_new_ask(query.message, ctx)
        return
    if what == "no":
        ctx.user_data.pop("poll_draft", None)
        await query.edit_message_text("Опрос отменён.")
        return
    if what == "yes":
        draft = ctx.user_data.pop("poll_draft", None)
        if not draft:
            await query.edit_message_text(f"Опрос потерялся — нажмите «{ABTN_POLL}» ещё раз.")
            return
        async with lock:
            state = load_state()
            pid = str(state["next_poll"])
            state["next_poll"] += 1
            state["polls"][pid] = {**draft, "votes": {}, "created": int(time.time()), "closed": False}
            save_state(state)
        await query.edit_message_text("🗳 Рассылаю опрос…")
        poll = state["polls"][pid]
        sent = 0
        for uid in list(state["users"]):
            try:
                await USER_BOT.send_message(int(uid), f"🗳 Опрос\n\n{poll['q']}", reply_markup=poll_buttons(pid, poll))
                sent += 1
            except Exception:
                pass
        async with lock:
            state = load_state()
            state["polls"][pid]["sent"] = sent
            save_state(state)
        poll = state["polls"][pid]
        await query.edit_message_text(admin_poll_text(pid, poll), reply_markup=admin_poll_buttons(pid, poll))
        return
    async with lock:
        state = load_state()
        poll = state["polls"].get(pid)
        if not poll:
            await query.edit_message_text("Опрос не найден.")
            return
        if what == "close":
            poll["closed"] = True
            save_state(state)
    try:
        await query.edit_message_text(admin_poll_text(pid, poll), reply_markup=admin_poll_buttons(pid, poll))
    except Exception:
        pass  # ничего не изменилось


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
        f"Клиенты получат:\n\n{resend_message(text)}\n\n"
        f"Активных конфигов: {len(peers)}, клиентов: {len(users)}. Сроки не меняются.\n\n"
        "Что разослать?\n"
        "• Режим — клиент ответит «Пользуюсь» / «Не пользуюсь» и получит конфиг этого режима.\n"
        "• Все сразу — кнопка на каждый конфиг клиента, файл по нажатию.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(MODES["smart"], callback_data="resend:m:smart"),
             InlineKeyboardButton(MODES["full"], callback_data="resend:m:full")],
            [InlineKeyboardButton(MODES["game"], callback_data="resend:m:game"),
             InlineKeyboardButton(MODES["pcgame"], callback_data="resend:m:pcgame")],
            [InlineKeyboardButton("📦 Все сразу", callback_data="resend:yes")],
            [InlineKeyboardButton("Отмена", callback_data="resend:no")],
        ]))


def resend_message(text):
    return f"🔄 Обновление конфига — {text}"


def device_peers(state, uid):
    """Действующие конфиги клиента для устройств (у роутеров режимов нет)."""
    return {k: p for k, p in user_peers(state, uid).items() if peer_active(p) and p.get("kind") not in ROUTER_KINDS}


async def resend_mode(query, text, mode):
    """Рассылка одного режима: клиент отвечает, пользуется ли им, и получает конфиг этого режима."""
    state = load_state()
    users = {p["user"] for p in state["peers"].values()
             if peer_active(p) and p.get("kind") not in ROUTER_KINDS}
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Пользуюсь", callback_data=f"rsy:{mode}"),
        InlineKeyboardButton("❌ Не пользуюсь", callback_data="rsn:"),
    ]])
    sent, failed = 0, []
    for uid in users:
        try:
            await USER_BOT.send_message(
                int(uid), f"{resend_message(text)}\n\nОбновился режим «{MODES[mode]}». Вы им пользуетесь?",
                reply_markup=buttons)
            await asyncio.sleep(0.05)
            sent += 1
        except Exception:
            failed.append(user_name(state, uid))
    result = f"📤 Режим «{MODES[mode]}»: спросил {sent} из {len(users)} клиентов."
    if failed:
        result += "\nНе удалось (не нажимали /start или заблокировали бота): " + ", ".join(failed)[:3000]
    await query.message.reply_text(result, reply_markup=ADMIN_KEYBOARD)


async def resend_all(query, text):
    state = load_state()
    by_user = {}
    for pub, p in state["peers"].items():
        if peer_active(p):
            by_user.setdefault(p["user"], []).append((pub, p))
    sent, failed = 0, []
    for uid, peers in by_user.items():
        # Не заваливаем чат файлами: кнопка на каждый конфиг, файлы — по нажатию.
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton(f"📥 Получить {title(p)}", callback_data=f"get:{pub}")]
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
    names = {pub: f"{user_name(state, p['user'])} / {title(p)}" for pub, p in state["peers"].items()}
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
    # Деньги — переводы (оплаты и пополнения); оплата с баланса — те же деньги второй раз, её не считаем.
    money = [p for p in state["payments"].values() if p["status"] == "approved" and p.get("via") != "balance"]
    approved = len(money)
    revenue = sum(p.get("price", PRICE) for p in money)
    balances = sum(u.get("balance", 0) for u in state["users"].values())
    await update.message.reply_text(
        f"Оплаченных конфигов сейчас: {active}\nОдобренных оплат всего: {approved} "
        f"(≈ {revenue} ₽)\nНа балансах клиентов: {balances} ₽\n\n" + ("\n".join(rows) or "Подключений нет."),
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
    if (ctx.user_data.get("await") or "").startswith(("ticket", "rename")):
        ctx.user_data.pop("await")
    ctx.user_data.pop("album", None)


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
    configs = "\n".join(f"  {title(p)} — {peer_status(p)}" for p in peers.values()) or "  нет"
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


async def rename_peer(update, ctx, pub, label):
    """Новое название конфига. Ответ клиента удаляется, исходное сообщение снова становится экраном конфига."""
    chat_id, msg_id = ctx.user_data.pop("rename_msg", (update.effective_chat.id, None))
    try:
        await update.message.delete()
    except Exception:
        pass

    async def show(note, again=False):
        if again:  # ждём название ещё раз
            ctx.user_data["await"] = f"rename:{pub}"
            ctx.user_data["rename_msg"] = (chat_id, msg_id)
        state = load_state()
        peer = state["peers"].get(pub)
        if not peer:
            return
        if again:
            text, buttons = f"{note}\n\n✏️ Напишите другое название.", InlineKeyboardMarkup([[back_button(pub)]])
        else:
            text, buttons = config_card(state, pub, peer, back=len(user_peers(state, peer["user"])) > 1)
            text = f"{note}\n\n{text}"
        try:
            if msg_id:
                await ctx.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=buttons)
            else:
                await ctx.bot.send_message(chat_id, text, reply_markup=buttons)
        except Exception:
            pass

    label = " ".join(label.split())
    if label.lower() in ("отмена", "отменить", "cancel"):
        await show("Название не изменено.")
        return
    if not 1 <= len(label) <= 30:
        await show("Название — от 1 до 30 символов.", again=True)
        return
    async with lock:
        state = load_state()
        peer = state["peers"].get(pub)
        if not peer or peer["user"] != str(update.effective_user.id):
            return
        others = [p for k, p in user_peers(state, update.effective_user.id).items() if k != pub]
        if any(p.get("label", "").lower() == label.lower() or p["name"].lower() == label.lower() for p in others):
            await show("Такое название уже есть у другого конфига.", again=True)
            return
        peer["label"] = label
        save_state(state)
    await show("✅ Название изменено." + (" Чтобы оно появилось и в приложении — скачайте файл заново."
                                          if TUNNEL_NAME.fullmatch(label) else ""))


async def user_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Свободный текст клиента: обращение, реквизиты для возврата или просто возврат в меню."""
    waiting = ctx.user_data.get("await") or ""
    if waiting in ("ref", "ref_set"):
        text = update.message.text.strip()
        async with lock:
            state = load_state()
            u = get_user(state, update.effective_user)
            rid = parse_ref(state, update.effective_user.id, text)
            if rid:
                u["ref"] = rid
                save_state(state)
        if not rid:
            await update.message.reply_text("Такого клиента нет. Проверьте ID (одно число) и пришлите ещё раз.")
            return
        ctx.user_data.pop("await")
        if waiting == "ref_set":
            await update.message.reply_text(f"✅ Записал: вас пригласил {user_name(state, rid)}. Когда оплатите VPN, "
                                            "друг получит зачёт.", reply_markup=KEYBOARD)
            return
        await request_access(update.effective_user)
        await update.message.reply_text(closed_text(state, update.effective_user.id))
        return
    if waiting == "topup":
        text = update.message.text.strip().replace("₽", "").replace(" ", "")
        if text.isdigit():
            if int(text) < TOPUP_MIN:
                await update.message.reply_text(f"Минимум — {TOPUP_MIN} ₽. Напишите сумму ещё раз.")
                return
            await topup_screen(update.message, ctx, int(text))
            return
        ctx.user_data.pop("await")
    if waiting.startswith("rename:"):
        ctx.user_data.pop("await")
        await rename_peer(update, ctx, waiting.partition(":")[2], update.message.text.strip())
        return
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
            p.setdefault("made", int(now))  # конфигам из старых версий — отсчёт для автоудаления с сегодня
            if is_admin(p["user"]) or not p["until"]:
                continue
            left = p["until"] - now
            renew = f"Продлить: «{BTN_LIST}» → «🔄 Продлить»."
            if left <= 0 and p["warned"] < 3:
                p["warned"] = 3
                expired = True
                notify.append((p["user"], f"Срок конфига {title(p)} закончился, он отключён. {renew}"))
            elif 0 < left <= DAY and p["warned"] < 2:
                p["warned"] = 2
                notify.append((p["user"], f"Конфиг {title(p)} отключится {fmt_date(p['until'])} — "
                                          f"меньше чем через сутки. {renew}"))
            elif DAY < left <= 3 * DAY and p["warned"] < 1:
                p["warned"] = 1
                notify.append((p["user"], f"Конфиг {title(p)} работает до {fmt_date(p['until'])}. {renew}"))
        if expired:
            apply_or_rollback(state, old_state)
        save_state(state)
    for uid, text in notify:
        try:
            await USER_BOT.send_message(int(uid), text, reply_markup=KEYBOARD)
        except Exception:
            log.warning("не удалось уведомить %s", uid)


async def delete_stale(days):
    """Удалить неоплаченные и закончившиеся больше days дней назад конфиги и сообщить клиентам."""
    async with lock:
        state = load_state()
        old_state = json.loads(json.dumps(state))
        pubs = stale_peers(state, days)
        if not pubs:
            return 0
        gone = [state["peers"][k] for k in pubs]
        remove_peers(state, pubs)
        apply_or_rollback(state, old_state)
        save_state(state)
    for p in gone:
        try:
            await USER_BOT.send_message(int(p["user"]), f"🗑 Конфиг {title(p)} удалён: он не был оплачен или продлён. "
                                                        f"Новый — «{BTN_BUY}».", reply_markup=KEYBOARD)
        except Exception:
            pass
    log.info("удалено неоплаченных конфигов: %d", len(gone))
    return len(gone)


async def expiry_loop():
    while True:
        try:
            await check_expiry()
            if load_state()["settings"].get("autodel"):
                await delete_stale(AUTODEL_DAYS)
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
    # Нажатие кнопки, на которое бот не успел ответить (был выключен или долго работал):
    # действие уже выполнено, а сама ошибка ничего не значит.
    if "query is too old" in str(ctx.error).lower() or "query id is invalid" in str(ctx.error).lower():
        return
    await report_error("Ошибка в боте", ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await ctx.bot.send_message(update.effective_chat.id, "Что-то пошло не так, попробуйте ещё раз.")
        except Exception:
            pass


async def clear_and_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """«Почистить чат»: удаляет переписку и присылает приветствие с меню заново."""
    await clear_chat(ctx.bot, update.effective_chat.id, update.message.message_id)
    await (admin_start if ctx.bot.id == ADMIN_BOT.id else start)(update, ctx)


# ---------- чистый чат ----------
# Нажатие кнопки меню удаляет само нажатие и прошлый экран бота (его текстовые ответы на прошлое
# нажатие). Файлы конфигов, QR-коды, оплаты, оповещения и переписка поддержки не удаляются:
# запоминаются только текстовые сообщения, отправленные, пока обрабатывается кнопка меню.

CLOSE_BUTTON = InlineKeyboardButton("✖️ Закрыть", callback_data="close:")
# Без «Закрыть»: заявки, оплаты и обращения (их нельзя терять) и подсказка к ключу (там своя кнопка).
NO_CLOSE = ("acc", "ok", "no", "tk", "tku", "kdone", "close")


def with_close(markup):
    """Добавить в конец inline-клавиатуры кнопку «✖️ Закрыть», которая удаляет сообщение."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows = markup.inline_keyboard
    for row in rows:
        for b in row:
            if (b.callback_data or "").partition(":")[0] in NO_CLOSE:
                return markup
    return InlineKeyboardMarkup([*rows, [CLOSE_BUTTON]])


class ScreenBot(ExtBot):
    """Бот, который запоминает свои ответы на кнопку меню, чтобы удалить их при следующем нажатии."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._active = {}   # chat_id -> id сообщений, отправленных в ответ на текущее нажатие
        self._screens = {}  # chat_id -> id сообщений прошлого экрана
        # Нижнее меню держится на сообщении, которое его прислало: удалишь его — меню пропадёт.
        # Поэтому такое сообщение удаляем, только когда бот уже прислал меню новым сообщением.
        self._kb = {}       # chat_id -> (id последнего сообщения с меню, было ли оно частью экрана)

    async def edit_message_text(self, *args, **kwargs):
        kwargs["reply_markup"] = with_close(kwargs.get("reply_markup"))
        return await super().edit_message_text(*args, **kwargs)

    async def edit_message_reply_markup(self, *args, **kwargs):
        kwargs["reply_markup"] = with_close(kwargs.get("reply_markup"))
        return await super().edit_message_reply_markup(*args, **kwargs)

    async def send_photo(self, *args, **kwargs):
        kwargs["reply_markup"] = with_close(kwargs.get("reply_markup"))
        return await super().send_photo(*args, **kwargs)

    async def send_message(self, *args, **kwargs):
        kwargs["reply_markup"] = with_close(kwargs.get("reply_markup"))
        msg = await super().send_message(*args, **kwargs)
        chat = msg.chat_id
        if chat in self._active:
            self._active[chat].append(msg.message_id)
        if isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup):
            old_id, old_screen = self._kb.get(chat, (None, False))
            self._kb[chat] = (msg.message_id, chat in self._active)
            if old_id and old_screen:
                try:
                    await self.delete_message(chat, old_id)
                except Exception:
                    pass
        return msg

    async def screen_begin(self, chat_id, user_msg_id):
        keep = self._kb.get(chat_id, (None, False))[0]
        old = [m for m in self._screens.pop(chat_id, []) if m != keep] + [user_msg_id]
        try:
            await self.delete_messages(chat_id, old)
        except Exception:
            pass  # старше 48 часов или уже удалены
        self._active[chat_id] = []

    def screen_end(self, chat_id):
        self._screens[chat_id] = self._active.pop(chat_id, [])


async def screen_begin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message and isinstance(ctx.bot, ScreenBot):
        await ctx.bot.screen_begin(update.effective_chat.id, update.message.message_id)


async def screen_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message and isinstance(ctx.bot, ScreenBot):
        ctx.bot.screen_end(update.effective_chat.id)


async def user_menu_begin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    leave_ticket(ctx)
    await screen_begin(update, ctx)


def build_user_app():
    app = Application.builder().bot(ScreenBot(token=TOKEN)).build()
    # Кнопки меню и команды: выход из переписки по обращению и чистый чат (прошлый экран удаляется).
    menu = filters.Text([BTN_BUY, BTN_LIST, BTN_HELP, BTN_NEWS, BTN_SUPPORT, BTN_CLEAR, BTN_BALANCE]) \
        | filters.Command(["start", "help", "news"])
    app.add_handler(MessageHandler(menu, user_menu_begin), group=-1)
    app.add_handler(MessageHandler(menu, screen_end), group=1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_BUY]), buy))
    app.add_handler(MessageHandler(filters.Text([BTN_LIST]), list_configs))
    app.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_NEWS]), news_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_BALANCE]), balance_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_screenshot))
    # Старые кнопки из прошлой версии бота, оставшиеся у клиентов на экране.
    app.add_handler(MessageHandler(filters.Text([BTN_SUPPORT]), support_start))
    app.add_handler(MessageHandler(filters.Text([BTN_CLEAR]), clear_and_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, user_text))
    app.add_handler(CallbackQueryHandler(on_user_button))
    app.add_error_handler(on_error)
    return app


def build_admin_app():
    app = Application.builder().bot(ScreenBot(token=ADMIN_TOKEN)).build()
    menu = filters.Text([ABTN_PAY, ABTN_USERS, ABTN_GIVE, ABTN_STATS, ABTN_RESEND, ABTN_NEWS, ABTN_TICKETS,
                         ABTN_POLL, ABTN_SERVERS, BTN_CLEAR]) | filters.Command(["start", "help"])
    admin_menu = filters.User(user_id=ADMINS, allow_empty=True) & menu
    app.add_handler(MessageHandler(admin_menu, screen_begin), group=-1)
    app.add_handler(MessageHandler(admin_menu, screen_end), group=1)
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
    app.add_handler(MessageHandler(filters.Text([ABTN_POLL]), admin_poll))
    app.add_handler(MessageHandler(filters.Text([ABTN_SERVERS]), admin_servers))
    app.add_handler(MessageHandler(filters.Text([ABTN_TICKETS]), admin_tickets))
    app.add_handler(MessageHandler(filters.Text([BTN_CLEAR]), admin_only(clear_and_start)))
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
        tasks = [asyncio.create_task(expiry_loop()), asyncio.create_task(split_loop()),
                 asyncio.create_task(dynamic_loop()), asyncio.create_task(monitor_loop()),
                 asyncio.create_task(tunnel_loop())]

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
