#!/usr/bin/env python3
"""
BrainrotBro — бот + сервер игры в одном файле.

Что делает:
  1) Бот: на /start пишет приветствие и кнопку «Играть»; ставит синюю кнопку «Играть» снизу слева.
  2) API-сервер для мини-аппа: монеты, инвентарь, кейсы, апгрейдер, промокоды, дропы, админка, тех. работы.
     ВЕСЬ рандом и все проверки — здесь, на сервере (клиенту верить нельзя).

Установка:   pip install aiohttp
Токен:       положи НОВЫЙ токен бота в файл token.txt рядом со скриптом (или переменную BOT_TOKEN).
Запуск:      python bot.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl

from aiohttp import ClientSession, ClientTimeout, web

# ====================== НАСТРОЙКИ ======================
OWNER_ID = 8664148534                                   # владелец (всегда админ, играет во время тех. работ)
SITE_URL = os.getenv("SITE_URL", "https://brainrotbro.kitijol334.workers.dev")
CHANNEL = "@BrainrotBro67"                              # канал для подписки (бот должен быть АДМИНОМ канала)
DB_PATH = os.getenv("DB_PATH", "brainrot.db")
PORT = int(os.getenv("PORT", "8080"))
FREE_COOLDOWN_MS = 12 * 3600 * 1000                     # бесплатный кейс — раз в 12 часов
MIN_COIN_BET = 5                                        # минимум монет в ставке апгрейдера
EDGE = 8500                                             # шанс = ставка/цель * 100 * 0.85 (=8500/100) — то же считает index.html
AUTH_MAX_AGE = 3 * 24 * 3600                            # сколько живёт initData

WELCOME = (
    "Добро пожаловать на BrainrotBro! 🌊\n\n"
    "Нажми снизу слева на голубую кнопку «Играть» и начни апгрейдить своих брейнротов! Удачи! 🍀"
)

# ====================== КАТАЛОГ (должен совпадать с index.html) ======================
ITEMS = {  # id: стоимость
    "c2": 2, "noob": 5, "tric": 10, "s67": 20, "brr": 20, "l67": 45, "bal": 50, "n25": 50,
    "l25": 67, "sah": 85, "ale": 120, "vac": 150, "gar": 350, "f67": 367, "cf67": 1400,
}
COIN_ITEMS = {"c2": 2}                                  # это не брейнроты, а монеты (сразу на баланс)
BRAINROTS = sorted((k for k in ITEMS if k not in COIN_ITEMS), key=lambda k: ITEMS[k])

CASES = {
    "free": {"price": 0, "sub": True, "cd": True,
             "drops": [("c2", 85), ("noob", 5), ("tric", 3), ("brr", 2), ("bal", 1), ("sah", .5), ("ale", .15), ("vac", .08), ("gar", .001)]},
    "cheap": {"price": 5,
              "drops": [("c2", 50), ("noob", 25), ("tric", 5), ("s67", 1), ("bal", .2), ("gar", .1)]},
    "c67": {"price": 25,
            "drops": [("noob", 65), ("s67", 5), ("l67", 3), ("n25", 1), ("l25", .5), ("f67", .1), ("cf67", .01)]},
}
# Шансы в списках НЕ дают ровно 100% — random.choices сам нормализует веса (как и раньше).

rng = random.SystemRandom()                             # криптостойкий рандом
log = logging.getLogger("brainrot")


# ====================== ТОКЕН ======================
def load_token() -> str:
    t = os.getenv("BOT_TOKEN", "").strip()
    if not t:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
    if not t:
        raise SystemExit("Нет токена бота: создай файл token.txt рядом с bot.py и вставь туда токен от @BotFather.")
    return t


BOT_TOKEN = load_token()
SESSION: ClientSession = None  # создаётся в main()


# ====================== БАЗА ДАННЫХ ======================
db = sqlite3.connect(DB_PATH, isolation_level=None)     # autocommit, транзакции делаем сами
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, name TEXT,
  bal INTEGER NOT NULL DEFAULT 0, free_at INTEGER NOT NULL DEFAULT 0,
  is_admin INTEGER NOT NULL DEFAULT 0, seen INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_users_username ON users(username);
CREATE TABLE IF NOT EXISTS inv(uid INTEGER, item TEXT, n INTEGER NOT NULL, PRIMARY KEY(uid, item));
CREATE TABLE IF NOT EXISTS promos(k TEXT PRIMARY KEY, code TEXT, uses INTEGER, coins INTEGER, used INTEGER NOT NULL DEFAULT 0, owner INTEGER);
CREATE TABLE IF NOT EXISTS promo_uses(k TEXT, uid INTEGER, PRIMARY KEY(k, uid));
CREATE TABLE IF NOT EXISTS feed(id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, who TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
""")


