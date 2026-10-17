"""Telegram-бот и сервер мессенджера в одном процессе.

Запуск (messenger.html должен лежать рядом с bot.py):
    pip install aiogram
    BOT_TOKEN=... python bot.py   (на Render больше ничего задавать не нужно)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, Message, PreCheckoutQuery, WebAppInfo,
)
from aiohttp import web

BOT_TOKEN = os.environ["BOT_TOKEN"]
PAGE_URL = os.getenv("PAGE_URL", "https://golsog99k-ux.github.io/messager/messenger.html")
_render_url = os.getenv("RENDER_EXTERNAL_URL")  # на Render задаётся автоматически: адрес этого сервера
MESSENGER_URL = f"{PAGE_URL}?api={_render_url}" if _render_url else os.getenv("MESSENGER_URL", PAGE_URL)
PORT = int(os.getenv("PORT", "8080"))
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://golsog99k-ux.github.io")  # откуда страница может обращаться к серверу
WELCOME_STARS = int(os.getenv("WELCOME_STARS", "5"))   # бонус новому пользователю
DB_PATH = os.getenv("DB_PATH", "bot.db")
PACKS = (50, 100, 250)  # цена пакета в Telegram Stars; столько же звёзд начисляется на баланс

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  tg_id INTEGER PRIMARY KEY, username TEXT, name TEXT, created_at TEXT NOT NULL,
  stars INTEGER NOT NULL DEFAULT 0, purchased INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payments(
  charge_id TEXT PRIMARY KEY, tg_id INTEGER NOT NULL,
  amount INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sender INTEGER NOT NULL,
  recipient INTEGER NOT NULL, text TEXT NOT NULL, ts INTEGER NOT NULL);
""")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_user(tg_id):
    return db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def ensure_user(tg_id, username, name):
    """Возвращает (запись, True если пользователь только что создан)."""
    row = get_user(tg_id)
    if row:
        return row, False
    with db:
        db.execute(
            "INSERT INTO users(tg_id, username, name, created_at, stars) VALUES(?,?,?,?,?)",
            (tg_id, username, name, now(), WELCOME_STARS),
        )
    return get_user(tg_id), True


def credit_purchase(tg_id, charge_id, amount):
    """Начисляет звёзды за покупку. False, если этот платёж уже был учтён."""
    with db:
        cur = db.execute(
            "INSERT OR IGNORE INTO payments VALUES(?,?,?,?)",
            (charge_id, tg_id, amount, now()),
        )
        if cur.rowcount == 0:
            return False
        db.execute(
            "UPDATE users SET stars=stars+?, purchased=1 WHERE tg_id=?",
            (amount, tg_id),
        )
    return True


def status_text(row):
    return f"⭐ Звёзды: {row['stars']}\n🛒 Покупка: {'есть' if row['purchased'] else 'нет'}"


def keyboard():
    rows = [[InlineKeyboardButton(text="Открыть мессенджер", web_app=WebAppInfo(url=MESSENGER_URL))]]
    rows.append([InlineKeyboardButton(text=f"Купить {n} ⭐", callback_data=f"buy:{n}") for n in PACKS])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_args(amount):
    return dict(
        title=f"{amount} звёзд",
        description=f"Пополнение баланса на {amount} ⭐",
        payload=f"buy:{amount}",
        currency="XTR",  # Telegram Stars: provider_token не нужен
        prices=[LabeledPrice(label=f"{amount} ⭐", amount=amount)],
    )


# ---------- Telegram-бот ----------
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    u = m.from_user
    row, is_new = ensure_user(u.id, u.username, u.first_name)
    if is_new:
        head = "Добро пожаловать! Вы здесь впервые, аккаунт создан."
        if WELCOME_STARS:
            head += f" Начислено {WELCOME_STARS} ⭐ в подарок."
    else:
        head = "С возвращением!"
    await m.answer(f"{head}\n\n{status_text(row)}", reply_markup=keyboard())


