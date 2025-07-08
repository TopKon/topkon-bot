# -*- coding: utf-8 -*-
"""
Топкон-бот  v1.0
— однократная регистрация (ФИО + авто)
— смена: старт → топливо (по желанию) → конец
— подсчёт «личного» пробега (разница до начала смены)
— Google Sheets для данных
— Flask-порт 8080, чтобы Render Free видел HTTP-порт
"""

import os, threading, datetime, asyncio
from collections import defaultdict
from zoneinfo import ZoneInfo

# ── Flask-заглушка ────────────────────────────────────────────────────────────
from flask import Flask

def run_fake_web():
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "Bot is alive!", 200

    # 8080 — стандарт для Render Free
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_fake_web, daemon=True).start()

# ── Telegram ─────────────────────────────────────────────────────────────────
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters,
    ConversationHandler
)

# ── Google Sheets ────────────────────────────────────────────────────────────
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# ── Константы ────────────────────────────────────────────────────────────────
MOSCOW = ZoneInfo("Europe/Moscow")
BOT_NAME = "Топкон"

# состояния ConversationHandler-ов
REG_NAME, REG_CAR          = range(2)
START_ODO, START_PHOTO     = range(2, 4)
FUEL_PHOTO, FUEL_COST, FUEL_LITERS = range(4, 7)
END_ODO, END_PHOTO         = range(7, 9)

# ── Google Sheets: подготовка ────────────────────────────────────────────────
def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
    )
    client = gspread.authorize(creds)
    wb = client.open_by_key(os.getenv("SPREADSHEET_ID"))

    # Лист-журнал (по умолчанию = Sheet1)
    log_sheet = wb.sheet1
    head = [
        "Дата", "ВодительID", "ФИО", "Авто",
        "Тип", "Время", "ОДО", "Фото_ID",
        "Литры", "Сумма", "Δ_км", "Личный_км"
    ]
    if log_sheet.row_values(1) != head:
        log_sheet.clear()
        log_sheet.append_row(head)

    # отдельный лист Drivers
    try:
        drivers_sheet = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drivers_sheet = wb.add_worksheet("Drivers", 1000, 3)
        drivers_sheet.append_row(["TelegramID", "ФИО", "Авто"])

    return log_sheet, drivers_sheet

LOG, DRIVERS = init_sheets()

# загружаем водителей в память
DRIVER_MAP = {
    row[0]: {"name": row[1], "car": row[2]}
    for row in DRIVERS.get_all_values()[1:]
}

# ── Вспом. функции ───────────────────────────────────────────────────────────
def last_odo(driver_id: str):
    """Последний одометр из журнала (или None)."""
    rows = LOG.get_all_records()
    for row in reversed(rows):
        if row["ВодительID"] == driver_id and row["ОДО"]:
            return int(row["ОДО"])
    return None

async def ensure_registered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in DRIVER_MAP:
        return True
    await update.message.reply_text("🚗 Вы не зарегистрированы. Введите ФИО:")
    return False

# ── Регистрация (однократная) ────────────────────────────────────────────────
async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name_tmp"] = update.message.text.strip()
    await update.message.reply_text("Введите номер автомобиля:")
    return REG_CAR

async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    name = context.user_data.pop("name_tmp")
    car = update.message.text.strip()

    DRIVERS.append_row([uid, name, car])
    DRIVER_MAP[uid] = {"name": name, "car": car}
    await update.message.reply_text(f"✅ Сохранено. {name}, можно начать смену командой /startshift")
    return ConversationHandler.END

# ── Старт смены ──────────────────────────────────────────────────────────────
async def startshift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    await update.message.reply_text("Введите пробег на начало смены (только число):")
    return START_ODO

async def startshift_odo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odo = int(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число, попробуйте ещё раз:")
        return START_ODO
    context.user_data["start_odo"] = odo
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO

async def startshift_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = update.message.photo[-1].file_id if update.message.photo else ""
    uid = str(update.effective_user.id)
    name = DRIVER_MAP[uid]["name"]
    car  = DRIVER_MAP[uid]["car"]
    odo_start = context.user_data.pop("start_odo")
    today = datetime.date.today(MOSCOW).isoformat()

    # личный пробег = текущий одо - последний одо
    last = last_odo(uid)
    personal_km = odo_start - last if last else 0

    LOG.append_row([
        today, uid, name, car,
        "Start", datetime.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        odo_start, photo_id, "", "", "", personal_km
    ])
    await update.message.reply_text("✅ Смена начата. Команды:\n/fuel – заправка\n/endshift – конец смены")
    return ConversationHandler.END

# ── Заправка ─────────────────────────────────────────────────────────────────
async def fuel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пришлите фото чека:")
    return FUEL_PHOTO

async def fuel_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fuel_photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму чека (₽):")
    return FUEL_COST

async def fuel_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cost = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите сумму:")
        return FUEL_COST
    context.user_data["fuel_cost"] = cost
    await update.message.reply_text("Введите количество литров:")
    return FU