@contextmanager
def tx():
    """Транзакция: либо всё применилось, либо ничего. Внутри НЕЛЬЗЯ делать await."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def now_ms() -> int:
    return int(time.time() * 1000)


def get_inv(uid):
    return {r["item"]: r["n"] for r in db.execute("SELECT item,n FROM inv WHERE uid=?", (uid,))}


def inv_add(uid, item, n=1):
    db.execute("INSERT INTO inv(uid,item,n) VALUES(?,?,?) ON CONFLICT(uid,item) DO UPDATE SET n=n+excluded.n", (uid, item, n))


def inv_take(uid, item, n=1) -> bool:
    r = db.execute("SELECT n FROM inv WHERE uid=? AND item=?", (uid, item)).fetchone()
    if not r or r["n"] < n:
        return False
    db.execute("UPDATE inv SET n=n-? WHERE uid=? AND item=?", (n, uid, item))
    db.execute("DELETE FROM inv WHERE uid=? AND item=? AND n<=0", (uid, item))
    return True


def get_bal(uid) -> int:
    return db.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()["bal"]


def get_setting(k, d="0"):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else d


def set_setting(k, v):
    db.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def maint_on() -> bool:
    return get_setting("maint") == "1"


def find_user(nick):
    nick = str(nick or "").strip().lstrip("@").lower()
    if not nick:
        return None
    return db.execute("SELECT * FROM users WHERE username=? ORDER BY seen DESC LIMIT 1", (nick,)).fetchone()


def feed_add(item, user):
    who = ("@" + user["username"]) if user["username"] else (user["name"] or "Игрок")[:16]
    db.execute("INSERT INTO feed(item,who,ts) VALUES(?,?,?)", (item, who, now_ms()))
    db.execute("DELETE FROM feed WHERE id < (SELECT MAX(id) FROM feed) - 500")


# ====================== АВТОРИЗАЦИЯ (initData от Telegram) ======================
def check_init(init_data: str):
    """Проверяем подпись Telegram. Вернёт dict пользователя или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > AUTH_MAX_AGE:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def touch_user(u) -> dict:
    uid = int(u["id"])
    username = (u.get("username") or "").lower() or None
    name = (u.get("first_name") or username or "Игрок")[:32]
    db.execute(
        "INSERT INTO users(id,username,name,seen) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,seen=excluded.seen",
        (uid, username, name, now_ms()))
    return dict(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


class ApiErr(Exception):
    def __init__(self, code, msg="", **extra):
        self.code, self.msg, self.extra = code, msg, extra


def api(fn):
    async def wrapper(request: web.Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        u = check_init(str(body.get("initData", "")))
        if not u:
            return web.json_response({"ok": False, "err": "auth", "msg": "Ошибка авторизации. Открой игру из Telegram заново."})
        user = touch_user(u)
        if maint_on() and user["id"] != OWNER_ID:
            return web.json_response({"ok": False, "err": "maint", "msg": "Технические работы"})
        try:
            res = await fn(user, body)
        except ApiErr as e:
            res = {"ok": False, "err": e.code, "msg": e.msg, **e.extra}
        except Exception:
            log.exception("Ошибка в %s", fn.__name__)
            res = {"ok": False, "err": "server", "msg": "Ошибка сервера, попробуй ещё раз"}
        res["now"] = now_ms()
        return web.json_response(res)
    return wrapper


# ====================== TELEGRAM BOT API ======================
async def tg_call(method: str, **params):
    try:
        async with SESSION.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params) as r:
            return await r.json()
    except Exception as e:  # не печатаем URL — в нём токен
        log.warning("Telegram %s: %s", method, type(e).__name__)
        return {"ok": False}


async def is_subscribed(uid: int) -> bool:
    r = await tg_call("getChatMember", chat_id=CHANNEL, user_id=uid)
    if not r.get("ok"):
        log.warning("getChatMember не сработал: %s (бот должен быть админом канала %s)", r.get("description"), CHANNEL)
        raise ApiErr("subcheck", "Не удалось проверить подписку. Попробуй чуть позже.")
    m = r["result"]
    return m["status"] in ("creator", "administrator", "member") or (m["status"] == "restricted" and m.get("is_member"))


# ====================== ИГРОВАЯ ЛОГИКА ======================
def roll(drops):
    ids = [d[0] for d in drops]
    return rng.choices(ids, weights=[d[1] for d in drops])[0]


def next_higher(item):
    for k in BRAINROTS:
        if ITEMS[k] > ITEMS[item]:
            return k
    return None


def chance(sv, tv) -> float:
    return int(sv / tv * EDGE + 0.5) / 100               # то же, что Math.round(sv/tv*8500)/100 в index.html


def snapshot(uid):
    return {"bal": get_bal(uid), "inv": get_inv(uid)}


@api
async def h_init(user, body):
    owner = user["id"] == OWNER_ID
    return {"ok": True, "me": {"id": user["id"], "is_admin": owner or bool(user["is_admin"]), "is_owner": owner},
            "freeAt": user["free_at"], **snapshot(user["id"])}


@api
async def h_open(user, body):
    uid = user["id"]
    c = CASES.get(body.get("case"))
    if not c:
        raise ApiErr("bad", "Кейс не найден")
    row = db.execute("SELECT bal,free_at FROM users WHERE id=?", (uid,)).fetchone()
    if c.get("cd") and row["free_at"] > now_ms():
        raise ApiErr("cooldown", "Бесплатный кейс можно открывать раз в 12 часов", freeAt=row["free_at"])
    if row["bal"] < c["price"]:
        raise ApiErr("funds", "Не хватает монет")
    if c.get("sub") and not await is_subscribed(uid):
        raise ApiErr("nosub", "Нужна подписка на канал")
    with tx():                                           # повторная проверка + выдача, без await внутри
        row = db.execute("SELECT bal,free_at FROM users WHERE id=?", (uid,)).fetchone()
        if c.get("cd") and row["free_at"] > now_ms():
            raise ApiErr("cooldown", "Бесплатный кейс можно открывать раз в 12 часов", freeAt=row["free_at"])
        if row["bal"] < c["price"]:
            raise ApiErr("funds", "Не хватает монет")
        drop = roll(c["drops"])
        bal = row["bal"] - c["price"]
        if drop in COIN_ITEMS:
            bal += COIN_ITEMS[drop]
        else:
            inv_add(uid, drop)
            feed_add(drop, user)
        free_at = now_ms() + FREE_COOLDOWN_MS if c.get("cd") else row["free_at"]
        db.execute("UPDATE users SET bal=?,free_at=? WHERE id=?", (bal, free_at, uid))
    return {"ok": True, "drop": drop, "freeAt": free_at, **snapshot(uid)}


@api
async def h_upgrade(user, body):
    uid = user["id"]
    st = body.get("stake") or {}
    tid = body.get("target")
    if tid not in ITEMS or tid in COIN_ITEMS:
        raise ApiErr("bad", "Неверная цель")
    tv = ITEMS[tid]
    sid, n = None, 0
    if st.get("k") == "i":
        sid = st.get("id")
        if sid not in ITEMS or sid in COIN_ITEMS:
            raise ApiErr("bad", "Неверная ставка")
        sv = ITEMS[sid]
    elif st.get("k") == "c":
        try:
            n = int(st.get("n"))
        except (TypeError, ValueError):
            raise ApiErr("bad", "Неверная ставка")
        if n < MIN_COIN_BET:
            raise ApiErr("bad", f"Минимальная ставка — {MIN_COIN_BET} монет")
        sv = n
    else:
        raise ApiErr("bad", "Неверная ставка")
    if sv >= tv:
        raise ApiErr("bad", "Цель должна быть дороже ставки")
    ch = chance(sv, tv)
    if not (0.99 < ch < 61):
        raise ApiErr("bad", "Шанс должен быть от 1% до 60%")

    with tx():
        if sid:
            have = db.execute("SELECT n FROM inv WHERE uid=? AND item=?", (uid, sid)).fetchone()
            if not have or have["n"] < 1:
                raise ApiErr("bad", "У тебя нет этого брейнрота")
        elif get_bal(uid) < n:
            raise ApiErr("funds", "Не хватает монет")

        spins = []
        gold = ch > 9 and rng.random() < 0.15            # золотой куш появился (не бывает при шансе ≤ 9%)
        hit = gold and rng.random() < 0.15               # стрелка остановилась на нём
        if not hit:
            won = rng.random() < ch / 100
            spins.append({"ch": ch, "gold": gold, "hit": False, "won": won})
            result, final = ("win", tid) if won else ("lose", None)
        else:
            spins.append({"ch": ch, "gold": True, "hit": True, "won": False})
            cur, tgt = None, tid                         # цепочка: каждый раз 50%, ловить куш уже нельзя
            while tgt:
                w = rng.random() < 0.5
                spins.append({"ch": 50, "gold": True, "hit": False, "won": w})
                if not w:
                    break
                cur, tgt = tgt, next_higher(tgt)
            result, final = ("win", cur) if cur else ("saved", None)

        if result != "saved":                            # ставка тратится (при «saved» ничего не теряем)
            if sid:
                inv_take(uid, sid)
            else:
                db.execute("UPDATE users SET bal=bal-? WHERE id=?", (n, uid))
        if result == "win":
            inv_add(uid, final)
            feed_add(final, user)
    return {"ok": True, "spins": spins, "result": result, "item": final, **snapshot(uid)}


@api
async def h_sell(user, body):
    uid, iid = user["id"], body.get("id")
    try:
        n = int(body.get("n", 1))
    except (TypeError, ValueError):
        n = 0
    if iid not in ITEMS or iid in COIN_ITEMS or n < 1:
        raise ApiErr("bad", "Нельзя продать")
    with tx():
        if not inv_take(uid, iid, n):
            raise ApiErr("bad", "У тебя нет столько")
        db.execute("UPDATE users SET bal=bal+? WHERE id=?", (ITEMS[iid] * n, uid))
    return {"ok": True, **snapshot(uid)}


@api
async def h_promo(user, body):
    uid = user["id"]
    k = str(body.get("code", "")).strip().lower()[:64]
    with tx():
        p = db.execute("SELECT * FROM promos WHERE k=?", (k,)).fetchone() if k else None
        if not p:
            raise ApiErr("promo_invalid", "Промокод недействителен")
        if db.execute("SELECT 1 FROM promo_uses WHERE k=? AND uid=?", (k, uid)).fetchone():
            raise ApiErr("promo_used", "Ты уже активировал этот промокод")
        if p["used"] >= p["uses"]:
            raise ApiErr("promo_expired", "Промокод закончился")
        db.execute("INSERT INTO promo_uses(k,uid) VALUES(?,?)", (k, uid))
        db.execute("UPDATE promos SET used=used+1 WHERE k=?", (k,))
        db.execute("UPDATE users SET bal=bal+? WHERE id=?", (p["coins"], uid))
    return {"ok": True, "coins": p["coins"], "bal": get_bal(uid)}


@api
async def h_feed(user, body):
    rows = db.execute("SELECT id,item,who FROM feed ORDER BY id DESC LIMIT 30").fetchall()
    return {"ok": True, "drops": [{"id": r["id"], "item": r["item"], "user": r["who"]} for r in rows]}


@api
async def h_admin(user, body):
    uid = user["id"]
    owner = uid == OWNER_ID
    if not (owner or user["is_admin"]):
        raise ApiErr("forbidden", "Нет доступа")
    act = body.get("act")
    if act in ("admin_add", "admin_list", "admin_del", "maint") and not owner:
        raise ApiErr("forbidden", "Это может только владелец")

    def target():
        t = find_user(body.get("nick"))
        if not t:
            raise ApiErr("nouser", "Игрок не найден — он должен хотя бы раз открыть игру")
        return t

    if act == "grant":
        try:
            amount = int(body.get("amount"))
        except (TypeError, ValueError):
            amount = 0
        if not 1 <= amount <= 10**9:
            raise ApiErr("bad", "Укажи количество монет (от 1 до 1 000 000 000)")
        t = target()
        db.execute("UPDATE users SET bal=bal+? WHERE id=?", (amount, t["id"]))
        return {"ok": True, "self": t["id"] == uid, "bal": get_bal(t["id"])}

    if act == "wipe":
        t = target()
        if t["id"] == OWNER_ID and not owner:
            raise ApiErr("forbidden", "Владельца очищать нельзя")
        with tx():
            db.execute("DELETE FROM inv WHERE uid=?", (t["id"],))
            db.execute("UPDATE users SET bal=0 WHERE id=?", (t["id"],))
        return {"ok": True, "self": t["id"] == uid}

    if act == "promo_add":
        code = str(body.get("code", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{3,32}", code):
            raise ApiErr("bad", "Промокод: 3–32 символа, латиница, цифры, _ и -")
        try:
            uses, coins = int(body.get("uses")), int(body.get("coins"))
        except (TypeError, ValueError):
            raise ApiErr("bad", "Укажи активации и монеты числами")
        if not (1 <= uses <= 10**6 and 1 <= coins <= 10**9):
            raise ApiErr("bad", "Активации: 1–1 000 000, монеты: 1–1 000 000 000")
        if db.execute("SELECT 1 FROM promos WHERE k=?", (code.lower(),)).fetchone():
            raise ApiErr("bad", "Такой промокод уже есть")
        db.execute("INSERT INTO promos(k,code,uses,coins,owner) VALUES(?,?,?,?,?)", (code.lower(), code, uses, coins, uid))
        return {"ok": True}

    if act == "promo_list":
        q = "SELECT code,uses,used,coins FROM promos" + ("" if owner else " WHERE owner=?") + " ORDER BY rowid DESC"
        rows = db.execute(q, () if owner else (uid,)).fetchall()
        return {"ok": True, "promos": [dict(r) for r in rows]}

    if act == "admin_add":
        t = target()
        db.execute("UPDATE users SET is_admin=1 WHERE id=?", (t["id"],))
        return {"ok": True}

    if act == "admin_list":
        rows = db.execute("SELECT id,username,name FROM users WHERE is_admin=1 AND id!=?", (OWNER_ID,)).fetchall()
        return {"ok": True, "admins": [dict(r) for r in rows]}

    if act == "admin_del":
        try:
            aid = int(body.get("id"))
        except (TypeError, ValueError):
            raise ApiErr("bad", "Неверный id")
        db.execute("UPDATE users SET is_admin=0 WHERE id=?", (aid,))
        return {"ok": True}

    if act == "maint":
        if "on" in body:
            set_setting("maint", "1" if body["on"] else "0")
        return {"ok": True, "on": maint_on()}

    raise ApiErr("bad", "Неизвестное действие")


# ====================== HTTP-СЕРВЕР ======================
@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def health(request):
    return web.Response(text="BrainrotBro server is running")


# ====================== БОТ ======================
async def send_welcome(chat_id):
    await tg_call("sendMessage", chat_id=chat_id, text=WELCOME,
                  reply_markup={"inline_keyboard": [[{"text": "🎮 Играть", "web_app": {"url": SITE_URL}}]]})


async def bot_loop():
    offset = 0
    while True:
        try:
            r = await tg_call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            if not r.get("ok"):
                await asyncio.sleep(3)
                continue
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                m = upd.get("message") or {}
                if (m.get("text") or "").startswith("/start") and m.get("chat", {}).get("type") == "private":
                    await send_welcome(m["chat"]["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка бота")
            await asyncio.sleep(3)


async def main():
    global SESSION
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SESSION = ClientSession(timeout=ClientTimeout(total=70))
    app = web.Application(middlewares=[cors])
    app.router.add_get("/", health)
    for path, fn in [("init", h_init), ("open", h_open), ("upgrade", h_upgrade), ("sell", h_sell),
                     ("promo", h_promo), ("feed", h_feed), ("admin", h_admin)]:
        app.router.add_post(f"/api/{path}", fn)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await tg_call("deleteWebhook")
    await tg_call("setChatMenuButton", menu_button={"type": "web_app", "text": "Играть", "web_app": {"url": SITE_URL}})
    log.info("Сервер запущен на порту %s, бот слушает сообщения. Сайт: %s", PORT, SITE_URL)
    try:
        await bot_loop()
    finally:
        await SESSION.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass