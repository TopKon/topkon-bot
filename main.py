# -*- coding: utf-8 -*-
"""
main.py — Topkon Bot Complete

Коробочное решение: один файл main.py
Поддерживает роли Администратор, Руководитель и Водитель.
Функции:
 - Регистрация с выбором роли и компании
 - Начало смены (/startshift)
 - Заправка (/fuel)
 - Завершение смены (/endshift)
 - Помощь (/help)
 - Всегда отображает меню после каждого сообщения
 - Обработка неизвестных команд
 - Flask-заглушка для Render бесплатного тарифа

Администратор по умолчанию: UID 1881053841
"""
from __future__ import annotations
import os, sys, subprocess, threading, datetime
from zoneinfo import ZoneInfo
from typing import Dict, Optional

# Авто‑установка зависимостей
REQUIRE = [
    "python-telegram-bot==20.8",
    "gspread==6.0.2",
    "oauth2client==4.1.3",
    "Flask==2.3.3",
]
try:
    import telegram  # noqa: F401
except ModuleNotFoundError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *REQUIRE])
    import telegram  # noqa: F401

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# Константы
TOKEN = os.getenv("TOKEN", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TZ = ZoneInfo("Europe/Moscow")
ADMIN_UID = '1881053841'

# Состояния
(
    ROLE_SELECT,
    REG_COMPANY,
    REG_NAME,
    REG_CAR,
    START_ODO,
    FUEL_PHOTO,
    FUEL_COST,
    FUEL_LITERS,
    END_ODO,
) = range(9)

# Заголовки лога\HEADER = [
    "Дата", "UID", "Роль", "Компания", "ФИО", "Авто",
    "Тип", "Время", "ОДО", "Фото", "Сумма", "Литры", "Δ_км", "Личный_км"
]
IDX = {h: i for i, h in enumerate(HEADER)}

# Flask-заглушка для Render

def _fake_web():
    app = Flask(__name__)
    @app.get("/")
    def ping():
        return "OK", 200
    app.run(host="0.0.0.0", port=8080)
threading.Thread(target=_fake_web, daemon=True).start()

# Инициализация Google Sheets

def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS, scope)
    gc = gspread.authorize(creds)
    wb = gc.open_by_key(SPREADSHEET_ID)
    # Лист лога
    log_ws = wb.sheet1
    if log_ws.row_values(1) != HEADER:
        log_ws.clear()
        log_ws.append_row(HEADER)
    # Лист пользователей
    try:
        usr_ws = wb.worksheet('Users')
    except WorksheetNotFound:
        usr_ws = wb.add_worksheet('Users', 1000, 5)
        usr_ws.append_row(["UID","Роль","Компания","Авто","ФИО"])
    return log_ws, usr_ws

LOG_WS, USR_WS = init_sheets()

# Загрузка пользователей в память
USERS: Dict[str, Dict] = {}
for row in USR_WS.get_all_values()[1:]:
    if len(row) < 5:
        continue
    uid, role, company, car, name = row[:5]
    USERS[uid] = {"role": role, "company": company, "car": car, "name": name}

# Вспомогательные функции

def now_iso() -> str:
    return datetime.datetime.now(TZ).isoformat(timespec='seconds')

def append_log(uid: str, **fields) -> None:
    row = [""] * len(HEADER)
    row[IDX['Дата']] = datetime.date.today(TZ).isoformat()
    row[IDX['UID']] = uid
    info = USERS.get(uid, {})
    row[IDX['Роль']] = info.get('role','')
    row[IDX['Компания']] = info.get('company','')
    row[IDX['ФИО']] = info.get('name','')
    row[IDX['Авто']] = info.get('car','')
    row[IDX['Тип']] = fields.get('Тип','')
    row[IDX['Время']] = now_iso()
    for k,v in fields.items():
        if k in IDX:
            row[IDX[k]] = str(v)
    LOG_WS.append_row(row)

def last_odo(uid: str, only_type: Optional[str]=None) -> int:
    for rec in reversed(LOG_WS.get_all_records()):
        if str(rec.get('UID'))==uid and (only_type is None or rec.get('Тип')==only_type):
            try:
                return int(rec.get('ОДО',0))
            except:
                pass
    return 0

def menu_keyboard(uid: str) -> ReplyKeyboardMarkup:
    keys = ['/startshift','/fuel','/endshift','/help']
    return ReplyKeyboardMarkup([keys], resize_keyboard=True)

async def ensure_reg(update: Update) -> bool:
    uid = str(update.effective_user.id)
    if uid in USERS:
        return True
    await update.message.reply_text("Пожалуйста, зарегистрируйтесь: /start")
    return False

# Обработчики команд

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in USERS:
        await update.message.reply_text(
            f"Здравствуйте, {USERS[uid]['name']}! Выберите команду:",
            reply_markup=menu_keyboard(uid)
        )
        return ConversationHandler.END
    # регистрация
    await update.message.reply_text(
        "👋 Добро пожаловать! Выберите роль:",
        reply_markup=ReplyKeyboardMarkup([['Водитель','Руководитель']], resize_keyboard=True)
    )
    return ROLE_SELECT

async def role_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice not in ('Водитель','Руководитель'):
        await update.message.reply_text("Пожалуйста, выберите роль: Водитель или Руководитель.")
        return ROLE_SELECT
    ctx.user_data['role'] = choice
    await update.message.reply_text("Введите компанию (ООО/ИП/АО 'Название'):", reply_markup=ReplyKeyboardRemove())
    return REG_COMPANY

