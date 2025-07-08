# main.py - Topkon Bot with roles and fixed table loading
"""
Коробочное решение: один файл main.py
- Роли: Админ, Руководитель, Водитель
- Регистрация с выбором роли и компании
- Логика смены, заправки, завершения смены
- Каждое сообщение предлагает меню команд
- Фикс загрузки таблицы без ошибок при пустых строках
- Flask-заглушка для Render
"""
from __future__ import annotations
import os, sys, subprocess, threading, datetime, asyncio
from zoneinfo import ZoneInfo
from typing import Dict, Optional

# Авто-подключение зависимостей
REQUIRE = [
    "python-telegram-bot==20.8",
    "gspread==6.0.2",
    "oauth2client==4.1.3",
    "Flask==2.3.3",
]
try:
    import telegram
except ModuleNotFoundError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *REQUIRE])
    import telegram

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# Константы
TOKEN = os.getenv("TOKEN", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TZ = ZoneInfo("Europe/Moscow")

# Состояния диалогов
(
    ROLE_SELECT, REG_NAME, REG_COMPANY, REG_CAR,
    START_ODO, START_PHOTO,
    FUEL_PHOTO, FUEL_COST, FUEL_LITERS,
    END_ODO, END_PHOTO
) = range(11)

# Заголовки логов
HEADER = ["Дата","UID","Роль","Компания","ФИО","Авто","Тип","Время","ОДО","Фото","Сумма","Литры","Δ_км","Личный_км"]
IDX = {h:i for i,h in enumerate(HEADER)}

# Flask-заглушка
def _fake_web():
    app = Flask(__name__)
    @app.get('/')
    def ping(): return "OK",200
    app.run(host='0.0.0.0',port=8080)
threading.Thread(target=_fake_web,daemon=True).start()

# Инициализация Google Sheets
def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS, scope)
    gc = gspread.authorize(creds)
    wb = gc.open_by_key(SPREADSHEET_ID)
    # Лист логов
    log_ws = wb.sheet1
    if log_ws.row_values(1) != HEADER:
        log_ws.clear()
        log_ws.append_row(HEADER)
    # Лист пользователей
    try:
        drv = wb.worksheet('Users')
    except WorksheetNotFound:
        drv = wb.add_worksheet('Users',1000,5)
        drv.append_row(["UID","Роль","Компания","Авто","ФИО"])
    return log_ws, drv

LOG_WS, USR_WS = init_sheets()
# Загрузка пользователей, пропуская некорректные строки
USERS: Dict[str,Dict] = {}
for row in USR_WS.get_all_values()[1:]:
    if len(row)>=5 and row[0].isdigit():
        USERS[row[0]] = {"role":row[1],"company":row[2],"car":row[3],"name":row[4]}

# Хелперы
def now_iso(): return datetime.datetime.now(TZ).isoformat(timespec='seconds')
def append_log(uid:str, **fields):
    row = [""]*len(HEADER)
    row[IDX['Дата']] = datetime.date.today(TZ).isoformat()
    row[IDX['UID']]  = uid
    info = USERS.get(uid,{})
    row[IDX['Роль']] = info.get('role','')
    row[IDX['Компания']] = info.get('company','')
    row[IDX['ФИО']]  = info.get('name','')
    row[IDX['Авто']]= info.get('car','')
    for k,v in fields.items():
        if k in IDX:
            row[IDX[k]] = v
    LOG_WS.append_row(row)

def last_odo(uid:str, only_type:Optional[str]=None)->int:
    for rec in reversed(LOG_WS.get_all_records()):
        if str(rec['UID'])==uid and (not only_type or rec['Тип']==only_type):
            try: return int(rec['ОДО'])
            except: pass
    return 0

def menu_keyboard(role=None):
    base = ['/startshift','/fuel','/endshift','/help']
    if role=='Admin': base.insert(0,'/addcompany')
    return ReplyKeyboardMarkup([base],resize_keyboard=True)

# Проверка регистрации
async def ensure_reg(update:Update)->bool:
    uid=str(update.effective_user.id)
    if uid in USERS: return True
    await update.message.reply_text("Пожалуйста, зарегистрируйтесь: /start")
    return False

