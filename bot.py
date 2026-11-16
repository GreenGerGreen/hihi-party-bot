import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from catalog import CATEGORIES, ITEMS, PROGRAMS

load_dotenv()
TOKEN=os.getenv('BOT_TOKEN','')
OWNER=int(os.getenv('OWNER_ID','142840890'))
DB_PATH=os.getenv('DB_PATH','hihi.sqlite3')
router=Router()

class Form(StatesGroup):
    qty=State(); contact=State(); age=State(); date=State(); name=State()
    admin_edit=State(); admin_add=State(); admin_target=State(); program=State()

def db():
    con=sqlite3.connect(DB_PATH)
    con.row_factory=sqlite3.Row
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
        ''')
        if not c.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
            c.executemany('INSERT INTO categories(id,title) VALUES(?,?)',CATEGORIES)
            c.executemany('INSERT INTO items(category,name,price,description) VALUES(?,?,?,?)',ITEMS)
        c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',('recipient',str(OWNER)))

def setting(k):
    with db() as c:
        r=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()
        return r['value'] if r else ''
def is_owner(uid): return uid==OWNER
def is_staff(uid): return is_owner(uid) or str(uid)==setting('recipient')
def kb(rows): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=d) for t,d in row] for row in rows])
def home(uid):
    rows=[[('🎉 Собрать праздник','categories')],[('🧾 Моя смета','cart')],[('🎭 Темы анимации','programs')]]
    if is_staff(uid): rows.append([('⚙️ Управление','admin')])
    return kb(rows)
def cats(admin=False):
    with db() as c: rows=c.execute('SELECT * FROM categories WHERE active=1 ORDER BY rowid').fetchall()
    return kb([[(r['title'],f'{"acat" if admin else "cat"}:{r["id"]}:0')] for r in rows]+[[('⬅️ Назад','admin' if admin else 'home')]])
def page_items(cat,page=0,admin=False):
    with db() as c:
        rows=c.execute('SELECT * FROM items WHERE category=? '+('' if admin else 'AND active=1 ')+ 'ORDER BY id LIMIT 7 OFFSET ?', (cat,page*7)).fetchall()
        total=c.execute('SELECT count(*) n FROM items WHERE category=? '+('' if admin else 'AND active=1'),(cat,)).fetchone()['n']
    action='edit' if admin else 'item'
    buttons=[[(('🟢 ' if r['active'] else '⚪ ')+r['name'] if admin else r['name']+f' — {r["price"]} ₽',f'{action}:{r["id"]}')] for r in rows]
    nav=[]
    if page>0:nav.append(('◀️',f'{"acat" if admin else "cat"}:{cat}:{page-1}'))
    if (page+1)*7<total:nav.append(('▶️',f'{"acat" if admin else "cat"}:{cat}:{page+1}'))
    if nav:buttons.append(nav)
    if admin:buttons.append([('➕ Добавить позицию',f'add:{cat}')])
    buttons.append([('⬅️ Категории','admincats' if admin else 'categories')])
    return kb(buttons)
async def show(call,text,markup):
    try:await call.message.edit_text(text,reply_markup=markup,parse_mode='HTML')
    except Exception:await call.message.answer(text,reply_markup=markup,parse_mode='HTML')
    await call.answer()

def cart_data(uid):
    with db() as c:
        rows=c.execute('''SELECT i.name,i.price,c.quantity,i.id FROM carts c JOIN items i ON i.id=c.item_id WHERE c.user_id=? ORDER BY i.category,i.id''',(uid,)).fetchall()
        d=c.execute('SELECT * FROM drafts WHERE user_id=?',(uid,)).fetchone()
    return rows,d

def cart_text(uid):
    rows,d=cart_data(uid)
    total=sum(r['price']*r['quantity'] for r in rows)
    parts=['🧾 <b>Ваша смета</b>']
    if d:
        for k,label in [('age','Возраст'),('date','Дата'),('name','Имя'),('program','Тема анимации')]:
            if d[k]:parts.append(f'{label}: {escape(d[k])}')
    parts.append('')
    if not rows:parts.append('Пока ничего не выбрано.')
    for r in rows:parts.append(f'• {escape(r["name"])} × {r["quantity"]} = {r["price"]*r["quantity"]} ₽')
    parts.append(f'\n<b>Итого: {total} ₽</b>')
    parts.append('Стоимость предварительная; отправка заявки не является бронированием или оплатой.')
    return '\n'.join(parts),total

def cart_kb(uid):
    rows,_=cart_data(uid)
    buttons=[[('➕ Добавить позиции','categories')]]
    for r in rows:buttons.append([(f'➖ {r["name"][:20]}',f'dec:{r["id"]}'),('➕',f'inc:{r["id"]}')])
    buttons += [[('🎭 Выбрать тему анимации','programs')],[('🎂 Возраст','age'),('📅 Дата','date')]]
    if rows:buttons += [[('📨 Отправить заявку','submit')],[('🗑 Очистить смету','clear')]]
    buttons.append([('🏠 Главное меню','home')])
    return kb(buttons)

@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    await state.clear()
    await m.answer('🎉 Добро пожаловать в кафе «Хи-Хи»!\nСоберите детский праздник и узнайте предварительную стоимость.',reply_markup=home(m.from_user.id))
@router.message(Command('myid'))
async def myid(m:Message):await m.answer(f'Ваш Telegram ID: <code>{m.from_user.id}</code>',parse_mode='HTML')
@router.message(Command('cancel'))
async def cancel(m:Message,state:FSMContext):
    await state.clear();await m.answer('Действие отменено.',reply_markup=home(m.from_user.id))
@router.callback_query(F.data=='home')
async def go_home(q:CallbackQuery,state:FSMContext):
    await state.clear();await show(q,'🎉 <b>Хи-Хи — конструктор праздника</b>',home(q.from_user.id))
@router.callback_query(F.data=='categories')
async def categories(q:CallbackQuery):await show(q,'Выберите раздел:',cats())
@router.callback_query(F.data.startswith('cat:'))
async def category(q:CallbackQuery):
    _,cat,page=q.data.split(':');await show(q,'Выберите позицию:',page_items(cat,int(page)))
@router.callback_query(F.data.startswith('item:'))
async def item(q:CallbackQuery):
    iid=int(q.data.split(':')[1])
    with db() as c:r=c.execute('SELECT * FROM items WHERE id=? AND active=1',(iid,)).fetchone()
    if not r:return await q.answer('Позиция недоступна',show_alert=True)
    await show(q,f'<b>{escape(r["name"])}</b>\n{escape(r["description"])}\nЦена: {r["price"]} ₽',kb([[('➕ В смету',f'plus:{iid}')],[('⬅️ Назад',f'cat:{r["category"]}:0')]]))
@router.callback_query(F.data.startswith(('plus:','inc:','dec:')))
async def adjust(q:CallbackQuery):
    action,sid=q.data.split(':');iid=int(sid);uid=q.from_user.id
    with db() as c:
        r=c.execute('SELECT active FROM items WHERE id=?',(iid,)).fetchone()
        if not r or (not r['active'] and action!='dec'):return await q.answer('Позиция недоступна',show_alert=True)
        if action=='dec':
            c.execute('UPDATE carts SET quantity=quantity-1 WHERE user_id=? AND item_id=?',(uid,iid))
            c.execute('DELETE FROM carts WHERE user_id=? AND item_id=? AND quantity<=0',(uid,iid))
        else:c.execute('INSERT INTO carts(user_id,item_id,quantity) VALUES(?,?,1) ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+1',(uid,iid))
    if action=='plus':await q.answer('Добавлено в смету');await q.message.answer('Добавлено! Продолжайте выбирать или откройте смету.',reply_markup=kb([[('🧾 Моя смета','cart')],[('🍽 Категории','categories')]]))
    else:
        txt,_=cart_text(uid);await show(q,txt,cart_kb(uid))
@router.callback_query(F.data=='cart')
async def cart(q:CallbackQuery):
    txt,_=cart_text(q.from_user.id);await show(q,txt,cart_kb(q.from_user.id))
@router.callback_query(F.data=='clear')
async def clear(q:CallbackQuery):
    with db() as c:
        c.execute('DELETE FROM carts WHERE user_id=?',(q.from_user.id,))
        c.execute('DELETE FROM drafts WHERE user_id=?',(q.from_user.id,))
    await show(q,'Смета очищена.',home(q.from_user.id))
@router.callback_query(F.data.in_({'age','date'}))
async def meta(q:CallbackQuery,state:FSMContext):
    field=q.data
    await state.set_state(Form.age if field=='age' else Form.date)
    await q.message.answer('Введите возраст именинника (число от 1 до 18):' if field=='age' else 'Введите желаемую дату праздника (например, 25.12.2026):')
    await q.answer()
@router.message(Form.age, F.text)
async def set_age(m:Message,state:FSMContext):
    if not m.text.isdigit() or not 1<=int(m.text)<=18:return await m.answer('Введите число от 1 до 18.')
    save_draft(m.from_user.id,'age',m.text);await state.clear();await m.answer('Возраст сохранён.',reply_markup=kb([[('🧾 Смета','cart')]]))
@router.message(Form.date,F.text)
async def set_date(m:Message,state:FSMContext):
    value=m.text.strip()
    if len(value)>60:return await m.answer('Дата слишком длинная.')
    save_draft(m.from_user.id,'date',value);await state.clear();await m.answer('Дата сохранена. Доступность согласует администратор.',reply_markup=kb([[('🧾 Смета','cart')]]))
def save_draft(uid,key,value):
    if key not in {'age','date','name','contact','program'}:raise ValueError('invalid field')
    with db() as c:
        c.execute('INSERT OR IGNORE INTO drafts(user_id) VALUES(?)',(uid,))
        c.execute(f'UPDATE drafts SET {key}=? WHERE user_id=?',(value,uid))
@router.callback_query(F.data=='programs')
async def programs(q:CallbackQuery):
    await show(q,'🎭 Выберите возрастную группу анимации:',kb([[(label,f'prg:{i}')] for i,label in enumerate(PROGRAMS)]+[[('⬅️ Назад','home')]]))
@router.callback_query(F.data.startswith('prg:'))
async def program_group(q:CallbackQuery):
    index=int(q.data.split(':')[1]);label=list(PROGRAMS)[index]
    await show(q,f'🎭 <b>{escape(label)}</b>\nВыберите тему:',kb([[(x[:48],f'theme:{index}:{i}')] for i,x in enumerate(PROGRAMS[label])]+[[('⬅️ Группы','programs')]]))
@router.callback_query(F.data.startswith('theme:'))
async def theme(q:CallbackQuery):
    _,idx,num=q.data.split(':');name=PROGRAMS[list(PROGRAMS)[int(idx)]][int(num)]
    save_draft(q.from_user.id,'program',name)
    await show(q,f'Выбрана тема «{escape(name)}».\nЧтобы включить анимацию в стоимость, добавьте услугу «Анимационная программа» (4500 ₽) в смету.',kb([[('🎭 Добавить анимацию','cat:entertainment:0')],[('🧾 Смета','cart')]]))
@router.callback_query(F.data=='submit')
async def submit(q:CallbackQuery,state:FSMContext):
    rows,_=cart_data(q.from_user.id)
    if not rows:return await q.answer('Смета пуста',show_alert=True)
    await state.set_state(Form.name);await q.message.answer('Как к вам обращаться? Укажите имя (до 80 символов). Для отмены /cancel');await q.answer()
@router.message(Form.name,F.text)
async def contact_name(m:Message,state:FSMContext):
    name=m.text.strip()
    if not 1<=len(name)<=80:return await m.answer('Введите имя длиной до 80 символов.')
    save_draft(m.from_user.id,'name',name);await state.set_state(Form.contact)
    await m.answer('Укажите телефон или Telegram для обратной связи (до 100 символов):')
@router.message(Form.contact,F.text)
async def contact_final(m:Message,state:FSMContext,bot:Bot):
    contact=m.text.strip()
    if not 3<=len(contact)<=100:return await m.answer('Укажите контакт длиной от 3 до 100 символов.')
    uid=m.from_user.id;save_draft(uid,'contact',contact)
    txt,total=cart_text(uid)
    _,draft=cart_data(uid)
    destination=int(setting('recipient') or OWNER)
    with db() as c:
        cur=c.execute('INSERT INTO orders(user_id,created,summary,total) VALUES(?,?,?,?)',(uid,datetime.now(timezone.utc).isoformat(),txt,total))
        order_id=cur.lastrowid
    msg=f'📨 <b>Новая заявка №{order_id}</b>\nКлиент: {escape(draft["name"])}\nКонтакт: {escape(contact)}\nTelegram ID: <code>{uid}</code>\n\n{txt}'
    try:await bot.send_message(destination,msg,parse_mode='HTML')
    except Exception:
        logging.exception('Cannot deliver order %s',order_id)
        with db() as c:c.execute('UPDATE orders SET status=? WHERE id=?',('delivery_failed',order_id))
        await m.answer('Не удалось доставить заявку администратору. Попробуйте позже или свяжитесь с кафе. Ваша смета сохранена.',reply_markup=home(uid))
    else:
        with db() as c:
            c.execute('UPDATE orders SET status=? WHERE id=?',('sent',order_id))
            c.execute('DELETE FROM carts WHERE user_id=?',(uid,))
            c.execute('DELETE FROM drafts WHERE user_id=?',(uid,))
        await m.answer(f'🎉 Заявка №{order_id} отправлена! Администратор свяжется с вами для подтверждения деталей.\nСумма: {total} ₽. Это не оплата и не подтверждённая бронь.',reply_markup=home(uid))
    await state.clear()

# Управление: владелец назначает получателя; сотрудник редактирует каталог.
@router.callback_query(F.data=='admin')
async def admin(q:CallbackQuery):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    rows=[[('🗂 Редактировать каталог','admincats')],[('📨 Последние заявки','orders')]]
    if is_owner(q.from_user.id):rows.append([('👤 Назначить получателя заявок','setrecipient')])
    rows.append([('🏠 Главное меню','home')])
    await show(q,'⚙️ <b>Управление кафе</b>\nИзменения каталога сразу доступны клиентам.',kb(rows))
@router.callback_query(F.data=='admincats')
async def admincats(q:CallbackQuery):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    await show(q,'Выберите раздел для редактирования:',cats(True))
@router.callback_query(F.data.startswith('acat:'))
async def acat(q:CallbackQuery):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    _,cat,page=q.data.split(':');await show(q,'Редактирование позиций:',page_items(cat,int(page),True))
@router.callback_query(F.data.startswith('edit:'))
async def edit(q:CallbackQuery):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    iid=int(q.data.split(':')[1])
    with db() as c:r=c.execute('SELECT * FROM items WHERE id=?',(iid,)).fetchone()
    if not r:return await q.answer('Не найдено',show_alert=True)
    await show(q,f'<b>{escape(r["name"])}</b>\nЦена: {r["price"]} ₽\n{escape(r["description"])}\nСтатус: {"показывается" if r["active"] else "скрыта"}',kb([[('✏️ Название',f'field:{iid}:name'),('💰 Цена',f'field:{iid}:price')],[('📝 Описание',f'field:{iid}:description')],[('👁 Скрыть / показать',f'toggle:{iid}')],[('⬅️ Назад',f'acat:{r["category"]}:0')]]))
@router.callback_query(F.data.startswith('field:'))
async def field(q:CallbackQuery,state:FSMContext):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    _,iid,key=q.data.split(':')
    if key not in {'name','price','description'}:return await q.answer('Ошибка',show_alert=True)
    await state.set_state(Form.admin_edit);await state.update_data(iid=int(iid),field=key)
    await q.message.answer(f'Введите новое значение поля «{key}» (для отмены /cancel):');await q.answer()
@router.message(Form.admin_edit,F.text)
async def edit_value(m:Message,state:FSMContext):
    if not is_staff(m.from_user.id):await state.clear();return
    data=await state.get_data();value=m.text.strip();key=data['field']
    if key=='price':
        if not value.isdigit() or int(value)>10000000:return await m.answer('Введите целую цену от 0 до 10 000 000 ₽.')
        value=int(value)
    elif len(value)>(120 if key=='name' else 800) or (key=='name' and not value):return await m.answer('Слишком длинный текст или пустое название.')
    with db() as c:c.execute(f'UPDATE items SET {key}=? WHERE id=?',(value,data['iid']))
    await state.clear();await m.answer('✅ Сохранено.',reply_markup=kb([[('↩️ К позиции',f'edit:{data["iid"]}')]]))
@router.callback_query(F.data.startswith('toggle:'))
async def toggle(q:CallbackQuery):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    iid=int(q.data.split(':')[1])
    with db() as c:c.execute('UPDATE items SET active=1-active WHERE id=?',(iid,))
    await q.answer('Видимость изменена');await edit_callback_refresh(q,iid)
async def edit_callback_refresh(q,iid):
    with db() as c:r=c.execute('SELECT * FROM items WHERE id=?',(iid,)).fetchone()
    await show(q,f'{escape(r["name"])}\nЦена: {r["price"]} ₽\nСтатус: {"показывается" if r["active"] else "скрыта"}',kb([[('✏️ Изменить',f'edit:{iid}')],[('⬅️ Назад',f'acat:{r["category"]}:0')]]))
@router.callback_query(F.data.startswith('add:'))
async def add(q:CallbackQuery,state:FSMContext):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    cat=q.data.split(':')[1]
    with db() as c:exists=c.execute('SELECT 1 FROM categories WHERE id=?',(cat,)).fetchone()
    if not exists:return await q.answer('Неизвестная категория',show_alert=True)
    await state.set_state(Form.admin_add);await state.update_data(category=cat)
    await q.message.answer('Добавление позиции. Отправьте <b>три строки</b>:\nНазвание\nЦена (целое число)\nОписание\n\nДля отмены /cancel',parse_mode='HTML');await q.answer()
@router.message(Form.admin_add,F.text)
async def add_value(m:Message,state:FSMContext):
    if not is_staff(m.from_user.id):await state.clear();return
    lines=m.text.split('\n',2)
    if len(lines)!=3 or not lines[1].strip().isdigit():return await m.answer('Нужны три строки: название, цена числом, описание.')
    name,price,desc=map(str.strip,lines)
    if not name or len(name)>120 or len(desc)>800 or int(price)>10000000:return await m.answer('Проверьте длину названия/описания и цену.')
    data=await state.get_data()
    with db() as c:c.execute('INSERT INTO items(category,name,price,description) VALUES(?,?,?,?)',(data['category'],name,int(price),desc))
    await state.clear();await m.answer('✅ Позиция добавлена.',reply_markup=kb([[('↩️ К разделу',f'acat:{data["category"]}:0')]]))
@router.callback_query(F.data=='setrecipient')
async def setrecipient(q:CallbackQuery,state:FSMContext):
    if not is_owner(q.from_user.id):return await q.answer('Только владелец',show_alert=True)
    await state.set_state(Form.admin_target)
    await q.message.answer('Отправьте числовой Telegram ID нового получателя заявок. Новый сотрудник должен предварительно нажать /start в боте. Для возврата заявок себе отправьте свой ID. /cancel — отмена.')
    await q.answer()
@router.message(Form.admin_target,F.text)
async def target(m:Message,state:FSMContext,bot:Bot):
    if not is_owner(m.from_user.id):await state.clear();return
    value=m.text.strip()
    if not value.isdigit():return await m.answer('Нужен числовой Telegram ID.')
    try:await bot.send_message(int(value),'Вы назначены получателем заявок кафе «Хи-Хи».')
    except Exception:return await m.answer('Не получилось связаться с аккаунтом. Пусть новый администратор откроет бота и нажмёт /start, затем повторите.')
    with db() as c:c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('recipient',value))
    await state.clear();await m.answer(f'✅ Получатель заявок изменён: {value}.',reply_markup=home(m.from_user.id))
@router.callback_query(F.data=='orders')
async def orders(q:CallbackQuery):
    if not is_staff(q.from_user.id):return await q.answer('Нет доступа',show_alert=True)
    with db() as c:rows=c.execute('SELECT id,total,status,created FROM orders ORDER BY id DESC LIMIT 10').fetchall()
    txt='📨 <b>Последние заявки</b>\n'+'\n'.join(f'№{r["id"]} — {r["total"]} ₽ — {escape(r["status"])}' for r in rows)
    await show(q,txt,kb([[('⬅️ Управление','admin')]]))
@router.message()
async def fallback(m:Message):await m.answer('Используйте кнопки меню или /start. Для отмены действия — /cancel.',reply_markup=home(m.from_user.id))

async def main():
    if not TOKEN or TOKEN=='PASTE_TOKEN_LOCALLY':raise RuntimeError('Укажите BOT_TOKEN в .env')
    init_db();logging.basicConfig(level=logging.INFO)
    bot=Bot(TOKEN);dp=Dispatcher();dp.include_router(router)
    await dp.start_polling(bot)
if __name__=='__main__':asyncio.run(main())