async def reg_company(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    company = update.message.text.strip()
    role = ctx.user_data['role']
    if role=='Руководитель':
        USERS[uid] = {'role':role,'company':company,'car':'','name':''}
        USR_WS.append_row([uid,role,company,'',''])
        await update.message.reply_text("✅ Вы зарегистрированы как Руководитель.", reply_markup=menu_keyboard(uid))
        return ConversationHandler.END
    # водитель
    ctx.user_data['company'] = company
    await update.message.reply_text("Введите ФИО:")
    return REG_NAME

async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['name'] = update.message.text.strip()
    await update.message.reply_text("Введите номер авто:")
    return REG_CAR

async def reg_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    car = update.message.text.strip()
    USERS[uid] = {'role':'Водитель','company':ctx.user_data['company'],'car':car,'name':ctx.user_data['name']}
    USR_WS.append_row([uid,'Водитель',ctx.user_data['company'],car,ctx.user_data['name']])
    await update.message.reply_text("✅ Регистрация завершена.", reply_markup=menu_keyboard(uid))
    return ConversationHandler.END

async def startshift_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_reg(update): return ConversationHandler.END
    uid = str(update.effective_user.id)
    await update.message.reply_text("Укажите пробег на начало смены (км):", reply_markup=menu_keyboard(uid))
    return START_ODO

async def start_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        v = int(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Повторите пробег:")
        return START_ODO
    prev = last_odo(uid,'End')
    out = v - prev
    append_log(uid, Тип='Start', ОДО=v, Личный_км=out)
    await update.message.reply_text(f"✅ Смена начата. Пробег вне смены: {out} km.", reply_markup=menu_keyboard(uid))
    return ConversationHandler.END

async def fuel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_reg(update): return ConversationHandler.END
    uid = str(update.effective_user.id)
    await update.message.reply_text("Пришлите фото чека:", reply_markup=menu_keyboard(uid))
    return FUEL_PHOTO

async def fuel_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Отправьте фото чека:")
        return FUEL_PHOTO
    ctx.user_data['photo'] = update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму (₽):")
    return FUEL_COST

async def fuel_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        c = float(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Повторите сумму:")
        return FUEL_COST
    ctx.user_data['cost'] = c
    await update.message.reply_text("Введите литры:")
    return FUEL_LITERS

async def fuel_liters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        l = float(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Повторите литры:")
        return FUEL_LITERS
    append_log(uid, Тип='Fuel', Фото=ctx.user_data.pop('photo'), Сумма=ctx.user_data.pop('cost'), Литры=l)
    await update.message.reply_text("✅ Заправка сохранена.", reply_markup=menu_keyboard(uid))
    return ConversationHandler.END

async def endshift_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_reg(update): return ConversationHandler.END
    uid = str(update.effective_user.id)
    await update.message.reply_text("Укажите пробег на конец смены (км):", reply_markup=menu_keyboard(uid))
    return END_ODO

async def end_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        v = int(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Повторите пробег:")
        return END_ODO
    prev = last_odo(uid,'Start')
    delta = v - prev
    # часы работы
    recs = LOG_WS.get_all_records()
    start_time = None
    for rec in reversed(recs):
        if str(rec.get('UID'))==uid and rec.get('Тип')=='Start':
            start_time = datetime.datetime.fromisoformat(rec.get('Время'))
            break
    now = datetime.datetime.now(TZ)
    hours = round((now - start_time).total_seconds()/3600,2) if start_time else 0
    append_log(uid, Тип='End', ОДО=v, Δ_км=delta)
    await update.message.reply_text(
        f"✅ Смена завершена. Вы проехали {delta} км и работали {hours} ч. Приятного отдыха!", reply_markup=menu_keyboard(uid)
    )
    return ConversationHandler.END

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "/start — регистрация\n/startshift — начать смену\n/fuel — заправка\n/endshift — завершить смену\n/help — помощь",
        reply_markup=menu_keyboard(uid)
    )

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "Извините, не понял запрос. Пожалуйста, выберите команду из меню.", reply_markup=menu_keyboard(uid)
    )

# Основная функция

def main() -> None:
    if not TOKEN:
        raise RuntimeError("TOKEN env var not set")
    app = ApplicationBuilder().token(TOKEN).build()
    # обработчики
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler('start', cmd_start)],
        states={
            ROLE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, role_select)],
            REG_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_company)],
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
        },
        fallbacks=[CommandHandler('start', cmd_start)],
    )
    app.add_handler(reg_conv)
    start_conv = ConversationHandler(
        entry_points=[CommandHandler('startshift', startshift_cmd)],
        states={START_ODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_odo)]},
        fallbacks=[CommandHandler('help', help_cmd)],
    )
    app.add_handler(start_conv)
    fuel_conv = ConversationHandler(
        entry_points=[CommandHandler('fuel', fuel_cmd)],
        states={
            FUEL_PHOTO: [MessageHandler(filters.PHOTO, fuel_photo)],
            FUEL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, fuel_cost)],
            FUEL_LITERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, fuel_liters)],
        },
        fallbacks=[CommandHandler('help', help_cmd)],
    )
    app.add_handler(fuel_conv)
    end_conv = ConversationHandler(
        entry_points=[CommandHandler('endshift', endshift_cmd)],
        states={END_ODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, end_odo)]},
        fallbacks=[CommandHandler('help', help_cmd)],
    )
    app.add_handler(end_conv)
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(MessageHandler(filters.ALL, unknown))

    print("🔄 Bot polling started", flush=True)
    app.run_polling()

if __name__ == '__main__':
    main()