@dp.message(Command("status"))
async def status(m: Message):
    row = get_user(m.from_user.id)
    if not row:
        return await m.answer("Вас ещё нет в базе. Нажмите /start.")
    await m.answer(status_text(row), reply_markup=keyboard())


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    raw = c.data.partition(":")[2]
    amount = int(raw) if raw.isdigit() else 0
    if amount not in PACKS:
        return await c.answer("Пакет недоступен", show_alert=True)
    await c.message.answer_invoice(**invoice_args(amount))
    await c.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = q.invoice_payload.startswith("buy:") and q.total_amount in PACKS
    await q.answer(ok=ok, error_message=None if ok else "Неверный платёж")


@dp.message(F.successful_payment)
async def paid(m: Message):
    p = m.successful_payment
    u = m.from_user
    ensure_user(u.id, u.username, u.first_name)
    if credit_purchase(u.id, p.telegram_payment_charge_id, p.total_amount):
        row = get_user(u.id)
        await m.answer(
            f"Оплата прошла, начислено {p.total_amount} ⭐.\n\n{status_text(row)}",
            reply_markup=keyboard(),
        )


# ---------- Сервер мессенджера ----------
def check_init_data(init_data):
    """Проверяет подпись Telegram. Возвращает данные пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, strict_parsing=True))
        got = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, got) or time.time() - int(data["auth_date"]) > 86400:
            return None
        return json.loads(data["user"])
    except (KeyError, ValueError):
        return None


def auth(request):
    """Вход через Telegram: проверяет подпись, создаёт пользователя, если его нет."""
    u = check_init_data(request.headers.get("X-Init-Data", ""))
    if not u:
        raise web.HTTPUnauthorized(text="Откройте мессенджер через бота в Telegram")
    ensure_user(u["id"], u.get("username"), u.get("first_name"))
    return u["id"]


async def body_json(request):
    try:
        return await request.json()
    except ValueError:
        raise web.HTTPBadRequest()


async def index(request):
    return web.FileResponse(Path(__file__).with_name("messenger.html"))


async def api_state(request):
    me = auth(request)
    a = request.query.get("after", "0")
    after = int(a) if a.isdigit() else 0
    row = get_user(me)
    users = db.execute(
        "SELECT tg_id AS id, COALESCE(name, username, 'Без имени') AS name FROM users WHERE tg_id != ?",
        (me,),
    ).fetchall()
    msgs = db.execute(
        "SELECT id, sender, recipient, text, ts FROM messages "
        "WHERE id > ? AND (sender = ? OR recipient = ?) ORDER BY id",
        (after, me, me),
    ).fetchall()
    return web.json_response({
        "me": {"id": me, "stars": row["stars"], "purchased": bool(row["purchased"])},
        "users": [dict(r) for r in users],
        "messages": [dict(r) for r in msgs],
    })


async def api_send(request):
    me = auth(request)
    body = await body_json(request)
    try:
        to, text = int(body["to"]), str(body["text"]).strip()[:2000]
    except (KeyError, ValueError, TypeError):
        raise web.HTTPBadRequest()
    if not text or to == me or not get_user(to):
        raise web.HTTPBadRequest()
    with db:
        db.execute(
            "INSERT INTO messages(sender, recipient, text, ts) VALUES(?,?,?,?)",
            (me, to, text, int(time.time() * 1000)),
        )
    return web.json_response({"ok": True})


async def api_invoice(request):
    auth(request)
    body = await body_json(request)
    try:
        amount = int(body["amount"])
    except (KeyError, ValueError, TypeError):
        raise web.HTTPBadRequest()
    if amount not in PACKS:
        raise web.HTTPBadRequest()
    link = await request.app["bot"].create_invoice_link(**invoice_args(amount))
    return web.json_response({"link": link})


@web.middleware
async def cors(request, handler):
    """Разрешает странице с ALLOWED_ORIGIN (например, GitHub Pages) обращаться к API."""
    try:
        resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "X-Init-Data, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    app = web.Application(middlewares=[cors])
    app["bot"] = bot
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", api_state),
        web.post("/api/send", api_send),
        web.post("/api/invoice", api_invoice),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
