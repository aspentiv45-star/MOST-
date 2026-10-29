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
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/most/state.json")
ENDPOINT_HOST = os.environ["ENDPOINT_HOST"]
RELAY_HOST = os.environ.get("RELAY_HOST", "")
CLIENT_DNS = os.environ.get("CLIENT_DNS", "1.1.1.1, 1.0.0.1")
MAX_PEERS = int(os.environ.get("MAX_PEERS", "5"))
PRICE = int(os.environ.get("PRICE", "200"))
DAYS = int(os.environ.get("DAYS", "30"))
PAY_FILE = os.environ.get("PAY_FILE", "/etc/most/pay.txt")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
# Раздельный туннель: российские сайты и приложения — напрямую с IP устройства.
SPLIT_RU = os.environ.get("SPLIT_RU", "1") == "1"
SPLIT_FILE = os.environ.get("SPLIT_FILE", "/var/lib/most/split.txt")
SPLIT_LIMIT = int(os.environ.get("SPLIT_LIMIT", "2000"))
FULL_TUNNEL = "0.0.0.0/0, ::/0"

DAY = 86400
NEW = "new"  # цель оплаты «новый конфиг»; иначе цель — публичный ключ продлеваемого конфига

# Параметры обфускации AmneziaWG: клиенту они нужны точно такими же, как у сервера.
OBFS_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
             "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")

BTN_BUY = f"➕ Купить конфиг — {PRICE} ₽"
BTN_LIST = "📄 Мои конфиги"
BTN_HELP = "❓ Как подключить"
KEYBOARD = ReplyKeyboardMarkup([[BTN_BUY], [BTN_LIST, BTN_HELP]], resize_keyboard=True)

HELP_TEXT = (
    "Как подключиться:\n\n"
    "1. Установите AmneziaVPN или AmneziaWG (App Store / Google Play / amnezia.org).\n"
    f"2. Нажмите «{BTN_BUY}», оплатите и пришлите скриншот перевода.\n"
    "3. После подтверждения бот пришлёт файл конфига.\n"
    "4. В приложении: «+» → импорт из файла (на iPhone: нажмите на файл → «Поделиться» → Amnezia).\n"
    "5. Включите туннель и проверьте на ipinfo.io — должны быть Нидерланды.\n\n"
    + ("Российские сайты и приложения работают напрямую, VPN выключать не нужно.\n\n" if SPLIT_RU else "")
    + ("Если на мобильном интернете обычный конфиг не подключается — "
       "включите второй, с «-wl» в названии.\n\n" if RELAY_HOST else "")
    + f"Один конфиг — одно устройство, {DAYS} дней — {PRICE} ₽. Для второго устройства купите ещё один конфиг.\n"
    f"Продлить конфиг: «{BTN_LIST}» → «🔄 Продлить»."
)

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

def interface_section():
    """Часть awg0.conf до первого [Peer] — её бот никогда не меняет."""
    with open(CONF) as f:
        lines = f.read().splitlines()
    head = []
    for line in lines:
        if line.strip() == "[Peer]":
            break
        head.append(line)
    while head and not head[-1].strip():
        head.pop()
    return head


def server_params():
    params = {}
    for line in interface_section():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            params[key.strip()] = value.strip()
    return params


def write_server_conf(state):
    """Пересобирает awg0.conf: в нём только оплаченные конфиги."""
    out = interface_section()
    for pub, p in state["peers"].items():
        if peer_active(p):
            out += ["", "[Peer]", f"# {p['user']} {p['name']}", f"PublicKey = {pub}", f"AllowedIPs = {p['ip']}/32"]
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONF))
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)

    stripped = run(["awg-quick", "strip", IFACE])
    with tempfile.NamedTemporaryFile("w", suffix=".conf") as f:
        f.write(stripped + "\n")
        f.flush()
        run(["awg", "syncconf", IFACE, f.name])


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