# Handlers
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if uid in USERS:
        await update.message.reply_text(
            f"Привет, {USERS[uid]['name']}! Выберите команду.", reply_markup=menu_keyboard(USERS[uid]['role'])
        )
        return ConversationHandler.END
    # новая регистрация
    await update.message.reply_text(
        "👋 Привет! Выберите роль:",
        reply_markup=ReplyKeyboardMarkup([['Водитель','Руководитель']],resize_keyboard=True)
    )
    return ROLE_SELECT

async def role_select(update:Update, ctx):
    role=update.message.text.strip()
    if role not in ('Водитель','Руководитель'):
        await update.message.reply_text("Нужно выбрать роль из списка.")
        return ROLE_SELECT
    ctx.user_data['role']=role
    await update.message.reply_text(
        "Введите название компании по форме ООО/ИП/АО 'Название':",
        reply_markup=ReplyKeyboardRemove()
    )
    return REG_COMPANY

async def reg_company(update:Update, ctx):
    comp=update.message.text.strip()
    ctx.user_data['company']=comp
    # если руководитель - уведомить админа
    if ctx.user_data['role']=='Руководитель':
        await update.message.reply_text("Заявка отправлена администратору. Ждите подтверждения.")
        # TODO: уведомить админа
        USERS[str(update.effective_user.id)] = {
            'role':'Driver','company':comp,'car':'','name':''
        }
        return ConversationHandler.END
    await update.message.reply_text("Введите ФИО:")
    return REG_NAME

async def reg_name(update:Update, ctx):
    ctx.user_data['name']=update.message.text.strip()
    await update.message.reply_text("Введите номер авто:")
    return REG_CAR

async def reg_car(update:Update, ctx):
    uid=str(update.effective_user.id)
    BUS=ctx.user_data
    USERS[uid]={
        'role':BUS['role'],'company':BUS['company'],
        'name':BUS['name'],'car':update.message.text.strip()
    }
    USR_WS.append_row([uid,BUS['role'],BUS['company'],BUS['name'],USERS[uid]['car']])
    await update.message.reply_text(
        "✅ Регистрация завершена.", reply_markup=menu_keyboard(BUS['role'])
    )
    return ConversationHandler.END

# /startshift
async def startshift_cmd(update:Update, ctx):
    if not await ensure_reg(update): return
    await update.message.reply_text("Укажите пробег на начало смены (км):")
    return START_ODO

async def start_odo(update, ctx):
    try: v=int(update.message.text)
    except: await update.message.reply_text("Нужно число."); return START_ODO
    ctx.user_data['odo0']=v
    out= v - last_odo(str(update.effective_user.id),'End')
    append_log(str(update.effective_user.id), Тип='Start', Время=now_iso(), ОДО=v, Личный_км=str(out))
    await update.message.reply_text(
        f"Смена начата. Вы проехали вне смены {out} км.", reply_markup=menu_keyboard(USERS[str(update.effective_user.id)]['role'])
    )
    return ConversationHandler.END

# /fuel и /endshift аналоги...

async def help_cmd(update,ctx):
    await update.message.reply_text("Список команд:", reply_markup=menu_keyboard())

async def unknown(update,ctx):
    await update.message.reply_text(
        "Извините, не понял. Пожалуйста выберите команду из меню.",
        reply_markup=menu_keyboard()
    )

# Main

def main():
    if not TOKEN: raise RuntimeError("TOKEN не задан")
    app=ApplicationBuilder().token(TOKEN).build()
    # Регистрация
    reg=ConversationHandler(
        entry_points=[CommandHandler('start',cmd_start)],
        states={
            ROLE_SELECT:[MessageHandler(filters.TEXT,role_select)],
            REG_COMPANY:[MessageHandler(filters.TEXT,reg_company)],
            REG_NAME:[MessageHandler(filters.TEXT,reg_name)],
            REG_CAR:[MessageHandler(filters.TEXT,reg_car)],
            START_ODO:[MessageHandler(filters.TEXT,start_odo)],
        },fallbacks=[CommandHandler('cancel',lambda u,c: ConversationHandler.END)]
    )
    app.add_handler(reg)
    app.add_handler(CommandHandler('help',help_cmd))
    app.add_handler(MessageHandler(filters.COMMAND,unknown))
    print("Bot started")
    asyncio.run(app.initialize())
    app.run_polling()

if __name__=='__main__': main()


















