'''Хи-Хи: Telegram Mini App gateway and HTTPS API for BotHost.

Environment:
 BOT_TOKEN: existing Telegram bot token (secret)
 OWNER_ID: first recipient Telegram user ID (default configured below)
 MINI_APP_URL: https://greengergreen.github.io/hihi-miniapp/
 PORT: Bothost HTTPS proxy internal port (default 3000)
 DB_PATH: optional persistent path for SQLite (for orders and recipient management)
'''

import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '').strip()
OWNER_ID = int(os.getenv('OWNER_ID', '142840890'))
MINI_APP_URL = os.getenv('MINI_APP_URL', 'https://greengergreen.github.io/hihi-miniapp/').strip()
PORT = int(os.getenv('PORT', '3000'))
DB_PATH = os.getenv('DB_PATH', 'hihi_orders.sqlite3')
ORIGIN = 'https://greengergreen.github.io'
logger = logging.getLogger('hihi')
router = Router()
bot: Bot | None = None
poll_task: asyncio.Task | None = None
rate_lock = threading.Lock()
rate_windows = {}

WELCOME = (
    '🎉 <b>Добро пожаловать в «Хи-Хи»!</b>\n\n'
    'Здесь начинается ваш идеальный праздник! 🥳\n\n'
    'Соберите его прямо здесь: выберите меню, развлечения, оформление '
    'и всё, что сделает этот день незабываемым.\n\n'
    '✨ Укажите количество детей, добавьте понравившиеся услуги '
    'и получите готовую смету.\n\n'
    '👇 <b>Нажмите кнопку ниже, чтобы собрать свой праздник!</b>'
)


def app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='🎉 Собрать свой праздник', web_app=WebAppInfo(url=MINI_APP_URL))
    ]])


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=app_keyboard(), parse_mode='HTML')


