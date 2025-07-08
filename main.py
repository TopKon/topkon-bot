# main.py - Topkon Bot Complete
"""
Коробочное решение: один файл main.py
Функционал:
- Роли: Администратор, Руководитель, Водитель
- Регистрация с выбором роли и компании
- Начало смены, заправка, завершение смены
- Меню команд после каждого запроса
- Обработка ошибок ввода и непонимания
- Flask-заглушка для Render
"""
from __future__ import annotations
import os
import sys
import subprocess
import threading
import datetime
import asyncio
from zoneinfo import ZoneInfo
from typing import Dict, Optional

# Auto-install dependencies if missing
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
    import telegram  # noqa: E402

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

# Constants
TOKEN = os.getenv("TOKEN", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TZ = ZoneInfo("Europe/Moscow")

# States
(
    ROLE_SELECT,
    REG_COMPANY,
    REG_NAME,
    REG_CAR,
    START_ODO,
    START_PHOTO,
    FUEL_PHOTO,
    FUEL_COST,
    FUEL_LITERS,
    END_ODO,
    END_PHOTO,
) = range(11)

# Header for log sheet
HEADER = [
    "Дата", "UID", "Роль", "Компания", "ФИО", "Авто",
    "Тип", "Время", "ОДО", "Фото", "Сумма", "Литры", "Δ_км", "Личный_км"
]
IDX = {h: i for i, h in enumerate(HEADER)}

# Flask stub for Render

def _fake_web():
    app = Flask(__name__)
    @app.get('/')
    def ping():
        return "OK", 200
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=_fake_web, daemon=True).start()

# Initialize Google Sheets

def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS, scope)
    gc = gspread.authorize(creds)
    wb = gc.open_by_key(SPREADSHEET_ID)
    # Log sheet
    log_ws = wb.sheet1
    if log_ws.row_values(1) != HEADER:
        log_ws.clear()
        log_ws.append_row(HEADER)
    # Users sheet
    try:
        usr_ws = wb.worksheet('Users')
    except WorksheetNotFound:
        usr_ws = wb.add_worksheet('Users', 1000, 5)
        usr_ws.append_row(["UID", "Роль", "Компания", "Авто", "ФИО"])
    return log_ws, usr_ws

LOG_WS, USR_WS = init_sheets()

# Load users dict from sheet
USERS: Dict[str, Dict] = {}
for row in USR_WS.get_all_values()[1:]:
    if len(row) < 5 or not row[0].isdigit():
        continue
    uid, role, company, car, name = row
    USERS[uid] = {"role": role, "company": company, "car": car, "name": name}

# Helpers

def now_iso() -> str:
    return datetime.datetime.now(TZ).isoformat(timespec='seconds')

def append_log(uid: str, **fields) -> None:
    row = [""] * len(HEADER)
    row[IDX['Дата']] = datetime.date.today(TZ).isoformat()
    row[IDX['UID']] = uid
    info = USERS.get(uid, {})
    row[IDX['Роль']] = info.get('role', '')
    row[IDX['Компания']] = info.get('company', '')
    row[IDX['ФИО']] = info.get('name', '')
    row[IDX['Авто']] = info.get('car', '')
    row[IDX['Тип']] = fields.get('Тип', '')
    row[IDX['Время']] = now_iso()
    for k, v in fields.items():
        if k in IDX:
            row[IDX[k]] = str(v)
    LOG_WS.append_row(row)


def last_odo(uid: str, only_type: Optional[str] = None) -> int:
    for rec in reversed(LOG_WS.get_all_records()):
        if str(rec['UID']) == uid and (only_type is None or rec['Тип'] == only_type):
            try:
                return int(rec['ОДО'])
            except:
                pass
    return 0


def menu_keyboard(uid: str) -> ReplyKeyboardMarkup:
    role = USERS.get(uid, {}).get('role')
    base = ['/startshift', '/fuel', '/endshift', '/help']
    if role == 'Admin':
        base.insert(0, '/addcompany')
    return ReplyKeyboardMarkup([base], resize_keyboard=True)

async def ensure_reg(update: Update) -> bool:
    uid = str(update.effective_user.id)
    if uid in USERS:
        return True
    await update.message.reply_text("Пожалуйста, зарегистрируйтесь: /start")
    return False

