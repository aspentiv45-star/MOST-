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
from datetime import datetime
from zoneinfo import ZoneInfo

import qrcode
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
CLIENT_DNS = os.environ.get("CLIENT_DNS", "1.1.1.1, 1.0.0.1")
MAX_PEERS = int(os.environ.get("MAX_PEERS", "3"))
PRICE = int(os.environ.get("PRICE", "200"))
DAYS = int(os.environ.get("DAYS", "30"))
PAY_FILE = os.environ.get("PAY_FILE", "/etc/most/pay.txt")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))

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
    f"3. После подтверждения нажмите «{BTN_NEW}» — бот пришлёт файл и QR-код.\n"
    "4. В приложении: «+» → импорт файла или сканирование QR-кода.\n"
    "5. Включите туннель и проверьте на ipinfo.io.\n\n"
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


def client_conf(peer, params):
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
        f"Endpoint = {ENDPOINT_HOST}:{params['ListenPort']}",
        "AllowedIPs = 0.0.0.0/0, ::/0",
        "PersistentKeepalive = 25",
    ]
    return "\n".join(lines) + "\n"


# ---------- клиентский бот ----------

async def send_peer(chat, peer):
    params = server_params()
    text = client_conf(peer, params)
    # Имя файла становится именем туннеля; на iOS оно не длиннее 15 символов.
    filename = f"{peer['name']}.conf"
    await chat.send_document(document=io.BytesIO(text.encode()), filename=filename,
                             caption=f"Конфиг {peer['name']}. Импортируйте файл или QR-код ниже.")
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
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Одобрить (+{DAYS} дн.)", callback_data=f"ok:{pid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"no:{pid}"),
    ]])
    # file_id одного бота не годится для другого — пересылаем саму картинку.
    for admin in ADMINS:
        try:
            await ADMIN_BOT.send_photo(admin, photo=io.BytesIO(data), caption=caption, reply_markup=buttons)
        except Exception:
            log.exception("не удалось отправить оплату админу %s", admin)
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
        names = {p["name"] for p in mine}
        n = next(i for i in range(1, 1000) if f"vpn{i}" not in names)
        priv = run(["awg", "genkey"])
        pub = run(["awg", "pubkey"], priv + "\n")
        peer = {"user": str(uid), "name": f"vpn{n}", "ip": next_free_ip(state, params),
                "priv": priv, "created": fmt_date(time.time())}
        state["peers"][pub] = peer
        write_server_conf(state)
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

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id in ADMINS:
            await func(update, ctx)
    return wrapper


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


@admin_only
async def admin_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Админ-панель MOST.\n\n"
        "Скриншоты оплат приходят сюда с кнопками «Одобрить» / «Отклонить».\n\n"
        "/users — клиенты и сроки подписок\n"
        "/stats — кто подключался и трафик\n"
        "/pending — неразобранные оплаты\n"
        f"/give ID ДНЕЙ — продлить вручную (например /give 123456 {DAYS})\n"
        "/take ID — отключить подписку\n"
        "/send ID текст — написать клиенту\n"
        "/broadcast текст — написать всем клиентам")


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMINS:
        await query.answer()
        return
    action, _, pid = query.data.partition(":")
    async with lock:
        state = load_state()
        payment = state["payments"].get(pid)
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
        await query.edit_message_caption(f"{query.message.caption}\n\n✅ Одобрено, подписка до {fmt_date(until)}")
        await USER_BOT.send_message(
            uid, f"Оплата подтверждена! Подписка активна до {fmt_date(until)}.\n"
                 f"Получите конфиг: «{BTN_NEW}» (если он у вас уже есть — он снова работает).",
            reply_markup=KEYBOARD)
    else:
        await query.edit_message_caption(f"{query.message.caption}\n\n❌ Отклонено")
        await USER_BOT.send_message(
            uid, "Оплата не подтверждена. Если это ошибка — пришлите скриншот ещё раз или напишите администратору.")


@admin_only
async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    now = time.time()
    rows = []
    for uid, u in sorted(state["users"].items(), key=lambda kv: -kv[1]["until"]):
        mark = "🟢" if u["until"] > now else "⚪️"
        until = fmt_date(u["until"]) if u["until"] else "—"
        rows.append(f"{mark} {u['name']} | ID {uid} | до {until} | конфигов: {len(user_peers(state, uid))}")
    text = "\n".join(rows) or "Клиентов пока нет."
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


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
    await update.message.reply_text(f"Активных подписок: {active}\n\n" + ("\n".join(rows) or "Подключений нет."))


@admin_only
async def admin_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    rows = [f"№{pid} — {state['users'].get(p['user'], {}).get('name', p['user'])}, {fmt_date(p['created'])}"
            for pid, p in state["payments"].items() if p["status"] == "pending"]
    await update.message.reply_text("\n".join(rows) or "Неразобранных оплат нет.")


@admin_only
async def admin_give(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 2 or not all(a.lstrip("-").isdigit() for a in ctx.args):
        await update.message.reply_text(f"Использование: /give ID ДНЕЙ, например /give 123456 {DAYS}")
        return
    uid, days = int(ctx.args[0]), int(ctx.args[1])
    until = await extend(uid, days)
    await update.message.reply_text(f"Подписка {uid} до {fmt_date(until)}.")
    try:
        await USER_BOT.send_message(uid, f"Подписка активна до {fmt_date(until)}.", reply_markup=KEYBOARD)
    except Exception:
        await update.message.reply_text("Клиенту написать не удалось — он ещё не нажимал /start в боте.")


@admin_only
async def admin_take(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 1 or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /take ID")
        return
    async with lock:
        state = load_state()
        if ctx.args[0] in state["users"]:
            state["users"][ctx.args[0]]["until"] = 0
        write_server_conf(state)
        save_state(state)
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

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("ошибка при обработке %s", update, exc_info=ctx.error)


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
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("pending", admin_pending))
    app.add_handler(CommandHandler("give", admin_give))
    app.add_handler(CommandHandler("take", admin_take))
    app.add_handler(CommandHandler("send", admin_send))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
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

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        expiry.cancel()
        for app in (user_app, admin_app):
            await app.updater.stop()
            await app.stop()


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
