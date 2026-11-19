"""Telegram Mini App aspentiv: веб-сервер внутри клиентского бота.

Страница (miniapp.html) и API на 127.0.0.1:MINIAPP_PORT; снаружи — nginx с HTTPS (miniapp-setup.sh).
Кто открыл приложение, Telegram сообщает в initData, подписанных токеном бота: подпись проверяем
на каждый запрос, отдельного входа нет. Данные — та же база, что у бота (state.json), через функции бота.
"""
import hashlib
import hmac
import io
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web

log = logging.getLogger("most.miniapp")
HERE = os.path.dirname(os.path.abspath(__file__))
B = None  # модуль бота: функции и настройки берём у него


def check_init_data(init_data, token, max_age=86400):
    """Проверка подписи initData (core.telegram.org/bots/webapps). -> dict пользователя или None."""
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = fields.pop("hash", "")
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    if time.time() - int(fields.get("auth_date", "0") or 0) > max_age:
        return None
    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None


def file_token(pub, ttl=900):
    """Подписанная ссылка на файл конфига: действует ttl секунд, подделать без токена бота нельзя."""
    exp = int(time.time()) + ttl
    sig = hmac.new(B.TOKEN.encode(), f"conf:{pub}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def file_token_ok(pub, token):
    exp, _, sig = (token or "").partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    want = hmac.new(B.TOKEN.encode(), f"conf:{pub}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(want, sig)


def url_id(pub):
    """Ключ конфига (base64 с / и +) — в вид, пригодный для пути в ссылке, и обратно."""
    return pub.replace("+", "-").replace("/", "_")


def pub_from_url(uid_):
    return uid_.replace("-", "+").replace("_", "/")


class Fail(Exception):
    def __init__(self, text, status=400):
        super().__init__(text)
        self.text, self.status = text, status


async def auth(request):
    """Пользователь из initData; только пущенные в бота и не заблокированные."""
    user = check_init_data(request.headers.get("X-Init-Data", ""), B.TOKEN)
    if not user:
        raise Fail("Откройте приложение из бота заново.", 401)
    state = B.load_state()
    uid = user["id"]
    if not B.is_approved(state, uid):
        raise Fail("Сначала откройте бота и нажмите «Старт».", 403)
    until = B.blocked_until(state, uid)
    if until:
        raise Fail(B.blocked_text(until), 403)
    return uid, user


def device_configs(state, uid):
    """Конфиги для устройств (роутеры в приложении не показываем)."""
    hs = B.last_handshakes()
    out = []
    for pub, p in sorted(B.user_peers(state, uid).items(), key=lambda kv: kv[1]["name"]):
        if p.get("kind") in B.ROUTER_KINDS:
            continue
        out.append({
            "id": pub, "name": p["name"], "label": p.get("label") or "", "title": B.title(p),
            "os": p.get("os") or "", "until": int(p.get("until") or 0),
            "until_text": B.fmt_date(p["until"]) if p.get("until") else "",
            "active": B.peer_active(p), "trial": bool(p.get("trial") and not p.get("paid")),
            "online": B.is_online(hs.get(pub, 0)), "mode": B.mode_of(p), "op": p.get("op") or "",
            "admin": B.is_admin(p["user"]),
        })
    return out


def me(uid, user):
    state = B.load_state()
    u = state["users"].get(str(uid), {})
    try:
        bot_name = B.USER_BOT.username
    except Exception:
        bot_name = ""
    plans = {str(m): {"days": d, "price": pr} for m, d, pr in B.plans(state, B.NEW)}
    return {
        "name": user.get("first_name") or u.get("name") or "",
        "balance": u.get("balance", 0),
        "configs": device_configs(state, uid),
        "plans": plans,
        "topup_min": B.TOPUP_MIN,
        "amounts": [a for a in B.TOPUP_AMOUNTS if a >= B.TOPUP_MIN],
        "stars": {"on": B.STARS_ON, "discount": B.STARS_DISCOUNT, "per_rub": 1 / B.star_rub()},
        "transfer": os.path.exists(B.PAY_FILE),
        "modes": {k: v for k, v in B.MODES.items()},
        "operators": B.OPERATORS,
        "ref": {"link": f"https://t.me/{bot_name}?start=ref_{uid}" if bot_name else "",
                "count": u.get("ref_count", 0), "need": B.REF_NEED, "days": B.REF_DAYS},
        "bot": bot_name,
        "trial_days": B.TRIAL_DAYS,
    }


def own_peer(state, uid, pub):
    p = state["peers"].get(pub)
    if not p or p["user"] != str(uid) or p.get("kind") in B.ROUTER_KINDS:
        raise Fail("Конфиг не найден.", 404)
    return p


def conf_text(peer):
    params = B.server_params(B.peer_iface(peer)[1])
    filename, host, port = B.peer_variants(peer)[0]
    return filename, B.client_conf(peer, params, host, port)


# ---------- обработчики ----------

async def api(request):
    """Все действия приложения: POST /api/<действие> с JSON."""
    action = request.match_info["action"]
    try:
        uid, user = await auth(request)
        try:
            body = await request.json() if request.can_read_body else {}
        except ValueError:
            body = {}
        if action not in ACTIONS:
            raise Fail("Нет такого действия.", 404)
        result = await ACTIONS[action](uid, user, body)
        return web.json_response({"ok": True, **(result or {})})
    except Fail as e:
        return web.json_response({"ok": False, "error": e.text}, status=e.status)
    except Exception:
        log.exception("miniapp %s", action)
        return web.json_response({"ok": False, "error": "Что-то пошло не так. Попробуйте ещё раз."}, status=500)


async def a_me(uid, user, body):
    return {"me": me(uid, user)}


async def a_mode(uid, user, body):
    pub, mode = body.get("id"), body.get("mode")
    if mode not in B.MODES:
        raise Fail("Нет такого режима.")
    async with B.lock:
        state = B.load_state()
        p = own_peer(state, uid, pub)
        if mode == "full":
            p.pop("mode", None)
        else:
            p["mode"] = mode
        B.save_state(state)
    return {"me": me(uid, user), "note": f"Режим «{B.MODES[mode]}». Скачайте новый файл — нажмите кнопку подключения."}


async def a_op(uid, user, body):
    pub, op = body.get("id"), body.get("op")
    if op not in B.OPERATORS:
        raise Fail("Нет такого оператора.")
    async with B.lock:
        state = B.load_state()
        p = own_peer(state, uid, pub)
        p["op"] = op
        state["users"].setdefault(str(uid), {"name": user.get("first_name") or str(uid)})["op"] = op
        B.save_state(state)
    on = os.environ.get("OP_PROFILES", "0") == "1"
    return {"me": me(uid, user),
            "note": f"Оператор: {B.OPERATORS[op]}." + (" Скачайте новый файл." if on else "")}


async def a_connect(uid, user, body):
    """Конфиг для подключения: ссылка на файл (скачать в приложении) и файл в чат с ботом."""
    state = B.load_state()
    p = own_peer(state, uid, body.get("id"))
    if not B.peer_active(p):
        raise Fail("Срок закончился — продлите конфиг.", 402)
    filename, _ = conf_text(p)
    base = os.environ.get("MINIAPP_URL", "").rstrip("/")
    pub = body.get("id")
    url = f"{base}/conf/{url_id(pub)}/{filename}?t={file_token(pub)}" if base else ""
    if body.get("chat"):
        how = "key" if p.get("os") == "android" else "ios" if p.get("os") == "ios" else "file"
        await B.deliver_peer(uid, p, how)
    return {"url": url, "filename": filename}


async def a_renew(uid, user, body):
    months = int(body.get("months") or 1)
    target = body.get("id") or B.NEW
    state = B.load_state()
    if target != B.NEW:
        own_peer(state, uid, target)
    ok, text = await B.buy_with_balance(uid, target, months)
    return {"me": me(uid, user), "done": ok, "note": text}


async def a_stars(uid, user, body):
    amount = int(body.get("amount") or 0)
    if not B.STARS_ON or amount < B.TOPUP_MIN or amount > 100000:
        raise Fail(f"Сумма — от {B.TOPUP_MIN} ₽.")
    stars = B.stars_for(amount)
    then = body.get("then")  # [за что, месяцев] — купить после пополнения
    months, target = (int(then[1]), str(then[0])) if isinstance(then, list) and len(then) == 2 else (0, "")
    link = await B.USER_BOT.create_invoice_link(
        title=f"Пополнение баланса на {amount} ₽",
        description="Сервис aspentiv VPN: пополнение баланса для покупки и продления подписки.",
        payload=f"t:{amount}:{stars}:{months}:{target}", currency="XTR",
        prices=[B.LabeledPrice(f"Пополнение на {amount} ₽", stars)])
    return {"link": link, "stars": stars}


async def a_transfer(uid, user, body):
    amount = int(body.get("amount") or 0)
    if amount < B.TOPUP_MIN or amount > 100000:
        raise Fail(f"Сумма — от {B.TOPUP_MIN} ₽.")
    then = body.get("then")
    then = (str(then[0]), int(then[1])) if isinstance(then, list) and len(then) == 2 else None
    ok, text = await B.request_requisites(uid, amount, then)
    return {"note": text, "sent": ok}


async def a_help(uid, user, body):
    what = body.get("what")
    if what in B.VIDEOS:
        await B.send_guide_video(uid, what)
        return {"note": "Видео отправлено в чат с ботом."}
    if what == "support":
        if B.USER_APP is not None:
            # как после «🆘 Нужна помощь»: следующее сообщение клиента станет обращением
            data = B.USER_APP.user_data[uid]
            data["await"] = "ticket"
            data.pop("ticket_topic", None)
        await B.USER_BOT.send_message(uid, B.SUPPORT_TEXT, reply_markup=B.SUPPORT_BUTTONS)
        return {"note": "Откройте чат с ботом — выберите тему или напишите сообщение."}
    if what == "refund":
        await B.USER_BOT.send_message(uid, "↩️ Возврат средств — выберите причину:",
                                      reply_markup=B.InlineKeyboardMarkup([[B.InlineKeyboardButton(
                                          "↩️ Вернуть средства", callback_data="rfd:")]]))
        return {"note": "Откройте чат с ботом и выберите причину возврата."}
    raise Fail("Нет такого раздела.")


ACTIONS = {"me": a_me, "mode": a_mode, "op": a_op, "connect": a_connect, "renew": a_renew,
           "stars": a_stars, "transfer": a_transfer, "help": a_help}


async def conf_file(request):
    """GET /conf/<конфиг>/<имя>.conf?t=… — файл конфига по подписанной ссылке (её даёт /api/connect)."""
    pub = pub_from_url(request.match_info["pub"])
    if not file_token_ok(pub, request.query.get("t")):
        return web.Response(status=403, text="Ссылка устарела — нажмите кнопку в приложении ещё раз.")
    state = B.load_state()
    p = state["peers"].get(pub)
    if not p or not B.peer_active(p) or p.get("kind") in B.ROUTER_KINDS:
        return web.Response(status=404, text="Конфиг не найден или срок закончился.")
    filename, text = conf_text(p)
    return web.Response(body=text.encode(), content_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})


async def page(request):
    with open(os.path.join(HERE, "miniapp.html"), encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def start(bot_module):
    """Запускает веб-сервер приложения в том же цикле, что и бот. -> runner (для остановки)."""
    global B
    B = bot_module
    port = int(os.environ.get("MINIAPP_PORT", "8090"))
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/", page)
    app.router.add_post("/api/{action}", api)
    app.router.add_get("/conf/{pub}/{name}", conf_file)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("Mini App: http://127.0.0.1:%s", port)
    return runner