# Handlers

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in USERS:
        await update.message.reply_text(
            f"Привет, {USERS[uid]['name']}! Выберите команду:",
            reply_markup=menu_keyboard(uid)
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Добро пожаловать! Выберите роль:",
        reply_markup=ReplyKeyboardMarkup([['Водитель','Руководитель']],resize_keyboard=True)
    )
    return ROLE_SELECT

async def role_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role = update.message.text.strip()
    if role not in ('Водитель','Руководитель'):
        await update.message.reply_text("Выберите роль из списка.")
        return ROLE_SELECT
    ctx.user_data['role'] = role
    await update.message.reply_text(
        "Введите компанию (ООО/ИП/АО 'Название'): ",
        reply_markup=ReplyKeyboardRemove()
    )
    return REG_COMPANY

async def reg_company(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['company'] = update.message.text.strip()
    uid = str(update.effective_user.id)
    role = ctx.user_data['role']
    if role == 'Руководитель':
        USERS[uid] = {'role': 'Руководитель', 'company': ctx.user_data['company'], 'car': '', 'name': ''}
        USR_WS.append_row([uid,'Руководитель',ctx.user_data['company'],'',''])
        await update.message.reply_text(
            "✅ Вы зарегистрированы как Руководитель.",
            reply_markup=menu_keyboard(uid)
        )
        return ConversationHandler.END
    await update.message.reply_text("Введите ФИО:")
    return REG_NAME

async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['name'] = update.message.text.strip()
    await update.message.reply_text("Введите номер авто:")
    return REG_CAR

async def reg_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    USERS[uid] = {
        'role': 'Водитель', 'company': ctx.user_data['company'],
        'car': update.message.text.strip(), 'name': ctx.user_data['name']
    }
    USR_WS.append_row([
        uid,'Водитель',ctx.user_data['company'],update.message.text.strip(),ctx.user_data['name']
    ])
    await update.message.reply_text(
        "✅ Регистрация завершена.",
        reply_markup=menu_keyboard(uid)
    )
    return ConversationHandler.END

async def startshift_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_reg(update): return
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "Укажите пробег на начало смены (км):",
        reply_markup=menu_keyboard(uid)
    )
    return START_ODO

async def start_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        v = int(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Попробуйте снова:")
        return START_ODO
    prev = last_odo(uid,'End')
    out = v - prev
    append_log(uid, Тип='Start', ОДО=v, Личный_км=out)
    await update.message.reply_text(
        f"✅ Смена начата. Пробег вне смены: {out} km.",
        reply_markup=menu_keyboard(uid)
    )
    return ConversationHandler.END

async def fuel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_reg(update): return
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "Пришлите фото чека:",reply_markup=menu_keyboard(uid)
    )
    return FUEL_PHOTO

async def fuel_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Отправьте фото чека:")
        return FUEL_PHOTO
    ctx.user_data['f_photo'] = update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму (₽):")
    return FUEL_COST

async def fuel_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        c = float(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Введите сумму:")
        return FUEL_COST
    ctx.user_data['f_cost'] = c
    await update.message.reply_text("Введите литры:")
    return FUEL_LITERS

async def fuel_liters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        l = float(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Введите литры:")
        return FUEL_LITERS
    append_log(
        uid, Тип='Fuel', Фото=ctx.user_data.pop('f_photo'), Сумма=ctx.user_data.pop('f_cost'), Литры=l
    )
    await update.message.reply_text(
        "✅ Заправка сохранена.",reply_markup=menu_keyboard(uid)
    )
    return ConversationHandler.END

async def endshift_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_reg(update): return
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "Укажите пробег на конец смены (km):",
        reply_markup=menu_keyboard(uid)
    )
    return END_ODO

async def end_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        v = int(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Попробуйте снова:")
        return END_ODO
    prev = last_odo(uid,'Start')
    delta = v - prev
    append_log(uid, Тип='End', ОДО=v, Δ_км=delta)
    await update.message.reply_text(
        f"✅ Смена завершена. Вы проехали {delta} km. Хорошего отдыха!",
        reply_markup=menu_keyboard(uid)
    )
    return ConversationHandler.END

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text("⚙️ Доступные команды:", reply_markup=menu_keyboard(uid))

# Main

def main() -> None:
    if not TOKEN:
        raise RuntimeError("TOKEN env var not set")
    app = ApplicationBuilder().token(TOKEN).build()
    # Registration
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler('start',cmd_start)],
            states={
                ROLE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, role_select)],
                REG_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_company)],
                REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
                REG_CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
            },
            fallbacks=[CommandHandler('start',cmd_start)]
        )
    )
    # Start shift





















