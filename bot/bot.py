#!/usr/bin/env python3
"""MOST — два Telegram-бота для продажи доступа к AmneziaWG.

Клиентский бот: оплата по скриншоту, выдача и удаление конфигов.
Админ-бот: одобрение оплат, продление подписок, статистика.

Оба бота работают в одном процессе на сервере с AmneziaWG (awg0).
Клиенты с активной подпиской попадают в /etc/amnezia/amneziawg/awg0.conf,
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
MAX_PEERS = int(os.environ.get("MAX_PEERS", "3"))
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

# Параметры обфускации AmneziaWG: клиенту они нужны точно такими же, как у сервера.
OBFS_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
             "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")

BTN_PAY = f"💳 Оплатить {PRICE} ₽"
BTN_SUB = "📅 Моя подписка"
BTN_NEW = "➕ Новый конфиг"
BTN_LIST = "📄 Мои конфиги"
BTN_HELP = "❓ Как подключить"
KEYBOARD = ReplyKeyboardMarkup([[BTN_PAY, BTN_SUB], [BTN_NEW, BTN_LIST], [BTN_HELP]], resize_keyboard=True)

HELP_TEXT = (
    "Как подключиться:\n\n"
    "1. Установите AmneziaVPN или AmneziaWG (App Store / Google Play / amnezia.org).\n"
    f"2. Оплатите подписку: «{BTN_PAY}» и пришлите скриншот перевода.\n"
    f"3. После подтверждения нажмите «{BTN_NEW}» — бот пришлёт файл.\n"
    "4. В приложении: «+» → импорт из файла (на iPhone: нажмите на файл → «Поделиться» → Amnezia).\n"
    "5. Включите туннель и проверьте на ipinfo.io — должны быть Нидерланды.\n\n"
    + ("Российские сайты и приложения работают напрямую, VPN выключать не нужно.\n\n" if SPLIT_RU else "")
    + ("Если на мобильном интернете обычный конфиг не подключается — "
       "включите второй, с «-wl» в названии.\n\n" if os.environ.get("RELAY_HOST") else "") +
    "Один конфиг — одно устройство. Не пересылайте конфиги другим людям."
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
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE))
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def get_user(state, user):
    u = state["users"].setdefault(str(user.id), {"name": "", "until": 0, "warned": 0})
    u["name"] = f"{user.full_name} (@{user.username})" if user.username else (user.full_name or str(user.id))
    return u


def is_active(state, uid):
    if int(uid) in ADMINS:
        return True
    return state["users"].get(str(uid), {}).get("until", 0) > time.time()


def user_peers(state, uid):
    return {pub: p for pub, p in state["peers"].items() if p["user"] == str(uid)}


def sub_status(state, uid):
    if int(uid) in ADMINS:
        return "Вы администратор — доступ без ограничений."
    until = state["users"].get(str(uid), {}).get("until", 0)
    if until > time.time():
        days = int((until - time.time()) // DAY)
        return f"Подписка активна до {fmt_date(until)} (осталось дней: {days})."
    if until:
        return f"Подписка закончилась {fmt_date(until)}. Продлите её: «{BTN_PAY}»."
    return f"Подписки нет. {DAYS} дней — {PRICE} ₽. Нажмите «{BTN_PAY}»."


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
    """Пересобирает awg0.conf: в нём только клиенты с активной подпиской."""
    out = interface_section()
    for pub, p in state["peers"].items():
        if is_active(state, p["user"]):
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


def next_free_ip(state, params):
    iface = ipaddress.ip_interface(params["Address"].split(",")[0].strip())
    used = {p["ip"] for p in state["peers"].values()} | {str(iface.ip)}
    for host in iface.network.hosts():
        if str(host) not in used:
            return str(host)
    raise RuntimeError("в подсети закончились адреса")


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

async def send_peer(chat, peer):
    params = server_params()
    # Имя файла становится именем туннеля; на iOS оно не длиннее 15 символов.
    variants = [(f"{peer['name']}.conf", ENDPOINT_HOST, "обычное подключение")]
    if RELAY_HOST:
        variants.append((f"{peer['name']}-wl.conf", RELAY_HOST,
                         "через вход в РФ — для мобильного интернета с белыми списками"))
    for filename, host, note in variants:
        text = client_conf(peer, params, host)
        await chat.send_document(document=io.BytesIO(text.encode()), filename=filename,
                                 caption=f"{filename}: {note}")
        # Конфиг с раздельным туннелем не помещается в QR-код — тогда только файл.
        if len(text) < 2000:
            png = io.BytesIO()
            qrcode.make(text).save(png, format="PNG")
            png.seek(0)
            await chat.send_photo(photo=png, caption=f"QR-код: {filename}")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with lock:
        state = load_state()
        get_user(state, update.effective_user)
        save_state(state)
    await update.message.reply_text(
        f"Привет! Это VPN на AmneziaWG.\n{DAYS} дней — {PRICE} ₽.\n\n{sub_status(state, update.effective_user.id)}",
        reply_markup=KEYBOARD)


def pay_text():
    # Файл читается при каждом нажатии: реквизиты можно менять без перезапуска бота.
    try:
        with open(PAY_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Реквизиты не указаны — напишите администратору."


async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Подписка на {DAYS} дней — {PRICE} ₽.\n\n{pay_text()}\n\n"
        "После оплаты пришлите сюда скриншот перевода одним фото. "
        "Администратор проверит его и включит подписку.")


async def subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    await update.message.reply_text(sub_status(state, update.effective_user.id))


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

    async with lock:
        state = load_state()
        u = get_user(state, update.effective_user)
        pid = str(state["next_payment"])
        state["next_payment"] += 1
        state["payments"][pid] = {"user": str(update.effective_user.id), "status": "pending",
                                  "created": int(time.time())}
        save_state(state)

    caption = (f"Оплата №{pid}\n{u['name']}\nID {update.effective_user.id}\n"
               f"{sub_status(state, update.effective_user.id)}")
    if msg.caption:
        caption += f"\nКомментарий: {msg.caption}"
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
    await msg.reply_text("Скриншот отправлен администратору. Как только он подтвердит оплату, придёт сообщение.")


async def new_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with lock:
        state = load_state()
        get_user(state, update.effective_user)
        if not is_active(state, uid):
            save_state(state)
            await update.message.reply_text(sub_status(state, uid))
            return
        mine = user_peers(state, uid)
        if len(mine) >= MAX_PEERS and uid not in ADMINS:
            await update.message.reply_text(
                f"У вас уже {len(mine)} конфига — это максимум. Удалите ненужный в «{BTN_LIST}».")
            return

        params = server_params()
        names = {p["name"] for p in mine.values()}
        old_state = json.loads(json.dumps(state))
        n = next(i for i in range(1, 1000) if f"vpn{i}" not in names)
        priv = run(["awg", "genkey"])
        pub = run(["awg", "pubkey"], priv + "\n")
        peer = {"user": str(uid), "name": f"vpn{n}", "ip": next_free_ip(state, params),
                "priv": priv, "created": fmt_date(time.time())}
        state["peers"][pub] = peer
        try:
            write_server_conf(state)
        except Exception as e:
            try:
                write_server_conf(old_state)
            except Exception:
                pass
            await report_error(f"Не удалось добавить конфиг для {uid}", e)
            await update.message.reply_text("Не получилось создать конфиг. Администратор уже знает, попробуйте позже.")
            return
        save_state(state)

    await send_peer(update.message.chat, peer)


async def list_configs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state()
    mine = user_peers(state, uid)
    if not mine:
        await update.message.reply_text(f"Конфигов пока нет. Нажмите «{BTN_NEW}».")
        return
    if not is_active(state, uid):
        await update.message.reply_text("Конфиги отключены, пока подписка не продлена.\n" + sub_status(state, uid))
    for pub, p in mine.items():
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬇️ Скачать", callback_data=f"get:{pub}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{pub}"),
        ]])
        await update.message.reply_text(f"{p['name']}, создан {p['created']}", reply_markup=buttons)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=KEYBOARD)


async def on_user_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action, _, pub = query.data.partition(":")
    await query.answer()
    async with lock:
        state = load_state()
        peer = state["peers"].get(pub)
        if not peer or peer["user"] != str(uid):
            await query.edit_message_text("Конфиг не найден.")
            return
        if action == "del":
            del state["peers"][pub]
            write_server_conf(state)
            save_state(state)
            await query.edit_message_text(f"{peer['name']} удалён.")
            return
    if action == "get":
        if not is_active(state, uid):
            await query.message.reply_text(sub_status(state, uid))
            return
        await send_peer(query.message.chat, peer)


# ---------- админ-бот ----------

ABTN_PAY = "⏳ Оплаты"
ABTN_USERS = "👥 Клиенты"
ABTN_GIVE = "🎁 Выдать подписку"
ABTN_STATS = "📊 Статистика"
ADMIN_KEYBOARD = ReplyKeyboardMarkup([[ABTN_PAY, ABTN_USERS], [ABTN_GIVE, ABTN_STATS]], resize_keyboard=True)


def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id in ADMINS:
            await func(update, ctx)
    return wrapper


def payment_buttons(pid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Одобрить (+{DAYS} дн.)", callback_data=f"ok:{pid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{pid}"),
    ]])


def user_buttons(uid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"➕ {DAYS} дней", callback_data=f"give:{uid}"),
        InlineKeyboardButton("⛔️ Отключить", callback_data=f"take:{uid}"),
    ]])


def user_line(state, uid):
    u = state["users"].get(str(uid), {"name": str(uid), "until": 0})
    mark = "🟢" if u["until"] > time.time() else "⚪️"
    until = f"до {fmt_date(u['until'])}" if u["until"] else "без подписки"
    return f"{mark} {u['name']}\nID {uid} · {until} · конфигов: {len(user_peers(state, uid))}"


async def extend(uid, days):
    """Продлевает подписку и сразу включает конфиги. Возвращает новую дату окончания."""
    async with lock:
        state = load_state()
        u = state["users"].setdefault(str(uid), {"name": str(uid), "until": 0, "warned": 0})
        u["until"] = max(u["until"], time.time()) + days * DAY
        u["warned"] = 0
        write_server_conf(state)
        save_state(state)
        return u["until"]


async def take(uid):
    async with lock:
        state = load_state()
        if str(uid) in state["users"]:
            state["users"][str(uid)]["until"] = 0
        write_server_conf(state)
        save_state(state)


async def notify_extended(uid, until):
    """Сообщает клиенту о продлении. False — если клиент ещё не писал боту."""
    try:
        await USER_BOT.send_message(
            uid, f"Подписка активна до {fmt_date(until)}.\n"
                 f"Получите конфиг: «{BTN_NEW}» (если он у вас уже есть — он снова работает).",
            reply_markup=KEYBOARD)
        return True
    except Exception:
        return False


@admin_only
async def admin_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Админ-панель MOST.\n\n"
        "Скриншоты оплат приходят сюда сами — с кнопками «Одобрить» / «Отклонить».\n\n"
        f"{ABTN_PAY} — неразобранные оплаты\n"
        f"{ABTN_USERS} — клиенты: продлить или отключить\n"
        f"{ABTN_GIVE} — выдать подписку по ID\n"
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
        caption = f"Оплата №{pid} от {fmt_date(p['created'])}\n{user_line(state, p['user'])}"
        if p.get("photo"):
            await update.message.reply_photo(p["photo"], caption=caption, reply_markup=payment_buttons(pid))
        else:
            await update.message.reply_text(caption, reply_markup=payment_buttons(pid))


@admin_only
async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    users = sorted(state["users"], key=lambda uid: -state["users"][uid]["until"])
    if not users:
        await update.message.reply_text("Клиентов пока нет.", reply_markup=ADMIN_KEYBOARD)
        return
    active = sum(1 for u in state["users"].values() if u["until"] > time.time())
    await update.message.reply_text(f"Клиентов: {len(users)}, с подпиской: {active}."
                                    + (" Показаны первые 30." if len(users) > 30 else ""))
    for uid in users[:30]:
        await update.message.reply_text(user_line(state, uid), reply_markup=user_buttons(uid))


@admin_only
async def admin_give_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["await"] = "give"
    await update.message.reply_text(
        f"Пришлите ID клиента и, через пробел, число дней (по умолчанию {DAYS}).\n"
        f"Например: 123456789 {DAYS}\n\n"
        "ID клиента видно в его оплатах и в «👥 Клиенты». "
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
    until = await extend(uid, days)
    told = await notify_extended(uid, until)
    await update.message.reply_text(
        f"Готово: подписка до {fmt_date(until)}.\n{user_line(load_state(), uid)}"
        + ("" if told else "\n\nКлиенту написать не удалось — он ещё не нажимал /start в клиентском боте."),
        reply_markup=ADMIN_KEYBOARD)


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMINS:
        await query.answer()
        return
    action, _, arg = query.data.partition(":")

    if action in ("give", "take"):
        uid = int(arg)
        if action == "give":
            until = await extend(uid, DAYS)
            await notify_extended(uid, until)
            await query.answer(f"Продлено до {fmt_date(until)}")
        else:
            await take(uid)
            await query.answer("Подписка отключена")
        await query.edit_message_text(user_line(load_state(), uid), reply_markup=user_buttons(uid))
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
        until = await extend(uid, DAYS)
        result = f"✅ Одобрено, подписка до {fmt_date(until)}"
        await notify_extended(uid, until)
    else:
        result = "❌ Отклонено"
        await USER_BOT.send_message(
            uid, "Оплата не подтверждена. Если это ошибка — пришлите скриншот ещё раз или напишите администратору.")
    if query.message.photo:
        await query.edit_message_caption(f"{query.message.caption}\n\n{result}")
    else:
        await query.edit_message_text(f"{query.message.text}\n\n{result}")


@admin_only
async def admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    names = {pub: f"{state['users'].get(p['user'], {}).get('name', p['user'])} / {p['name']}"
             for pub, p in state["peers"].items()}
    now = time.time()
    rows = []
    for line in run(["awg", "show", IFACE, "dump"]).splitlines()[1:]:
        f = line.split("\t")
        pub, handshake, rx, tx = f[0], int(f[4]), int(f[5]), int(f[6])
        ago = "никогда" if not handshake else f"{int(now - handshake) // 60} мин назад"
        rows.append(f"{names.get(pub, pub[:8])}: вход {ago}, ↓{rx / 2**20:.0f} МБ ↑{tx / 2**20:.0f} МБ")
    active = sum(1 for u in state["users"].values() if u["until"] > now)
    await update.message.reply_text(f"Активных подписок: {active}\n\n" + ("\n".join(rows) or "Подключений нет."),
                                    reply_markup=ADMIN_KEYBOARD)


@admin_only
async def admin_give(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await admin_give_start(update, ctx)
        return
    update.message.text = " ".join(ctx.args)
    ctx.user_data["await"] = "give"
    await admin_text(update, ctx)


@admin_only
async def admin_take(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 1 or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /take ID")
        return
    await take(int(ctx.args[0]))
    await update.message.reply_text(f"Подписка {ctx.args[0]} отключена, конфиги не работают.")


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
        expired = False
        for uid, u in state["users"].items():
            if not u["until"]:
                continue
            left = u["until"] - now
            if left <= 0 and u["warned"] < 3:
                u["warned"] = 3
                expired = True
                notify.append((uid, f"Подписка закончилась, конфиги отключены. Продлить: «{BTN_PAY}»."))
            elif 0 < left <= DAY and u["warned"] < 2:
                u["warned"] = 2
                notify.append((uid, f"Подписка закончится {fmt_date(u['until'])} — меньше чем через сутки. "
                                    f"Продлить: «{BTN_PAY}»."))
            elif DAY < left <= 3 * DAY and u["warned"] < 1:
                u["warned"] = 1
                notify.append((uid, f"Подписка закончится {fmt_date(u['until'])}. Продлить: «{BTN_PAY}»."))
        if expired:
            write_server_conf(state)
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
        except Exception:
            log.exception("ошибка проверки подписок")
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
    app.add_handler(MessageHandler(filters.Text([BTN_PAY]), pay))
    app.add_handler(MessageHandler(filters.Text([BTN_SUB]), subscription))
    app.add_handler(MessageHandler(filters.Text([BTN_NEW]), new_config))
    app.add_handler(MessageHandler(filters.Text([BTN_LIST]), list_configs))
    app.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_screenshot))
    app.add_handler(CallbackQueryHandler(on_user_button))
    app.add_error_handler(on_error)
    return app


def build_admin_app():
    app = Application.builder().token(ADMIN_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], admin_start))
    app.add_handler(CommandHandler(["users"], admin_users))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(CommandHandler("give", admin_give))
    app.add_handler(CommandHandler("take", admin_take))
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
        for app in (user_app, admin_app):
            await app.start()
            await app.updater.start_polling()
        expiry = asyncio.create_task(expiry_loop())
        split_task = asyncio.create_task(split_loop())

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        expiry.cancel()
        split_task.cancel()
        for app in (user_app, admin_app):
            await app.updater.stop()
            await app.stop()


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
