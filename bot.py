"""Хи-Хи: Telegram-конструктор праздников. aiogram 3.x, SQLite.

Existing tables are retained; v2 tables are additive. Configure DB_PATH on persistent storage.
"""
import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from html import escape
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from dotenv import load_dotenv
from catalog import CATEGORIES, ITEMS, PROGRAMS

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '')
OWNER = int(os.getenv('OWNER_ID', '142840890'))
DB_PATH = os.getenv('DB_PATH', 'hihi_v3.sqlite3')
router = Router()
SECTIONS = [('child', '🍟 Детский стол'), ('adult', '☕ Стол родителей'),
            ('entertainment', '🎭 Развлечения'), ('decor', '🎈 Оформление')]
GROUPS = [('pizza', '🍕 Пицца'), ('snacks', '🍟 Закуски'), ('drinks', '🥤 Напитки')]

class Form(StatesGroup):
    children = State()
    quantity = State()
    name = State()
    phone = State()
    admin_edit = State()
    admin_add = State()
    admin_target = State()
    drink_quantity = State()


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS categories(id TEXT PRIMARY KEY,title TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT,category TEXT NOT NULL,name TEXT NOT NULL,price INTEGER NOT NULL CHECK(price>=0),description TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS carts(user_id INTEGER NOT NULL,item_id INTEGER NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),PRIMARY KEY(user_id,item_id));
        CREATE TABLE IF NOT EXISTS drafts(user_id INTEGER PRIMARY KEY,age TEXT DEFAULT '',date TEXT DEFAULT '',name TEXT DEFAULT '',contact TEXT DEFAULT '',program TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,created TEXT NOT NULL,summary TEXT NOT NULL,total INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'new');
        CREATE TABLE IF NOT EXISTS party(user_id INTEGER PRIMARY KEY,children INTEGER NOT NULL DEFAULT 0,name TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',theme TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS cart_v2(user_id INTEGER NOT NULL,section TEXT NOT NULL,item_id INTEGER NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),units INTEGER NOT NULL DEFAULT 1 CHECK(units>0),PRIMARY KEY(user_id,section,item_id));
        CREATE TABLE IF NOT EXISTS item_rules(item_id INTEGER PRIMARY KEY,rule TEXT NOT NULL DEFAULT 'quantity',unit TEXT NOT NULL DEFAULT 'шт.',per_child INTEGER NOT NULL DEFAULT 0);
        ''')
        if not c.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
            c.executemany('INSERT INTO categories(id,title) VALUES(?,?)', CATEGORIES)
            c.executemany('INSERT INTO items(category,name,price,description) VALUES(?,?,?,?)', ITEMS)
        c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', ('recipient',str(OWNER)))
        for r in c.execute('SELECT id,category,name,description FROM items').fetchall():
            rule,unit,per = infer_rule(r)
            c.execute('INSERT OR IGNORE INTO item_rules(item_id,rule,unit,per_child) VALUES(?,?,?,?)',(r['id'],rule,unit,per))


def infer_rule(item):
    n=item['name'].lower(); cat=item['category']; desc=(item['description'] or '').lower()
    if n=='игровая комната': return ('room','реб.',1)
    if n in ('анимационная программа','квест / челлендж-пати'): return ('hours','час',0)
    if cat=='entertainment':
        if '1 человек' in desc: return ('quantity','чел.',1)
        if '1 шт.' in desc: return ('quantity','шт.',0)
        return ('single','усл.',0)
    if cat=='decor': return ('quantity','шт.',0)
    if n.startswith('наггетсы'): return ('quantity','порц.',1)
    if n.startswith('картофель'): return ('quantity','порц.',1)
    if cat=='pizza': return ('quantity','шт.',0)
    if cat=='snacks': return ('quantity','порц.',n in ('блинчики с топпингом','канапе'))
    if cat=='drinks' and (n.startswith('сок ') or n.startswith('вода ')) and ('0,2 л' in n or '0,5 л' in n): return ('quantity','шт.',1)
    return ('quantity','шт.',0)


def setting(key):
    with db() as c:
        r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return r['value'] if r else ''


def owner(uid): return uid == OWNER

def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=d) for t,d in row] for row in rows])

async def show(q,text,buttons):
    try:
        await q.message.edit_text(text,reply_markup=keyboard(buttons),parse_mode='HTML')
    except Exception as exc:
        if 'message is not modified' not in str(exc).lower():
            await q.message.answer(text,reply_markup=keyboard(buttons),parse_mode='HTML')
    await q.answer()


def party(uid):
    with db() as c: return c.execute('SELECT * FROM party WHERE user_id=?',(uid,)).fetchone()

def set_party(uid,field,value):
    if field not in {'children','name','phone','theme'}: raise ValueError(field)
    with db() as c:
        c.execute('INSERT OR IGNORE INTO party(user_id) VALUES(?)',(uid,))
        c.execute(f'UPDATE party SET {field}=? WHERE user_id=?',(value,uid))

def nav(uid,back='home'):
    rows=[[('🧾 Моя смета','cart')],[('🏠 Главное меню',back)]]
    return rows

def home(uid):
    rows=[[('🎉 Собрать праздник','begin')],[('🧾 Моя смета','cart')]]
    if owner(uid):rows.append([('⚙️ Управление','admin')])
    return rows

def get_item(iid):
    with db() as c:
        return c.execute('''SELECT i.*,r.rule,r.unit,r.per_child FROM items i
                            LEFT JOIN item_rules r ON i.id=r.item_id WHERE i.id=?''',(iid,)).fetchone()

def items_for(section,group=None,page=0):
    if section in ('child','adult'):
        if group not in {g for g,_ in GROUPS}:return [],0
        where="i.category=? AND i.active=1";args=[group]
        if group=='pizza':
            where += " AND i.name LIKE '%25 см'"  # one entry per flavor, menu uses size-first
        if group=='snacks':
            where += " AND i.name NOT LIKE 'Наггетсы %'"
        if group=='drinks':
            where += " AND i.name NOT LIKE 'Сок %' AND i.name NOT LIKE 'Вода %'"
            if section=='child':
                where += " AND i.name IN ('Чай 180 мл','Детское шампанское')"
            else:
                where += " AND i.name NOT IN ('Американо','Капучино','Глясе','Латте','Эспрессо')"
    else:
        where='i.category=? AND i.active=1';args=[section]
    with db() as c:
        count=c.execute(f'SELECT COUNT(*) n FROM items i WHERE {where}',args).fetchone()['n']
        rows=c.execute(f'SELECT i.* FROM items i WHERE {where} ORDER BY i.id LIMIT 7 OFFSET ?',(*args,page*7)).fetchall()
    return rows,count

def listing(section,group,page=0):
    rows,count=items_for(section,group,page)
    buttons=[[(f'{r["name"][:37]} — {r["price"]} ₽',f'item:{section}:{r["id"]}')] for r in rows]
    navrow=[]
    if page>0:navrow.append(('◀️',f'list:{section}:{group}:{page-1}'))
    if (page+1)*7<count:navrow.append(('▶️',f'list:{section}:{group}:{page+1}'))
    if navrow:buttons.append(navrow)
    buttons.extend([[('🧾 Моя смета','cart')],[('⬅️ Назад',f'section:{section}')]])
    return buttons

def add_line(uid,section,iid,qty,units=1):
    if qty<1 or units<1 or qty>10000 or units>10000:raise ValueError('quantity')
    with db() as c:
        existing=c.execute('SELECT quantity,units FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(uid,section,iid)).fetchone()
        if existing:
            # One service per party: a repeated choice extends it, never duplicates it.
            r=get_item(iid)
            if r['rule']=='single':return False
            if r['rule'] in ('hours','room'):
                c.execute('UPDATE cart_v2 SET units=units+? WHERE user_id=? AND section=? AND item_id=?',(units,uid,section,iid))
            else:
                c.execute('UPDATE cart_v2 SET quantity=quantity+? WHERE user_id=? AND section=? AND item_id=?',(qty,uid,section,iid))
        else:
            c.execute('INSERT INTO cart_v2(user_id,section,item_id,quantity,units) VALUES(?,?,?,?,?)',(uid,section,iid,qty,units))
    return True

def cart_rows(uid):
    with db() as c:
        return c.execute('''SELECT v.section,v.item_id,v.quantity,v.units,i.name,i.price,i.description,
                              COALESCE(r.rule,'quantity') rule,COALESCE(r.unit,'шт.') unit
                              FROM cart_v2 v JOIN items i ON i.id=v.item_id
                              LEFT JOIN item_rules r ON r.item_id=i.id
                              WHERE v.user_id=? ORDER BY CASE v.section WHEN 'child' THEN 1 WHEN 'adult' THEN 2 WHEN 'entertainment' THEN 3 ELSE 4 END,i.id''',(uid,)).fetchall()

def line_sum(r): return r['price']*r['quantity']*r['units']

def cart_text(uid,admin=False):
    rows=cart_rows(uid); p=party(uid)
    lines=['🧾 <b>'+('Заявка «Хи-Хи»' if admin else 'Моя смета')+'</b>']
    if p:
        if p['children']:lines.append(f'👧 Детей: {p["children"]}')
        if admin:
            if p['name']:lines.append('👤 Имя: '+escape(p['name']))
            if p['phone']:lines.append('📱 Телефон: '+escape(p['phone']))
    for section,title in SECTIONS:
        selected=[r for r in rows if r['section']==section]
        if not selected:continue
        lines.append('\n<b>'+title+'</b>')
        for r in selected:
            if r['rule']=='room':
                details=f'{r["quantity"]} дет. × {r["units"]*30} мин.'
            elif r['rule']=='hours':details=f'{r["units"]} ч.'
            elif r['rule']=='single':details='1 усл.'
            else:details=f'{r["quantity"]} {r["unit"]} × {r["price"]} ₽'
            lines.append(f'• {escape(r["name"])} — {details} = {line_sum(r)} ₽')
            if r['section'] in ('entertainment','decor') and r['description']:
                lines.append(f'  <i>{escape(r["description"][:110])}</i>')
        lines.append(f'<i>Подытог: {sum(line_sum(r) for r in selected)} ₽</i>')
    if not rows:lines.append('\nПока ничего не выбрано.')
    total=sum(line_sum(r) for r in rows)
    lines.append(f'\n<b>Итого: {total} ₽</b>')
    lines.append('Предварительный расчёт. Заявка не является бронированием или оплатой.')
    return '\n'.join(lines),total

def cart_buttons(uid):
    rows=cart_rows(uid);buttons=[]
    for r in rows:
        section,iid=r['section'],r['item_id']
        if r['rule']=='room':
            buttons.append([('➖',f'adj:{section}:{iid}:q-'),(f'👧 {r["quantity"]} дет.',f'noop'),('➕',f'adj:{section}:{iid}:q+')])
            buttons.append([('⏱ −30 мин',f'adj:{section}:{iid}:u-'),(f'{r["units"]*30} мин', 'noop'),('⏱ +30 мин',f'adj:{section}:{iid}:u+')])
        elif r['rule']=='hours':
            buttons.append([('➖',f'adj:{section}:{iid}:u-'),(f'{r["units"]} ч.', 'noop'),('➕',f'adj:{section}:{iid}:u+')])
            buttons.append([('🗑 Удалить услугу',f'drop:{section}:{iid}')])
        elif r['rule']=='single':
            buttons.append([('🗑 Удалить позицию',f'drop:{section}:{iid}')])
        else:
            buttons.append([('➖',f'adj:{section}:{iid}:q-'),(f'{r["quantity"]} {r["unit"]}', 'noop'),('➕',f'adj:{section}:{iid}:q+')])
    buttons.extend([[('➕ Добавить позиции','sections')],[('👧 Изменить число детей','children')]])
    if rows:buttons.extend([[('📨 Отправить заявку','submit')],[('🗑 Очистить смету','clearask')]])
    buttons.append([('🔄 Начать всё заново','restartask')]);buttons.append([('🏠 Главное меню','home')])
    return buttons

@router.callback_query(F.data=='noop')
async def noop(q:CallbackQuery):await q.answer()

@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    await state.clear()
    if not party(m.from_user.id) or not party(m.from_user.id)['children']:
        await state.set_state(Form.children)
        return await m.answer('🎉 Сколько детей будет на празднике? (от 1 до 30)', reply_markup=ForceReply(input_field_placeholder='Число от 1 до 30', selective=True))
    await m.answer('🎉 Выберите всё для праздника:',reply_markup=keyboard([[ (label,f'section:{key}') ] for key,label in SECTIONS]+nav(m.from_user.id)))

@router.message(Command('myid'))
async def myid(m:Message):await m.answer(f'Ваш Telegram ID: <code>{m.from_user.id}</code>',parse_mode='HTML')

@router.message(Command('cancel'))
async def cancel(m:Message,state:FSMContext):
    await state.clear();await m.answer('Действие отменено.',reply_markup=keyboard(home(m.from_user.id)))

@router.callback_query(F.data=='home')
async def go_home(q:CallbackQuery,state:FSMContext):
    await state.clear();await show(q,'🎉 <b>Хи-Хи — конструктор праздника</b>',home(q.from_user.id))

@router.callback_query(F.data=='begin')
async def begin(q:CallbackQuery):
    if party(q.from_user.id) and party(q.from_user.id)['children']>0:
        return await show(q,'Выберите раздел праздника:',[[ (label,f'section:{key}') ] for key,label in SECTIONS]+nav(q.from_user.id))
    await show(q,'Сколько детей будет на празднике? (от 1 до 30)',[[('👧 Указать количество детей','children')]])

@router.callback_query(F.data=='children')
async def children(q:CallbackQuery,state:FSMContext):
    await state.set_state(Form.children)
    await q.message.answer('👧 Введите количество детей (от 1 до 30):', reply_markup=ForceReply(input_field_placeholder='Число от 1 до 30', selective=True))
    await q.answer()

@router.message(Form.children,F.text)
async def children_value(m:Message,state:FSMContext):
    if not m.text.strip().isdigit() or not 1<=int(m.text.strip())<=30:return await m.answer('Введите число от 1 до 30.')
    set_party(m.from_user.id,'children',int(m.text.strip()))
    await state.clear()
    await m.answer('🎉 Выберите всё для праздника:',reply_markup=keyboard([[(label,f'section:{key}')] for key,label in SECTIONS]+nav(m.from_user.id)))

@router.callback_query(F.data=='sections')
async def sections(q:CallbackQuery):
    await show(q,'Выберите раздел:',[[(label,f'section:{key}')] for key,label in SECTIONS]+nav(q.from_user.id))

@router.callback_query(F.data.startswith('section:'))
async def section(q:CallbackQuery):
    key=q.data.split(':')[1]
    if key in ('child','adult'):
        buttons=[[(title, f'pizzasize:{key}' if g=='pizza' else f'list:{key}:{g}:0')] for g,title in GROUPS]
        buttons.insert(1,[('🍗 Наггетсы',f'nugstart:{key}')])
        buttons += [[('🧾 Моя смета','cart')],[('⬅️ Разделы','sections')]]
    elif key in ('entertainment','decor'):
        buttons=listing(key,key)
    else:return await q.answer('Неизвестный раздел',show_alert=True)
    await show(q,'Выберите категорию или позицию:',buttons)

@router.callback_query(F.data.startswith('list:'))
async def list_items(q:CallbackQuery):
    _,section,group,page=q.data.split(':')
    if group=='drinks':
        buttons=[[('🧃 Сок','drinkvol:'+section+':juice')],[('💧 Вода','drinkvol:'+section+':water')]]
        if section=='adult':buttons += [[('☕ Кофе','coffee:'+section)]]
        buttons += listing(section,group,int(page))
        return await show(q,'🥤 Выберите напиток:',buttons)
    await show(q,'Выберите позицию:',listing(section,group,int(page)))

def chosen_controls(uid,section,r,back=None):
    with db() as c:
        row=c.execute('SELECT quantity,units FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(uid,section,r['id'])).fetchone()
    qty=row['quantity'] if row else 0
    buttons=[]
    if r['rule']=='quantity':
        buttons.append([('➖',f'adjitem:{section}:{r["id"]}:-'),(f'{qty} {r["unit"]}', 'noop'),('➕',f'adjitem:{section}:{r["id"]}:+')])
    if back:buttons.append([('⬅️ Продолжить выбор',back)])
    buttons.append([('🧾 Моя смета','cart')])
    return buttons

def chosen_text(uid,section,r):
    with db() as c:
        row=c.execute('SELECT quantity FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(uid,section,r['id'])).fetchone()
    qty=row['quantity'] if row else 0
    return f'✅ <b>{escape(r["name"])}</b>\nВ смете: {qty} {escape(r["unit"])} × {r["price"]} ₽ = {qty*r["price"]} ₽'

@router.callback_query(F.data.startswith('pizzasize:'))
async def pizza_size(q:CallbackQuery):
    section=q.data.split(':')[1]
    await show(q,'🍕 Выберите размер пиццы:',[[('25 см — 700 ₽',f'pizzaflav:{section}:25')],[('30 см — 850 ₽',f'pizzaflav:{section}:30')],[('⬅️ Назад',f'section:{section}')]])

@router.callback_query(F.data.startswith('pizzaflav:'))
async def pizza_flavors(q:CallbackQuery):
    _,section,size=q.data.split(':')
    with db() as c:rows=c.execute('SELECT id,name FROM items WHERE category=? AND active=1 AND name LIKE ? ORDER BY id',('pizza',f'% {size} см')).fetchall()
    buttons=[[(r['name'].replace('Пицца ','').replace(f' {size} см',''),f'pizzaadd:{section}:{r["id"]}:{size}')] for r in rows]
    buttons += [[('🧾 Моя смета','cart')],[('⬅️ Размер',f'pizzasize:{section}')]]
    await show(q,f'🍕 Пицца {size} см. Нажимайте на вкусы: каждое нажатие добавляет 1 пиццу.\n',buttons)

@router.callback_query(F.data.startswith('pizzaadd:'))
async def pizza_add(q:CallbackQuery):
    _,section,raw,size=q.data.split(':');r=get_item(int(raw))
    if not r or not r['active'] or r['category']!='pizza':return await q.answer('Недоступно',show_alert=True)
    add_line(q.from_user.id,section,r['id'],1)
    with db() as c:rows=c.execute('SELECT id,name FROM items WHERE category=? AND active=1 AND name LIKE ? ORDER BY id',('pizza',f'% {size} см')).fetchall()
    buttons=[[(r2['name'].replace('Пицца ','').replace(f' {size} см',''),f'pizzaadd:{section}:{r2["id"]}:{size}')] for r2 in rows]
    buttons += [[('🧾 Моя смета','cart')],[('⬅️ Размер',f'pizzasize:{section}')]]
    await show(q,f'✅ Добавлена: {escape(r["name"])}.\nМожно нажать на тот же вкус ещё раз или выбрать другой:',buttons)

@router.callback_query(F.data.startswith('nugstart:'))
async def nug_start(q:CallbackQuery):
    section=q.data.split(':')[1]
    with db() as c:r=c.execute("SELECT id FROM items WHERE name='Наггетсы 2 шт.' AND active=1").fetchone()
    if not r:return await q.answer('Наггетсы недоступны',show_alert=True)
    await show(q,'🍗 Сколько порций наггетсов нужно?',[[('👧 По количеству детей',f'pick:{section}:{r["id"]}:kids')],[('✏️ Своё количество',f'pick:{section}:{r["id"]}:manual')],[('⬅️ Назад',f'section:{section}')]])

@router.callback_query(F.data.startswith('coffee:'))
async def coffee(q:CallbackQuery):
    section=q.data.split(':')[1]
    if section!='adult':return await q.answer('Только для родителей',show_alert=True)
    with db() as c:rows=c.execute("SELECT id,name,price FROM items WHERE category='drinks' AND active=1 AND name IN ('Американо','Капучино','Глясе','Латте','Эспрессо') ORDER BY id").fetchall()
    await show(q,'☕ Выберите кофе:',[[(f'{r["name"]} — {r["price"]} ₽',f'item:{section}:{r["id"]}')] for r in rows]+[[('⬅️ Напитки',f'list:{section}:drinks:0')]])

@router.callback_query(F.data.startswith('drinkvol:'))
async def drink_vol(q:CallbackQuery):
    _,section,kind=q.data.split(':')
    sizes=[('0,2 л',60),('1 л',250)] if kind=='juice' else [('0,5 л',60),('1,5 л',120)]
    await show(q,'Выберите объём:',[[(f'{size} — {price} ₽',f'drinkvariant:{section}:{kind}:{"small" if i==0 else "large"}')] for i,(size,price) in enumerate(sizes)]+[[('⬅️ Напитки',f'list:{section}:drinks:0')]])

@router.callback_query(F.data.startswith('drinkvariant:'))
async def drink_variant(q:CallbackQuery):
    _,section,kind,size=q.data.split(':')
    vol=('0,2 л' if size=='small' else '1 л') if kind=='juice' else ('0,5 л' if size=='small' else '1,5 л')
    with db() as c:rows=c.execute('SELECT id,name FROM items WHERE category=? AND active=1 AND name LIKE ? ORDER BY id',('drinks',('Сок %' if kind=='juice' else 'Вода %')+'%'+vol)).fetchall()
    buttons=[[(r['name'].replace('Сок ','').replace('Вода ','').replace(', '+vol,''),f'drinkpick:{section}:{r["id"]}:{kind}:{size}')] for r in rows]
    buttons += [[('🧾 Моя смета','cart')],[('⬅️ Объём',f'drinkvol:{section}:{kind}')]]
    await show(q,'Выберите '+('вкус сока:' if kind=='juice' else 'тип воды:'),buttons)

@router.callback_query(F.data.startswith('drinkpick:'))
async def drink_pick(q:CallbackQuery,state:FSMContext):
    _,section,raw,kind,size=q.data.split(':');r=get_item(int(raw))
    if not r or not r['active'] or r['category']!='drinks':return await q.answer('Недоступно',show_alert=True)
    if size=='small':
        p=party(q.from_user.id);n=p['children'] if p else 0
        return await show(q,f'{escape(r["name"])}. Сколько упаковок добавить?',[[ (f'👧 По количеству детей — {n}',f'drinkadd:{section}:{r["id"]}:{n}:{kind}:{size}') ],[('✏️ Своё количество',f'drinkmanual:{section}:{r["id"]}:{kind}:{size}')],[('⬅️ Назад',f'drinkvariant:{section}:{kind}:{size}')]])
    add_line(q.from_user.id,section,r['id'],1)
    await show(q,chosen_text(q.from_user.id,section,r),chosen_controls(q.from_user.id,section,r,f'drinkvariant:{section}:{kind}:{size}'))

@router.callback_query(F.data.startswith('drinkadd:'))
async def drink_add(q:CallbackQuery):
    _,section,raw,qty,kind,size=q.data.split(':');r=get_item(int(raw))
    if not r or not r['active']:return await q.answer('Недоступно',show_alert=True)
    add_line(q.from_user.id,section,r['id'],int(qty))
    await show(q,chosen_text(q.from_user.id,section,r),chosen_controls(q.from_user.id,section,r,f'drinkvariant:{section}:{kind}:{size}'))

@router.callback_query(F.data.startswith('drinkmanual:'))
async def drink_manual(q:CallbackQuery,state:FSMContext):
    _,section,raw,kind,size=q.data.split(':')
    await state.set_state(Form.quantity);await state.update_data(section=section,iid=int(raw),back=f'drinkvariant:{section}:{kind}:{size}')
    await q.message.answer('Сколько упаковок добавить? Отправьте число от 1 до 1000.')
    await q.answer()

@router.callback_query(F.data.startswith('item:'))
async def item(q:CallbackQuery):
    _,section,raw=q.data.split(':');r=get_item(int(raw))
    if not r or not r['active']:return await q.answer('Позиция недоступна',show_alert=True)
    if section in ('child','adult') and r['category'] not in {g for g,_ in GROUPS}:return await q.answer('Недоступно',show_alert=True)
    if section=='child' and r['category']=='drinks' and r['name'] not in ('Чай 180 мл','Детское шампанское') and not r['name'].startswith(('Вода ','Сок ')):return await q.answer('Недоступно',show_alert=True)
    if section in ('entertainment','decor') and r['category']!=section:return await q.answer('Недоступно',show_alert=True)
    if r['rule'] in ('single','hours'):
        added=add_line(q.from_user.id,section,r['id'],1)
        msg=('✅ Добавлено: ' if added else 'Уже в смете: ')+escape(r['name'])
        return await show(q,msg,[[('➕ Ещё услуги',f'section:{section}')],[('🧾 Моя смета','cart')]])
    if r['rule']=='room' or r['per_child']:
        p=party(q.from_user.id);n=p['children'] if p else 0
        return await show(q,f'<b>{escape(r["name"])}</b> — {r["price"]} ₽\nСколько добавить?',[[ (f'👧 По количеству детей — {n}',f'pick:{section}:{r["id"]}:kids') ],[('✏️ Своё количество',f'pick:{section}:{r["id"]}:manual')],[('⬅️ Назад',f'section:{section}' if section in ('entertainment','decor') else f'list:{section}:{r["category"]}:0')]])
    add_line(q.from_user.id,section,r['id'],1)
    back=f'section:{section}' if section in ('entertainment','decor') else (f'coffee:{section}' if r['name'] in ('Американо','Капучино','Глясе','Латте','Эспрессо') else f'list:{section}:{r["category"]}:0')
    await show(q,chosen_text(q.from_user.id,section,r),chosen_controls(q.from_user.id,section,r,back))

@router.callback_query(F.data.startswith('adjitem:'))
async def adjust_item(q:CallbackQuery):
    _,section,raw,op=q.data.split(':');iid=int(raw);r=get_item(iid)
    if not r:return await q.answer('Не найдено',show_alert=True)
    with db() as c:
        row=c.execute('SELECT quantity FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(q.from_user.id,section,iid)).fetchone()
        if op=='+':
            if row:c.execute('UPDATE cart_v2 SET quantity=quantity+1 WHERE user_id=? AND section=? AND item_id=?',(q.from_user.id,section,iid))
            else:c.execute('INSERT INTO cart_v2(user_id,section,item_id,quantity) VALUES(?,?,?,1)',(q.from_user.id,section,iid))
        elif row:
            if row['quantity']==1:c.execute('DELETE FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(q.from_user.id,section,iid))
            else:c.execute('UPDATE cart_v2 SET quantity=quantity-1 WHERE user_id=? AND section=? AND item_id=?',(q.from_user.id,section,iid))
    await show(q,chosen_text(q.from_user.id,section,r),chosen_controls(q.from_user.id,section,r,(f'coffee:{section}' if r['name'] in ('Американо','Капучино','Глясе','Латте','Эспрессо') else f'list:{section}:{r["category"]}:0')))

@router.callback_query(F.data.startswith('pick:'))
async def pick(q:CallbackQuery,state:FSMContext):
    _,section,raw,mode=q.data.split(':');iid=int(raw);r=get_item(iid)
    if not r or not r['active']:return await q.answer('Позиция недоступна',show_alert=True)
    if mode=='kids':
        p=party(q.from_user.id)
        if not p or not p['children']:
            await state.set_state(Form.children)
            return await show(q,'Сначала укажите число детей.',[[('👧 Указать количество детей','children')]])
        quantity=p['children']
        if r['name'].startswith('Наггетсы'):
            await show(q,f'Выбрано {quantity} порций. Сколько наггетсов в каждой порции?',nugget_buttons(section,quantity))
        else:
            add_line(q.from_user.id,section,iid,quantity)
            await show(q,chosen_text(q.from_user.id,section,r),chosen_controls(q.from_user.id,section,r,f'section:{section}' if section in ('entertainment','decor') else f'list:{section}:{r["category"]}:0'))
    else:
        await state.set_state(Form.quantity)
        await state.update_data(section=section,iid=iid,back=f'section:{section}' if section in ('entertainment','decor') else f'list:{section}:{r["category"]}:0')
        await q.message.answer('Сколько порций / штук / человек нужно? Отправьте целое число от 1 до 1000.')
        await q.answer()

def nugget_buttons(section,qty):
    with db() as c:
        rows=c.execute("SELECT id,name,price FROM items WHERE name LIKE 'Наггетсы %' AND active=1 ORDER BY price").fetchall()
    return [[(f'{r["name"]} — {r["price"]} ₽/порц.',f'nug:{section}:{r["id"]}:{qty}')] for r in rows]+[[('🧾 Моя смета','cart')]]

@router.message(Form.quantity,F.text)
async def quantity_value(m:Message,state:FSMContext):
    raw=m.text.strip()
    if not raw.isdigit() or not 1<=int(raw)<=1000:return await m.answer('Отправьте целое число от 1 до 1000.')
    data=await state.get_data();section=data['section'];iid=data['iid'];r=get_item(iid);qty=int(raw)
    await state.clear()
    if not r or not r['active']:return await m.answer('Позиция недоступна.')
    if r['name'].startswith('Наггетсы'):
        return await m.answer(f'Выбрано {qty} порций. Сколько наггетсов в порции?',reply_markup=keyboard(nugget_buttons(section,qty)))
    add_line(m.from_user.id,section,iid,qty)
    await m.answer(chosen_text(m.from_user.id,section,r),reply_markup=keyboard(chosen_controls(m.from_user.id,section,r,data.get('back'))),parse_mode='HTML')

@router.callback_query(F.data.startswith('nug:'))
async def nugget_choice(q:CallbackQuery):
    _,section,raw,rawqty=q.data.split(':');iid=int(raw);qty=int(rawqty);r=get_item(iid)
    if not r or not r['active'] or not r['name'].startswith('Наггетсы'):return await q.answer('Недоступно',show_alert=True)
    add_line(q.from_user.id,section,iid,qty)
    await show(q,chosen_text(q.from_user.id,section,r),chosen_controls(q.from_user.id,section,r,f'nugstart:{section}'))

def after_add(section,r):
    back=f'section:{section}' if section in ('entertainment','decor') else f'list:{section}:{r["category"]}:0'
    return [[('➕ Продолжить выбор',back)],[('🧾 Моя смета','cart')],[('🏠 Главное меню','home')]]

@router.callback_query(F.data.startswith('add:'))
async def add(q:CallbackQuery):
    _,section,raw,rawqty,rawunits=q.data.split(':');iid=int(raw);r=get_item(iid)
    if not r or not r['active']:return await q.answer('Недоступно',show_alert=True)
    add_line(q.from_user.id,section,iid,int(rawqty),int(rawunits))
    await show(q,'Услуга добавлена / продлена.',after_add(section,r))

@router.callback_query(F.data=='cart')
async def cart(q:CallbackQuery):
    txt,_=cart_text(q.from_user.id)
    await show(q,txt,cart_buttons(q.from_user.id))

@router.callback_query(F.data.startswith('adj:'))
async def adjust(q:CallbackQuery):
    _,section,raw,action=q.data.split(':');iid=int(raw);uid=q.from_user.id
    with db() as c:
        r=c.execute('SELECT quantity,units FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(uid,section,iid)).fetchone()
        if not r:return await q.answer('Позиция уже удалена',show_alert=True)
        field='units' if action.startswith('u') else 'quantity'
        delta=1 if action.endswith('+') else -1
        value=r[field]+delta
        if value<=0 or (field=='units' and value<1):
            if field=='quantity':c.execute('DELETE FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(uid,section,iid))
            else:return await q.answer('Нельзя уменьшить ниже базового времени',show_alert=True)
        else:c.execute(f'UPDATE cart_v2 SET {field}=? WHERE user_id=? AND section=? AND item_id=?',(value,uid,section,iid))
    txt,_=cart_text(uid);await show(q,txt,cart_buttons(uid))

@router.callback_query(F.data.startswith('drop:'))
async def drop(q:CallbackQuery):
    _,section,raw=q.data.split(':')
    with db() as c:c.execute('DELETE FROM cart_v2 WHERE user_id=? AND section=? AND item_id=?',(q.from_user.id,section,int(raw)))
    txt,_=cart_text(q.from_user.id);await show(q,txt,cart_buttons(q.from_user.id))

@router.callback_query(F.data=='clearask')
async def clearask(q:CallbackQuery):
    await show(q,'Точно очистить всю смету? Это действие нельзя отменить.',[[('🗑 Да, очистить','clearconfirm')],[('Нет, вернуться','cart')]])

@router.callback_query(F.data=='clearconfirm')
async def clearconfirm(q:CallbackQuery):
    with db() as c:
        c.execute('DELETE FROM cart_v2 WHERE user_id=?',(q.from_user.id,))
        c.execute('DELETE FROM party WHERE user_id=?',(q.from_user.id,))
    await show(q,'Смета очищена.',home(q.from_user.id))

@router.callback_query(F.data=='restartask')
async def restart_ask(q:CallbackQuery):
    await show(q,'🔄 Начать всё заново? Смета и количество детей будут сброшены.',[[('Да, начать заново','restartconfirm')],[('Нет, вернуться','cart')]])

@router.callback_query(F.data=='restartconfirm')
async def restart_confirm(q:CallbackQuery,state:FSMContext):
    with db() as c:
        c.execute('DELETE FROM cart_v2 WHERE user_id=?',(q.from_user.id,))
        c.execute('DELETE FROM party WHERE user_id=?',(q.from_user.id,))
    await state.set_state(Form.children)
    await show(q,'🎉 Сколько детей будет на празднике? (от 1 до 30)',[])

@router.callback_query(F.data=='submit')
async def submit(q:CallbackQuery,state:FSMContext):
    if not cart_rows(q.from_user.id):return await q.answer('Смета пуста',show_alert=True)
    await state.set_state(Form.name)
    await q.message.answer('Как к вам обращаться? Отправьте имя (до 80 символов).')
    await q.answer()

@router.message(Form.name,F.text)
async def name_value(m:Message,state:FSMContext):
    name=m.text.strip()
    if not 1<=len(name)<=80:return await m.answer('Введите имя длиной до 80 символов.')
    set_party(m.from_user.id,'name',name)
    await state.set_state(Form.phone)
    await m.answer('Укажите номер телефона для связи (например, +7 999 123-45-67).')

@router.message(Form.phone,F.text)
async def phone_value(m:Message,state:FSMContext,bot:Bot):
    phone=m.text.strip()
    digits=re.sub(r'\D','',phone)
    if not 10<=len(digits)<=15 or len(phone)>35:return await m.answer('Пожалуйста, укажите номер телефона (10–15 цифр).')
    uid=m.from_user.id;set_party(uid,'phone',phone)
    txt,total=cart_text(uid,admin=True)
    destination=int(setting('recipient') or OWNER)
    with db() as c:
        cur=c.execute('INSERT INTO orders(user_id,created,summary,total) VALUES(?,?,?,?)',(uid,datetime.now(timezone.utc).isoformat(),txt,total))
        order_id=cur.lastrowid
    username=m.from_user.username
    who=f'@{escape(username)}' if username else f'ID {uid}'
    msg=f'📨 <b>Новая заявка №{order_id}</b>\nTelegram: {who} (<code>{uid}</code>)\n\n{txt}'
    try:
        await bot.send_message(destination,msg,parse_mode='HTML')
    except Exception:
        logging.exception('Delivery failed for order %s',order_id)
        with db() as c:c.execute("UPDATE orders SET status='delivery_failed' WHERE id=?",(order_id,))
        await m.answer('Не удалось доставить заявку. Смета сохранена; попробуйте позже.',reply_markup=keyboard(home(uid)))
    else:
        with db() as c:
            c.execute("UPDATE orders SET status='sent' WHERE id=?",(order_id,))
            c.execute('DELETE FROM cart_v2 WHERE user_id=?',(uid,))
            c.execute('DELETE FROM party WHERE user_id=?',(uid,))
        await m.answer(f'🎉 Заявка №{order_id} отправлена! Сумма: {total} ₽. Администратор свяжется с вами. Это не бронь и не оплата.',reply_markup=keyboard(home(uid)))
    await state.clear()

# Owner-only administration. Recipient of requests does not gain editing rights.
@router.callback_query(F.data=='admin')
async def admin(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    await show(q,'⚙️ Управление «Хи-Хи»',[[('🗂 Каталог','admincats')],[('📨 Последние заявки','orders')],[('👤 Получатель заявок','recipient')],[('🏠 Главное меню','home')]])

@router.callback_query(F.data=='recipient')
async def recipient(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    await show(q,f'📨 Текущий получатель: <code>{escape(setting("recipient") or str(OWNER))}</code>\nПо умолчанию — @GreenGerGreen.',[[('✏️ Сменить получателя','setrecipient')],[('↩️ Вернуть владельцу','resetrecipient')],[('⬅️ Назад','admin')]])

@router.callback_query(F.data=='resetrecipient')
async def resetrecipient(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    with db() as c:c.execute("INSERT INTO settings(key,value) VALUES('recipient',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(OWNER),))
    await show(q,'Заявки снова направляются владельцу @GreenGerGreen.',[[('⬅️ Управление','admin')]])

@router.callback_query(F.data=='setrecipient')
async def setrecipient(q:CallbackQuery,state:FSMContext):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    await state.set_state(Form.admin_target)
    await q.message.answer('Отправьте числовой Telegram ID нового получателя. Он должен предварительно нажать /start в этом боте. Узнать ID можно командой /myid. Для отмены /cancel.')
    await q.answer()

@router.message(Form.admin_target,F.text)
async def target_value(m:Message,state:FSMContext,bot:Bot):
    if not owner(m.from_user.id):await state.clear();return
    raw=m.text.strip()
    if not raw.isdigit():return await m.answer('Нужен числовой Telegram ID.')
    try:await bot.send_message(int(raw),'Вы назначены получателем заявок кафе «Хи-Хи».')
    except Exception:return await m.answer('Не удалось написать пользователю. Пусть он нажмёт /start, затем попробуйте снова.')
    with db() as c:c.execute("INSERT INTO settings(key,value) VALUES('recipient',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(raw,))
    await state.clear();await m.answer('Получатель изменён. Права владельца остались только у вас.',reply_markup=keyboard(home(m.from_user.id)))

@router.callback_query(F.data=='admincats')
async def admincats(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    await show(q,'Категория для редактирования:',[[(title,f'acat:{key}:0')] for key,title in CATEGORIES]+[[('⬅️ Управление','admin')]])

def admin_listing(cat,page):
    with db() as c:
        rows=c.execute('SELECT * FROM items WHERE category=? ORDER BY id LIMIT 7 OFFSET ?',(cat,page*7)).fetchall()
        total=c.execute('SELECT COUNT(*) n FROM items WHERE category=?',(cat,)).fetchone()['n']
    buttons=[[(('🟢 ' if r['active'] else '⚪ ')+r['name'][:37],f'edit:{r["id"]}')] for r in rows]
    navrow=[]
    if page:navrow.append(('◀️',f'acat:{cat}:{page-1}'))
    if (page+1)*7<total:navrow.append(('▶️',f'acat:{cat}:{page+1}'))
    if navrow:buttons.append(navrow)
    buttons += [[('➕ Добавить позицию',f'new:{cat}')],[('⬅️ Категории','admincats')]]
    return buttons

@router.callback_query(F.data.startswith('acat:'))
async def acat(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    _,cat,page=q.data.split(':')
    await show(q,'Редактирование позиций:',admin_listing(cat,int(page)))

@router.callback_query(F.data.startswith('edit:'))
async def edit(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    iid=int(q.data.split(':')[1]);r=get_item(iid)
    if not r:return await q.answer('Не найдено',show_alert=True)
    txt=f'<b>{escape(r["name"])}</b>\nЦена: {r["price"]} ₽\n{escape(r["description"])}\nСтатус: {"доступна" if r["active"] else "скрыта"}\nТип: {r["rule"]}; ед.: {r["unit"]}; по детям: {"да" if r["per_child"] else "нет"}'
    buttons=[[('✏️ Название',f'field:{iid}:name'),('💰 Цена',f'field:{iid}:price')],[('📝 Описание',f'field:{iid}:description')],[('📏 Единица',f'field:{iid}:unit')],[('👧 По числу детей: да/нет',f'perchild:{iid}')],[('⚙️ Правило количества',f'rules:{iid}')],[('👁 Скрыть / показать',f'toggle:{iid}')],[('⬅️ Назад',f'acat:{r["category"]}:0')]]
    await show(q,txt,buttons)

@router.callback_query(F.data.startswith('rules:'))
async def rules(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    iid=int(q.data.split(':')[1])
    await show(q,'Выберите правило подсчёта:',[[(label,f'rule:{iid}:{key}')] for key,label in [('quantity','Количество'),('single','Одна услуга'),('hours','Часы, +1 час'),('room','Игровая комната, +30 минут')]]+[[('⬅️ Назад',f'edit:{iid}')]])

@router.callback_query(F.data.startswith('rule:'))
async def rule(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    _,raw,value=q.data.split(':')
    if value not in {'quantity','single','hours','room'}:return await q.answer('Ошибка',show_alert=True)
    with db() as c:c.execute('UPDATE item_rules SET rule=? WHERE item_id=?',(value,int(raw)))
    await show(q,'Правило изменено.',[[('⬅️ Позиция',f'edit:{raw}')]])

@router.callback_query(F.data.startswith('perchild:'))
async def perchild(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    iid=int(q.data.split(':')[1])
    with db() as c:c.execute('UPDATE item_rules SET per_child=1-per_child WHERE item_id=?',(iid,))
    await show(q,'Настройка изменена.',[[('⬅️ Позиция',f'edit:{iid}')]])

@router.callback_query(F.data.startswith('toggle:'))
async def toggle(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    iid=int(q.data.split(':')[1])
    with db() as c:c.execute('UPDATE items SET active=1-active WHERE id=?',(iid,))
    await show(q,'Видимость изменена.',[[('⬅️ Позиция',f'edit:{iid}')]])

@router.callback_query(F.data.startswith('field:'))
async def field(q:CallbackQuery,state:FSMContext):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    _,raw,key=q.data.split(':')
    if key not in {'name','price','description','unit'}:return await q.answer('Ошибка',show_alert=True)
    await state.set_state(Form.admin_edit);await state.update_data(iid=int(raw),field=key)
    await q.message.answer(f'Введите новое значение «{key}». Для отмены /cancel.')
    await q.answer()

@router.message(Form.admin_edit,F.text)
async def edit_value(m:Message,state:FSMContext):
    if not owner(m.from_user.id):await state.clear();return
    data=await state.get_data();key=data['field'];value=m.text.strip()
    if key=='price':
        if not value.isdigit() or int(value)>10000000:return await m.answer('Цена должна быть целым числом от 0 до 10 000 000.')
        value=int(value)
    elif (key in {'name','unit'} and not value) or len(value)>{'name':120,'description':800,'unit':20}.get(key,800):
        return await m.answer('Проверьте длину текста.')
    with db() as c:
        table='item_rules' if key=='unit' else 'items'
        c.execute(f'UPDATE {table} SET {key}=? WHERE {"item_id" if key=="unit" else "id"}=?',(value,data['iid']))
    await state.clear();await m.answer('Сохранено.',reply_markup=keyboard([[('⬅️ Позиция',f'edit:{data["iid"]}')]]))

@router.callback_query(F.data.startswith('new:'))
async def new(q:CallbackQuery,state:FSMContext):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    cat=q.data.split(':')[1]
    if cat not in {x for x,_ in CATEGORIES}:return await q.answer('Неизвестная категория',show_alert=True)
    await state.set_state(Form.admin_add);await state.update_data(cat=cat)
    await q.message.answer('Отправьте три строки:\nНазвание\nЦена (целое число)\nОписание\n\nДля отмены /cancel.')
    await q.answer()

@router.message(Form.admin_add,F.text)
async def new_value(m:Message,state:FSMContext):
    if not owner(m.from_user.id):await state.clear();return
    lines=m.text.split('\n',2)
    if len(lines)!=3 or not lines[1].strip().isdigit():return await m.answer('Нужны три строки: название, цена, описание.')
    name,price,desc=map(str.strip,lines)
    if not name or len(name)>120 or len(desc)>800 or int(price)>10000000:return await m.answer('Проверьте значения.')
    data=await state.get_data()
    with db() as c:
        cur=c.execute('INSERT INTO items(category,name,price,description) VALUES(?,?,?,?)',(data['cat'],name,int(price),desc))
        iid=cur.lastrowid
        rule,unit,per=infer_rule({'name':name,'category':data['cat'],'description':desc})
        c.execute('INSERT INTO item_rules(item_id,rule,unit,per_child) VALUES(?,?,?,?)',(iid,rule,unit,per))
    await state.clear();await m.answer('Позиция добавлена.',reply_markup=keyboard([[('⬅️ Позиция',f'edit:{iid}')]]))

@router.callback_query(F.data=='orders')
async def orders(q:CallbackQuery):
    if not owner(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    with db() as c:rows=c.execute('SELECT id,total,status FROM orders ORDER BY id DESC LIMIT 10').fetchall()
    txt='📨 <b>Последние заявки</b>\n'+'\n'.join(f'№{r["id"]}: {r["total"]} ₽ — {escape(r["status"])}' for r in rows)
    await show(q,txt,[[('⬅️ Управление','admin')]])

@router.message()
async def fallback(m:Message):
    await m.answer('Используйте кнопки меню или /start. Для отмены /cancel.',reply_markup=keyboard(home(m.from_user.id)))

async def main():
    if not TOKEN:raise RuntimeError('BOT_TOKEN must be configured in host environment')
    init_db()
    logging.basicConfig(level=logging.INFO)
    bot=Bot(TOKEN)
    dp=Dispatcher();dp.include_router(router)
    await dp.start_polling(bot)

if __name__=='__main__':asyncio.run(main())