def open_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def setup_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            date TEXT,
            kids INTEGER,
            summary TEXT,
            declared_total INTEGER,
            delivered INTEGER NOT NULL DEFAULT 0
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )''')


def recipient_id() -> int:
    with open_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='telegram_recipient'").fetchone()
    return int(row['value']) if row else OWNER_ID


class LineItem(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str = Field(min_length=1, max_length=170)
    kind: str = Field(default="", max_length=20)
    id: str = Field(default="", max_length=200)
    qty: int = Field(default=1, ge=1, le=1000)
    unit: str = Field(default='шт.', max_length=35)
    price: float = Field(default=0, ge=0, le=10_000_000)
    blocks: int = Field(default=1, ge=1, le=1000)


class Application(BaseModel):
    model_config = ConfigDict(extra='ignore')
    type: str = Field(pattern='^(order|callback|date)$')
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=7, max_length=40)
    date: str = Field(default='', max_length=150)
    kids: int = Field(default=0, ge=0, le=100)
    items: list[LineItem] = Field(default_factory=list, max_length=120)
    total: float = Field(default=0, ge=0, le=50_000_000)


def format_money(value: float) -> str:
    """Whole-ruble amounts remain easy to read in Telegram."""
    return f'{value:,.0f}'.replace(',', ' ') if float(value).is_integer() else f'{value:,.2f}'.replace(',', ' ').replace('.', ',')


def item_section(item: LineItem) -> str:
    """New frontend sends kind; old carts can be recognized from the item ID."""
    kind = (item.kind or item.id.split('|', 1)[0]).lower()
    return kind if kind in {'child', 'adult', 'fun', 'decor'} else 'other'


def item_display(item: LineItem) -> tuple[str, str]:
    """Group pizza by diameter; juices/water by volume, without losing varieties."""
    name = item.name.strip()
    pizza = re.fullmatch(r'Пицца\s+[«"](.+?)[»"]\s*,\s*(\d+)\s*см', name, re.I)
    if pizza:
        return f'Пицца {pizza.group(2)} см', pizza.group(1).strip()
    drink = re.fullmatch(r'(Сок|Вода)\s+(.+?)\s*,\s*([\d,.]+)\s*л', name, re.I)
    if drink:
        return f'{drink.group(1).capitalize()} {drink.group(3)} л', drink.group(2).strip()
    return name, ''


def item_amount(item: LineItem) -> float:
    return item.qty * item.price * item.blocks


def format_section_lines(items: list[LineItem]) -> list[str]:
    # Python dictionaries preserve the order in which the customer selected products.
    groups = {}
    for item in items:
        label, flavor = item_display(item)
        key = (label.casefold(), item.unit.casefold(), item.blocks)
        if key not in groups:
            groups[key] = {'label': label, 'unit': item.unit.strip() or 'шт.',
                           'qty': 0, 'sum': 0.0, 'blocks': item.blocks, 'flavors': {}}
        group = groups[key]
        group['qty'] += item.qty
        group['sum'] += item_amount(item)
        if flavor:
            group['flavors'][flavor] = group['flavors'].get(flavor, 0) + item.qty
    lines = []
    for g in groups.values():
        unit = g['unit']
        if g['label'].startswith('Пицца '):
            unit = 'шт.'
        elif g['label'].startswith(('Сок ', 'Вода ')):
            unit = 'шт.'
        quantity = f"{g['qty']} {unit}"
        if g['blocks'] > 1:
            quantity += f" × {g['blocks']}"  # For multi-block services/room use
        flavors = g['flavors']
        variation = ''
        if flavors:
            varying = any(q > 1 for q in flavors.values())
            variation = ' (' + ', '.join(f'{f} ×{q}' if varying else f for f,q in flavors.items()) + ')'
        lines.append(f"• {escape(g['label'])} — {escape(quantity)}{escape(variation)} — {format_money(g['sum'])} ₽")
    return lines


def format_application(a: Application) -> str:
    title = {'order': '🧾 Новая заявка «Хи-Хи»',
             'callback': '📞 Заказ обратного звонка',
             'date': '📅 Запрос даты праздника'}[a.type]
    parts = [f'<b>{title}</b>', f'👤 Имя: {escape(a.name.strip())}',
             f'📱 Телефон: {escape(a.phone.strip())}']
    if a.type == 'date':
        parts.append(f'📆 Дата: {escape(a.date)}')
    if a.type == 'order':
        labels = [('child', '🍕 Детский стол'),
                  ('adult', '☕ Взрослый стол'),
                  ('fun', '🎉 Развлечения'),
                  ('decor', '🎈 Оформление'),
                  ('other', '📦 Другие позиции')]
        sections = {key: [] for key, _ in labels}
        for item in a.items:
            sections[item_section(item)].append(item)
        for key, label in labels:
            selected = sections[key]
            if not selected:
                continue
            heading = label + (f': {a.kids} чел.' if key == 'child' and a.kids else ':')
            parts.extend(['', f'<b>{heading}</b>'])
            parts.extend(format_section_lines(selected))
            parts.append(f"<i>Подытог: {format_money(sum(item_amount(i) for i in selected))} ₽</i>")
        parts.extend(['', f'💰 <b>Итого по смете: {format_money(a.total)} ₽</b>',
                      '<i>Сумма и цены переданы сайтом, перед подтверждением перепроверьте заказ.</i>'])
    return '\n'.join(parts)


def within_rate_limit(ip: str):
    now = time.monotonic()
    with rate_lock:
        vals = [t for t in rate_windows.get(ip, []) if now - t < 60]
        if len(vals) >= 5:
            raise HTTPException(429, 'Слишком много запросов. Повторите через минуту.')
        vals.append(now)
        rate_windows[ip] = vals
        if len(rate_windows)>3000:
            for key in list(rate_windows):
                if not any(now-v < 60 for v in rate_windows[key]):
                    del rate_windows[key]


async def start_bot():
    global bot
    if not TOKEN:
        raise RuntimeError('Секрет BOT_TOKEN отсутствует в переменных окружения Bothost')
    if not MINI_APP_URL.startswith('https://'):
        raise RuntimeError('MINI_APP_URL должен начинаться с https://')
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command='start', description='🎉 Собрать праздник')])
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
        text='🎉 Собрать праздник', web_app=WebAppInfo(url=MINI_APP_URL)
    ))
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global poll_task
    setup_db()
    poll_task = asyncio.create_task(start_bot(), name='telegram-polling')
    try:
        yield
    finally:
        if poll_task:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        if bot:
            await bot.session.close()


app = FastAPI(title='Хи-Хи API', docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_methods=['POST','GET'], allow_headers=['Content-Type'])


@app.get('/health')
async def health():
    return {'ok': True, 'service': 'Хи-Хи', 'bot_started': bool(bot and poll_task and not poll_task.done())}


@app.post('/submit')
async def submit(data: Application, request: Request):
    if not TOKEN or bot is None or not poll_task or poll_task.done():
        raise HTTPException(503, 'Telegram временно недоступен')
    # CORS controls web browsers, but public HTTP clients can still POST.
    # This endpoint only sends notifications; it grants no admin access.
    ip = request.client.host if request.client else 'unknown'
    within_rate_limit(ip)
    if len([x for x in data.phone if x.isdigit()]) < 7:
        raise HTTPException(422, 'Некорректный телефон')
    if data.type=='order' and not data.items:
        raise HTTPException(422, 'Смета пуста')
    if data.type=='date' and not data.date.strip():
        raise HTTPException(422, 'Не указана дата')
    message = format_application(data)
    if len(message)>3950:
        raise HTTPException(413, 'Слишком длинная заявка')
    with open_db() as conn:
        cur=conn.execute('''INSERT INTO orders(created_at,type,name,phone,date,kids,summary,declared_total)
            VALUES(?,?,?,?,?,?,?,?)''',(
            datetime.now(timezone.utc).isoformat(), data.type, data.name, data.phone,
            data.date, data.kids, message, round(data.total)
        ))
        order_id=cur.lastrowid
    try:
        await bot.send_message(recipient_id(), f'#{order_id}\n'+message,parse_mode='HTML')
    except Exception:
        logger.exception('Delivery failed for order %s',order_id)
        raise HTTPException(502, 'Заявка сохранена, но уведомление пока не доставлено')
    with open_db() as conn:
        conn.execute('UPDATE orders SET delivered=1 WHERE id=?',(order_id,))
    return {'ok': True, 'id': order_id}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host='0.0.0.0', port=PORT, log_level='info', workers=1)
