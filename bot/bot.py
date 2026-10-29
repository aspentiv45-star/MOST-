#!/usr/bin/env python3
"""MOST — Telegram-бот, который выдаёт конфиги AmneziaWG.

Работает на том же сервере, где поднят AmneziaWG (интерфейс awg0).
Новый клиент добавляется в /etc/amnezia/amneziawg/awg0.conf и сразу
применяется через `awg syncconf` — без перезапуска туннеля.
"""
import asyncio
import io
import ipaddress
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime

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
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
IFACE = os.environ.get("AWG_IFACE", "awg0")
CONF = os.environ.get("AWG_CONF", f"/etc/amnezia/amneziawg/{IFACE}.conf")
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/most/state.json")
ENDPOINT_HOST = os.environ["ENDPOINT_HOST"]
CLIENT_DNS = os.environ.get("CLIENT_DNS", "1.1.1.1, 1.0.0.1")
MAX_PEERS = int(os.environ.get("MAX_PEERS", "3"))

# Параметры обфускации AmneziaWG: клиенту они нужны точно такими же, как у сервера.
OBFS_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
             "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")

BTN_NEW = "➕ Новый конфиг"
BTN_LIST = "📄 Мои конфиги"
BTN_HELP = "❓ Как подключить"
KEYBOARD = ReplyKeyboardMarkup([[BTN_NEW, BTN_LIST], [BTN_HELP]], resize_keyboard=True)

HELP_TEXT = (
    "Как подключиться:\n\n"
    "1. Установите приложение AmneziaVPN или AmneziaWG (App Store / Google Play / amnezia.org).\n"
    "2. Нажмите «➕ Новый конфиг» — бот пришлёт файл и QR-код.\n"
    "3. В приложении: «+» → импорт из файла или сканирование QR-кода.\n"
    "4. Включите туннель и проверьте на ipinfo.io — должны быть Нидерланды.\n\n"
    "Один конфиг — одно устройство. Не пересылайте конфиги другим людям."
)

log = logging.getLogger("most")
lock = asyncio.Lock()


def run(cmd, data=None):
    return subprocess.run(cmd, input=data, capture_output=True, text=True, check=True).stdout.strip()


# ---------- состояние ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"allowed": {}, "peers": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE))
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def is_allowed(state, uid):
    return uid in ADMINS or str(uid) in state["allowed"]


def user_peers(state, uid):
    return {pub: p for pub, p in state["peers"].items() if p["user"] == str(uid)}


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


def server_public_key(params):
    return run(["awg", "pubkey"], params["PrivateKey"] + "\n")


def write_server_conf(state):
    """Пересобирает awg0.conf из [Interface] и списка клиентов и применяет его на лету."""
    out = interface_section()
    for pub, p in state["peers"].items():
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
        f"PublicKey = {server_public_key(params)}",
        f"Endpoint = {ENDPOINT_HOST}:{params['ListenPort']}",
        "AllowedIPs = 0.0.0.0/0, ::/0",
        "PersistentKeepalive = 25",
    ]
    return "\n".join(lines) + "\n"


# ---------- отправка конфига ----------

async def send_peer(chat, peer):
    text = client_conf(peer, server_params())
    # Имя файла становится именем туннеля; на iOS оно не длиннее 15 символов.
    await chat.send_document(document=io.BytesIO(text.encode()), filename=f"{peer['name']}.conf",
                             caption=f"Конфиг {peer['name']}. Импортируйте файл или QR-код ниже.")
    png = io.BytesIO()
    qrcode.make(text).save(png, format="PNG")
    png.seek(0)
    await chat.send_photo(photo=png, caption=f"QR-код для {peer['name']}")


def user_label(user):
    label = user.full_name or str(user.id)
    return f"{label} (@{user.username})" if user.username else label


# ---------- обработчики ----------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = load_state()
    if is_allowed(state, user.id):
        await update.message.reply_text("Привет! Выберите действие.", reply_markup=KEYBOARD)
        return

    await update.message.reply_text(
        f"Доступа пока нет. Запрос отправлен администратору.\nВаш ID: {user.id}")
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Разрешить", callback_data=f"allow:{user.id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{user.id}"),
    ]])
    for admin in ADMINS:
        try:
            await ctx.bot.send_message(admin, f"Запрос доступа: {user_label(user)}, ID {user.id}",
                                       reply_markup=buttons)
        except Exception:
            log.exception("не удалось написать админу %s", admin)


async def new_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    async with lock:
        state = load_state()
        if not is_allowed(state, uid):
            await update.message.reply_text("Нет доступа. Нажмите /start, чтобы запросить его.")
            return
        mine = user_peers(state, uid)
        if len(mine) >= MAX_PEERS and uid not in ADMINS:
            await update.message.reply_text(
                f"У вас уже {len(mine)} конфига — это максимум. Удалите ненужный в «{BTN_LIST}».")
            return

        params = server_params()
        names = {p["name"] for p in mine}
        n = next(i for i in range(1, 1000) if f"nl{i}" not in names)
        priv = run(["awg", "genkey"])
        pub = run(["awg", "pubkey"], priv + "\n")
        peer = {"user": str(uid), "name": f"nl{n}", "ip": next_free_ip(state, params),
                "priv": priv, "created": datetime.now().strftime("%Y-%m-%d %H:%M")}
        state["peers"][pub] = peer
        write_server_conf(state)
        save_state(state)

    await send_peer(update.message.chat, peer)