def create_peer(state, uid, until):
    """Добавляет конфиг в state (без применения на сервере). Возвращает его ключ и данные."""
    names = {p["name"] for p in user_peers(state, uid).values()}
    n = next(i for i in range(1, 1000) if f"vpn{i}" not in names)
    priv = run(["awg", "genkey"])
    pub = run(["awg", "pubkey"], priv + "\n")
    peer = {"user": str(uid), "name": f"vpn{n}", "ip": next_free_ip(state, server_params()),
            "priv": priv, "created": fmt_date(time.time()), "until": until, "warned": 0}
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
        f"AllowedIPs = {allowed_ips()}",
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
    tunnel = await asyncio.to_thread(split.tunnel_networks, nets, SPLIT_LIMIT)
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
    params = server_params()
    # Имя файла становится именем туннеля; на iOS оно не длиннее 15 символов.
    variants = [(f"{peer['name']}.conf", ENDPOINT_HOST, "обычное подключение")]
    if RELAY_HOST:
        variants.append((f"{peer['name']}-wl.conf", RELAY_HOST,
                         "через вход в РФ — для мобильного интернета с белыми списками"))
    for filename, host, note in variants:
        text = client_conf(peer, params, host)
        await USER_BOT.send_document(chat_id, document=io.BytesIO(text.encode()), filename=filename,
                                     caption=f"{filename}: {note}\n{peer_status(peer)}")
        # Конфиг с раздельным туннелем не помещается в QR-код — тогда только файл.
        if len(text) < 2000:
            png = io.BytesIO()
            qrcode.make(text).save(png, format="PNG")
            png.seek(0)
            await USER_BOT.send_photo(chat_id, photo=png, caption=f"QR-код: {filename}")