async def list_configs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state()
    if not is_allowed(state, uid):
        await update.message.reply_text("Нет доступа. Нажмите /start, чтобы запросить его.")
        return
    mine = user_peers(state, uid)
    if not mine:
        await update.message.reply_text(f"Конфигов пока нет. Нажмите «{BTN_NEW}».")
        return
    for pub, p in mine.items():
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬇️ Скачать", callback_data=f"get:{pub}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{pub}"),
        ]])
        await update.message.reply_text(f"{p['name']} — {p['ip']}, создан {p['created']}",
                                        reply_markup=buttons)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = HELP_TEXT
    if update.effective_user.id in ADMINS:
        text += ("\n\nКоманды администратора:\n"
                 "/users — кому разрешён доступ\n"
                 "/stats — кто сейчас подключён\n"
                 "/allow ID — разрешить доступ\n"
                 "/deny ID — забрать доступ и удалить конфиги")
    await update.message.reply_text(text, reply_markup=KEYBOARD)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    action, _, arg = query.data.partition(":")
    await query.answer()

    if action in ("allow", "reject"):
        if uid not in ADMINS:
            return
        if action == "allow":
            await allow_user(ctx, int(arg))
            await query.edit_message_text(query.message.text + "\n✅ Разрешено")
        else:
            await query.edit_message_text(query.message.text + "\n❌ Отклонено")
            await ctx.bot.send_message(int(arg), "Администратор отклонил запрос доступа.")
        return

    async with lock:
        state = load_state()
        peer = state["peers"].get(arg)
        if not peer or (peer["user"] != str(uid) and uid not in ADMINS):
            await query.edit_message_text("Конфиг не найден.")
            return
        if action == "del":
            del state["peers"][arg]
            write_server_conf(state)
            save_state(state)
            await query.edit_message_text(f"{peer['name']} удалён.")
            return
    if action == "get":
        await send_peer(query.message.chat, peer)


async def allow_user(ctx, target):
    async with lock:
        state = load_state()
        try:
            chat = await ctx.bot.get_chat(target)
            name = chat.full_name or str(target)
        except Exception:
            name = str(target)
        state["allowed"][str(target)] = {"name": name}
        save_state(state)
    try:
        await ctx.bot.send_message(target, "Доступ открыт! Нажмите «➕ Новый конфиг».", reply_markup=KEYBOARD)
    except Exception:
        log.warning("не удалось уведомить %s", target)


async def allow_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /allow ID")
        return
    await allow_user(ctx, int(ctx.args[0]))
    await update.message.reply_text(f"Доступ для {ctx.args[0]} открыт.")


async def deny_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /deny ID")
        return
    target = ctx.args[0]
    async with lock:
        state = load_state()
        state["allowed"].pop(target, None)
        removed = [pub for pub, p in state["peers"].items() if p["user"] == target]
        for pub in removed:
            del state["peers"][pub]
        write_server_conf(state)
        save_state(state)
    await update.message.reply_text(f"Доступ для {target} закрыт, удалено конфигов: {len(removed)}.")


async def users_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    state = load_state()
    rows = []
    for uid, info in state["allowed"].items():
        count = len(user_peers(state, uid))
        rows.append(f"{info['name']} — ID {uid}, конфигов: {count}")
    await update.message.reply_text("\n".join(rows) or "Пока никому не выдан доступ.")


async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    state = load_state()
    names = {}
    for pub, p in state["peers"].items():
        owner = state["allowed"].get(p["user"], {}).get("name", p["user"])
        names[pub] = f"{owner} / {p['name']}"
    rows = []
    now = time.time()
    for line in run(["awg", "show", IFACE, "dump"]).splitlines()[1:]:
        fields = line.split("\t")
        pub, handshake, rx, tx = fields[0], int(fields[4]), int(fields[5]), int(fields[6])
        ago = "никогда" if not handshake else f"{int(now - handshake) // 60} мин назад"
        rows.append(f"{names.get(pub, pub[:8])}: вход {ago}, ↓{rx / 2**20:.1f} МБ ↑{tx / 2**20:.1f} МБ")
    await update.message.reply_text("\n".join(rows) or "Клиентов нет.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("ошибка при обработке %s", update, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        await ctx.bot.send_message(update.effective_chat.id, "Что-то пошло не так, попробуйте ещё раз.")


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_config))
    app.add_handler(CommandHandler("list", list_configs))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("deny", deny_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_NEW]), new_config))
    app.add_handler(MessageHandler(filters.Text([BTN_LIST]), list_configs))
    app.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