def pay_text():
    # Файл читается при каждом нажатии: реквизиты можно менять без перезапуска бота.
    try:
        with open(PAY_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Реквизиты не указаны — напишите администратору."


def target_label(state, target):
    if target == NEW:
        return "новый конфиг"
    peer = state["peers"].get(target)
    return f"продление {peer['name']} ({peer_status(peer)})" if peer else "продление удалённого конфига"


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with lock:
        state = load_state()
        get_user(state, update.effective_user)
        save_state(state)
    mine = user_peers(state, update.effective_user.id)
    active = sum(1 for p in mine.values() if peer_active(p))
    await update.message.reply_text(
        f"Привет! Это VPN на AmneziaWG.\nОдин конфиг (одно устройство) — {PRICE} ₽ за {DAYS} дней.\n\n"
        + (f"Ваших конфигов: {len(mine)}, активных: {active}. Подробнее — «{BTN_LIST}»." if mine
           else f"Чтобы подключиться, нажмите «{BTN_BUY}»."),
        reply_markup=KEYBOARD)


async def ask_payment(message, ctx, target, state):
    ctx.user_data["pay_for"] = target
    await message.reply_text(
        f"Оплата: {target_label(state, target)} — {PRICE} ₽ за {DAYS} дней.\n\n{pay_text()}\n\n"
        "После оплаты пришлите сюда скриншот перевода одним фото. "
        "Администратор проверит его, и бот сразу пришлёт результат.")


async def buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    if len(user_peers(state, update.effective_user.id)) >= MAX_PEERS and not is_admin(update.effective_user.id):
        await update.message.reply_text(
            f"У вас уже {MAX_PEERS} конфигов — это максимум. Продлите или удалите ненужный в «{BTN_LIST}».")
        return
    await ask_payment(update.message, ctx, NEW, state)


async def on_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        file = await msg.photo[-1].get_file()
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file = await msg.document.get_file()
    else:
        await msg.reply_text("Пришлите скриншот оплаты картинкой.")
        return
    data = bytes(await file.download_as_bytearray())

    state = load_state()
    target = ctx.user_data.pop("pay_for", None)
    mine = user_peers(state, update.effective_user.id)
    if target is None and not mine:
        target = NEW
    if target is None:
        # Непонятно, за что оплата — спрашиваем, картинку держим до ответа.
        ctx.user_data["pending_photo"] = (data, msg.caption)
        rows = [[InlineKeyboardButton(f"➕ Новый конфиг — {PRICE} ₽", callback_data=f"payfor:{NEW}")]]
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
        state["payments"][pid] = {"user": str(user.id), "target": target, "status": "pending",
                                  "created": int(time.time())}
        save_state(state)

    caption = (f"Оплата №{pid} — {PRICE} ₽\n{u['name']}\nID {user.id}\n"
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
            row.append(InlineKeyboardButton(f"🔄 Продлить — {PRICE} ₽", callback_data=f"renew:{pub}"))
        buttons = InlineKeyboardMarkup([row, [InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{pub}")]])
        await update.message.reply_text(f"{p['name']} — {peer_status(p)}", reply_markup=buttons)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=KEYBOARD)


async def on_user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action, _, arg = query.data.partition(":")
    await query.answer()

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
ADMIN_KEYBOARD = ReplyKeyboardMarkup([[ABTN_PAY, ABTN_USERS], [ABTN_GIVE, ABTN_STATS]], resize_keyboard=True)


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
        if target == NEW:
            pub, peer = create_peer(state, uid, time.time() + days * DAY)
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
        if target == NEW:
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
        f"{ABTN_PAY} — неразобранные оплаты\n"
        f"{ABTN_USERS} — клиенты и их конфиги: продлить или отключить\n"
        f"{ABTN_GIVE} — бесплатно выдать конфиг по ID\n"
        f"{ABTN_STATS} — кто подключён и трафик\n\n"
        "Команды: /send ID текст — написать клиенту, /broadcast текст — написать всем.",
        reply_markup=ADMIN_KEYBOARD)


@admin_only
async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    pending = [(pid, p) for pid, p in state["payments"].items() if p["status"] == "pending"]
    if not pending:
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
        f"Например: 123456789 {DAYS}\n\n"
        "Клиент получит новый конфиг бесплатно. ID видно в его оплатах и в «👥 Клиенты». "
        "Клиент должен хотя бы раз нажать /start в клиентском боте.")


@admin_only
async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.pop("await", None) != "give":
        await update.message.reply_text("Выберите действие на кнопках ниже.", reply_markup=ADMIN_KEYBOARD)
        return
    parts = update.message.text.split()
    if not parts or not parts[0].isdigit() or (len(parts) > 1 and not parts[1].isdigit()):
        ctx.user_data["await"] = "give"
        await update.message.reply_text(f"Нужно число — ID клиента. Например: 123456789 {DAYS}")
        return
    uid, days = int(parts[0]), int(parts[1]) if len(parts) > 1 else DAYS
    result = await grant(uid, NEW, days, paid=False)
    await update.message.reply_text(f"Готово: выдан конфиг.\n{result}", reply_markup=ADMIN_KEYBOARD)


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer()
        return
    action, _, arg = query.data.partition(":")

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
async def admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    names = {pub: f"{user_name(state, p['user'])} / {p['name']}" for pub, p in state["peers"].items()}
    now = time.time()
    rows = []
    for line in run(["awg", "show", IFACE, "dump"]).splitlines()[1:]:
        f = line.split("\t")
        pub, handshake, rx, tx = f[0], int(f[4]), int(f[5]), int(f[6])
        ago = "никогда" if not handshake else f"{int(now - handshake) // 60} мин назад"
        rows.append(f"{names.get(pub, pub[:8])}: вход {ago}, ↓{rx / 2**20:.0f} МБ ↑{tx / 2**20:.0f} МБ")
    active = sum(1 for p in state["peers"].values() if peer_active(p) and not is_admin(p["user"]))
    approved = sum(1 for p in state["payments"].values() if p["status"] == "approved")
    await update.message.reply_text(
        f"Оплаченных конфигов сейчас: {active}\nОдобренных оплат всего: {approved} "
        f"(≈ {approved * PRICE} ₽)\n\n" + ("\n".join(rows) or "Подключений нет."),
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
